# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Map tools for the ReAct agent: geocoding real places and rendering them as
an interactive map with gold star markers.

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
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS
from openjiuwen.harness.tools.web._decode import _decode_response_text

from . import config, genui

_GEOCODE_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"

# Google's own hosted gold star pin -- used instead of the default red
# teardrop marker per the "highlight places with golden stars" requirement.
_GOLD_STAR_ICON_URL = "https://maps.google.com/mapfiles/kml/paddle/ylw-stars.png"
_STAR_MARKER_SIZE_PX = 32

MAP_EMBED_ROUTE_PATH = "/map-embed"


@tool(
    description=(
        "Resolve a real place name or address into map coordinates, via the "
        "actual Google Geocoding API (not guessed from memory) -- call this "
        "once per place before calling `show_map`, so its star lands on the "
        "correct real-world location. Pass a specific, unambiguous query "
        "(e.g. 'Grand Palace, Bangkok, Thailand' rather than just 'the "
        "palace'). Returns `lat`/`lng` and the resolved `formatted_address` "
        "on success. An `error` (e.g. no match found, or the API key isn't "
        "configured) means don't fabricate coordinates for that place -- "
        "leave it out of the map instead."
    )
)
async def geocode_place(query: str) -> dict[str, Any]:
    api_key = config.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return {"query": query, "lat": None, "lng": None, "error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    params = {"address": query, "key": api_key}
    url = _GEOCODE_ENDPOINT + "?" + urlencode(params)
    try:
        async with _http.new_session() as session:
            status, headers, body, _final_url, _truncated = await _http.request(
                session, "GET", url, headers=_REQUEST_HEADERS, timeout_seconds=15, max_bytes=1_000_000
            )
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"query": query, "lat": None, "lng": None, "error": str(exc)}

    text = _decode_response_text(body, content_type=headers.get("Content-Type", ""))
    if status >= 400:
        return {"query": query, "lat": None, "lng": None, "error": f"Geocoding API returned HTTP {status}: {text[:300]}"}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"query": query, "lat": None, "lng": None, "error": f"Could not parse Geocoding API response: {exc}"}

    api_status = data.get("status")
    if api_status != "OK" or not data.get("results"):
        error_message = data.get("error_message") or api_status or "no results"
        return {"query": query, "lat": None, "lng": None, "error": f"Geocoding failed: {error_message}"}

    result = data["results"][0]
    location = result["geometry"]["location"]
    return {
        "query": query,
        "lat": location["lat"],
        "lng": location["lng"],
        "formatted_address": result.get("formatted_address", query),
    }


class MapPlace(BaseModel):
    label: str = Field(description="Short name shown for this place, e.g. 'Grand Palace'.")
    lat: float = Field(description="Latitude from a prior `geocode_place` call for this place.")
    lng: float = Field(description="Longitude from a prior `geocode_place` call for this place.")


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
        icon: {
          url: __STAR_ICON_JSON__,
          scaledSize: new google.maps.Size(__MARKER_SIZE__, __MARKER_SIZE__),
        },
      });
      marker.addListener("click", function () {
        // textContent (not innerHTML) so a place label can never be
        // interpreted as markup -- it's LLM-sourced, not hand-authored.
        const content = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = place.label;
        content.appendChild(strong);
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


def render_map_embed_html(places: list[MapPlace], api_key: str) -> str:
    """Render a small self-contained HTML page embedding the real Google Maps
    JavaScript API -- unlike a static map image, this gives real interactive
    markers: tapping a gold star opens an info window with that place's
    label. Served by ``server.py`` at ``MAP_EMBED_ROUTE_PATH``.
    """
    places_payload = [{"label": p.label, "lat": p.lat, "lng": p.lng} for p in places]
    # Guard against a place label containing a literal "</script>" that would
    # otherwise break out of the surrounding <script> tag.
    places_json = json.dumps(places_payload).replace("</", "<\\/")
    html = _MAP_EMBED_TEMPLATE
    html = html.replace("__PLACES_JSON__", places_json)
    html = html.replace("__STAR_ICON_JSON__", json.dumps(_GOLD_STAR_ICON_URL))
    html = html.replace("__MARKER_SIZE__", str(_STAR_MARKER_SIZE_PX))
    html = html.replace("__API_KEY__", quote(api_key, safe=""))
    return html


@tool(
    description=(
        "Render an interactive map with one or more real places highlighted "
        "as gold star pins, as an A2UI surface -- tapping a star shows that "
        "place's name. Every place's `lat`/`lng` must come from a prior "
        "`geocode_place` call on that place -- never invent coordinates. "
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
    places_payload = json.dumps([{"label": p.label, "lat": p.lat, "lng": p.lng} for p in parsed_places])
    embed_url = f"{base_url.rstrip('/')}{MAP_EMBED_ROUTE_PATH}?data={quote(places_payload, safe='')}"
    summary = "\n".join(f"- {p.label}" for p in parsed_places)
    return {
        "text": f"{title}\n{summary}",
        "genui": genui.map_card(surface_id, title, embed_url, caption=summary),
    }
