# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Map tools for the ReAct agent: geocoding real places and rendering them as
an interactive map with default Google Maps markers.

Unlike a static map image, real tap-to-see-info markers need a live Google
Maps JavaScript API page, not a server-rendered PNG (see the Static Maps API
alternative this replaced). ``render_map_embed_html()`` builds that page;
``server.py`` serves it at ``MAP_EMBED_ROUTE_PATH`` over this app's own
(self-signed) HTTPS, and the client's dedicated ``MapWeb`` custom component
(mirroring ``YouTubeWebComponent`` -- see that component's own docstring for
why a *custom* component is needed here, not the generic ``Web`` one) loads
it, exactly the same pattern already proven for ``/youtube-embed``.
"""

import json
from typing import Any, Optional
from urllib.parse import quote, urlencode

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS
from openjiuwen.harness.tools.web._decode import _decode_response_text

from . import config, genui

# Places API (New) -- unlike the legacy Geocoding API, one Text Search call
# here also returns a place's real star rating and a real photo when Google
# has them, so `geocode_place` can offer both without a second API/call.
_PLACES_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
_PLACES_PHOTO_ENDPOINT_PREFIX = "https://places.googleapis.com/v1/"
_PLACES_FIELD_MASK = "places.location,places.formattedAddress,places.rating,places.userRatingCount,places.photos"
_PLACE_PHOTO_MAX_WIDTH_PX = 640

MAP_EMBED_ROUTE_PATH = "/map-embed"


@tool(
    description=(
        "Resolve a real place name or address into map coordinates, via the "
        "actual Google Places API (not guessed from memory) -- call this "
        "once per place before calling `show_map`, so its pin lands on the "
        "correct real-world location. Pass a specific, unambiguous query "
        "(e.g. 'Grand Palace, Bangkok, Thailand' rather than just 'the "
        "palace'). Returns `lat`/`lng` and `formatted_address` on success, "
        "plus `rating`/`user_ratings_total`/`image_url` whenever Google has "
        "them for that place -- pass those straight into `show_map` too so "
        "its info window can show a real photo and star rating, not just a "
        "name. Any of `rating`/`user_ratings_total`/`image_url` can come "
        "back `null` (a place with no photos yet, or no ratings yet, is "
        "normal) -- in that case just omit that field on `show_map`'s place "
        "rather than inventing one. An `error` (e.g. no match found, or the "
        "API key isn't configured) means don't fabricate coordinates for "
        "that place -- leave it out of the map instead."
    )
)
async def geocode_place(query: str) -> dict[str, Any]:
    api_key = config.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return {"query": query, "lat": None, "lng": None, "error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    headers = {**_REQUEST_HEADERS, "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _PLACES_FIELD_MASK}
    try:
        async with _http.new_session() as session:
            status, resp_headers, body, _final_url, _truncated = await _http.request(
                session,
                "POST",
                _PLACES_SEARCH_ENDPOINT,
                headers=headers,
                json_body={"textQuery": query},
                timeout_seconds=15,
                max_bytes=1_000_000,
            )
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"query": query, "lat": None, "lng": None, "error": str(exc)}

    text = _decode_response_text(body, content_type=resp_headers.get("Content-Type", ""))
    if status >= 400:
        return {"query": query, "lat": None, "lng": None, "error": f"Places API returned HTTP {status}: {text[:300]}"}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"query": query, "lat": None, "lng": None, "error": f"Could not parse Places API response: {exc}"}

    places = data.get("places") or []
    if not places:
        return {"query": query, "lat": None, "lng": None, "error": "Places API found no results for this query."}

    place = places[0]
    location = place.get("location") or {}
    if "latitude" not in location or "longitude" not in location:
        return {"query": query, "lat": None, "lng": None, "error": "Places API result had no location."}

    image_url = None
    photos = place.get("photos") or []
    if photos:
        photo_name = photos[0].get("name")
        if photo_name:
            photo_params = urlencode({"maxWidthPx": _PLACE_PHOTO_MAX_WIDTH_PX, "key": api_key})
            image_url = f"{_PLACES_PHOTO_ENDPOINT_PREFIX}{photo_name}/media?{photo_params}"

    return {
        "query": query,
        "lat": location["latitude"],
        "lng": location["longitude"],
        "formatted_address": place.get("formattedAddress", query),
        "rating": place.get("rating"),
        "user_ratings_total": place.get("userRatingCount"),
        "image_url": image_url,
    }


class MapPlace(BaseModel):
    label: str = Field(description="Short name shown for this place, e.g. 'Grand Palace'.")
    lat: float = Field(description="Latitude from a prior `geocode_place` call for this place.")
    lng: float = Field(description="Longitude from a prior `geocode_place` call for this place.")
    image_url: Optional[str] = Field(
        default=None,
        description=(
            "Real photo URL from a prior `geocode_place` call for this place, shown in its info "
            "window when tapped. Omit if `geocode_place` returned `null` -- never invent one."
        ),
    )
    rating: Optional[float] = Field(
        default=None,
        description=(
            "Real star rating (e.g. 4.6) from a prior `geocode_place` call for this place, shown in "
            "its info window when tapped. Omit if `geocode_place` returned `null` -- never invent one."
        ),
    )
    user_ratings_total: Optional[int] = Field(
        default=None,
        description="Real review count from a prior `geocode_place` call, shown alongside `rating` if given.",
    )


_MAP_EMBED_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>html, body, #map { height: 100%; margin: 0; padding: 0; }</style>
</head>
<body>
<div id="map"></div>
<script>
  const PLACES = __PLACES_JSON__;
  function initMap() {
    const bounds = new google.maps.LatLngBounds();
    const map = new google.maps.Map(document.getElementById("map"), {
      zoom: 12,
      center: { lat: PLACES[0].lat, lng: PLACES[0].lng },
    });
    const infoWindow = new google.maps.InfoWindow();
    PLACES.forEach(function (place) {
      const position = { lat: place.lat, lng: place.lng };
      const marker = new google.maps.Marker({
        position: position,
        map: map,
        title: place.label,
      });
      marker.addListener("click", function () {
        // textContent/property assignment (never innerHTML/string concat) so
        // LLM-sourced text can never be interpreted as markup. `image_url`
        // is safe to use as a plain `src` regardless: it's never taken
        // verbatim from anything server-side, always built by this app's
        // own map_tools.geocode_place from a Google Places photo reference.
        const content = document.createElement("div");
        content.style.maxWidth = "220px";
        if (place.image_url) {
          const img = document.createElement("img");
          img.src = place.image_url;
          img.style.width = "100%";
          img.style.height = "120px";
          img.style.objectFit = "cover";
          img.style.borderRadius = "4px";
          img.style.marginBottom = "6px";
          content.appendChild(img);
        }
        const strong = document.createElement("strong");
        strong.textContent = place.label;
        content.appendChild(strong);
        if (place.rating) {
          const ratingDiv = document.createElement("div");
          let ratingText = "★ " + place.rating.toFixed(1);
          if (place.user_ratings_total) {
            ratingText += " (" + place.user_ratings_total.toLocaleString() + ")";
          }
          ratingDiv.textContent = ratingText;
          content.appendChild(ratingDiv);
        }
        infoWindow.setContent(content);
        infoWindow.open(map, marker);
      });
      bounds.extend(position);
    });
    if (PLACES.length > 1) {
      map.fitBounds(bounds);
    } else {
      map.setZoom(15);
    }
  }
</script>
<script src="https://maps.googleapis.com/maps/api/js?key=__API_KEY__&callback=initMap" async defer></script>
</body>
</html>
"""


def _place_payload(p: MapPlace) -> dict[str, Any]:
    return {
        "label": p.label,
        "lat": p.lat,
        "lng": p.lng,
        "image_url": p.image_url,
        "rating": p.rating,
        "user_ratings_total": p.user_ratings_total,
    }


def render_map_embed_html(places: list[MapPlace], api_key: str) -> str:
    """Render a small self-contained HTML page embedding the real Google Maps
    JavaScript API -- unlike a static map image, this gives real interactive
    markers: tapping a pin opens an info window with that place's label,
    photo, and star rating (whichever of those it has). Served by
    ``server.py`` at ``MAP_EMBED_ROUTE_PATH``.
    """
    places_payload = [_place_payload(p) for p in places]
    # Guard against a place label containing a literal "</script>" that would
    # otherwise break out of the surrounding <script> tag.
    places_json = json.dumps(places_payload).replace("</", "<\\/")
    html = _MAP_EMBED_TEMPLATE
    html = html.replace("__PLACES_JSON__", places_json)
    html = html.replace("__API_KEY__", quote(api_key, safe=""))
    return html


@tool(
    description=(
        "Render an interactive map with one or more real places highlighted "
        "as pins, as an A2UI surface -- tapping a pin shows that place's "
        "name, plus its real photo and star rating whenever those "
        "were available from `geocode_place`. Every place's `lat`/`lng` must "
        "come from a prior `geocode_place` call on that place -- never "
        "invent coordinates, ratings, or image URLs; pass through exactly "
        "what `geocode_place` returned (including `null` fields -- just "
        "leave those out of the place, don't invent a replacement value). "
        "This is the tool for \"show me X on a map\"/\"where is X located\" "
        "requests -- geocode every place you want to show first, then call "
        "this once with the full list; don't call it once per place. With "
        "multiple places, the map automatically frames all of them."
    )
)
def show_map(title: str, places: list[MapPlace]) -> dict[str, Any]:
    api_key = config.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return {"text": "Maps aren't configured on the server right now.", "genui": []}
    base_url = config.get("PUBLIC_BASE_URL")
    if not base_url:
        return {"text": "The interactive map isn't configured on the server right now.", "genui": []}
    parsed_places = [p if isinstance(p, MapPlace) else MapPlace(**p) for p in places]
    if not parsed_places:
        return {"text": "I couldn't find any of those places to put on a map.", "genui": []}

    surface_id = genui.new_surface_id("map")
    places_payload = json.dumps([_place_payload(p) for p in parsed_places])
    embed_url = f"{base_url.rstrip('/')}{MAP_EMBED_ROUTE_PATH}?data={quote(places_payload, safe='')}"
    summary = "\n".join(f"- {p.label}" for p in parsed_places)
    return {
        "text": f"{title}\n{summary}",
        "genui": genui.map_card(surface_id, title, embed_url, caption=summary),
    }
