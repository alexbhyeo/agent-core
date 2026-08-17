# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Hotel search tools for the ReAct agent: real hotel availability/pricing
via SerpApi's Google Hotels engine, rendered as a gallery of hotel cards.

Required search parameters (check-in/check-out dates, guest counts) are
never guessed -- the booking policy in ``agent.py``'s system prompt has the
agent collect them from the user first via `ask_preferences_form` (which
already auto-inserts `check_in`/`check_out` date fields for hotel-titled
forms), then pass them straight into `search_hotels`.

If `search_hotels`/`show_hotel_results` are unavailable (no API key
configured) or return no hotels, the system prompt's booking policy falls
back to `free_search`/`browser_inspect_page` to find and inspect a real
hotel site instead -- see ``agent.py``.
"""

import json
from typing import Any, Optional
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.tool import tool
from openjiuwen.harness.tools.web import _http
from openjiuwen.harness.tools.web._common import _REQUEST_HEADERS
from openjiuwen.harness.tools.web._decode import _decode_response_text

from .. import config, genui

_HOTELS_SEARCH_ENDPOINT = "https://serpapi.com/search.json"
_HOTELS_ENGINE = "google_hotels"
MAX_HOTEL_RESULTS = 10


@tool(
    description=(
        "Search for real, currently-bookable hotels via SerpApi's Google "
        "Hotels engine (not guessed from memory) -- use this whenever the "
        "user wants to book/find a hotel or other lodging. `location` "
        "should be a specific place (e.g. 'Bali', 'Shibuya, Tokyo', not just "
        "'somewhere nice'). `check_in_date`/`check_out_date` must be real "
        "dates in YYYY-MM-DD form -- never invent these; collect them from "
        "the user first via `ask_preferences_form` (title it so it includes "
        "the word 'hotel' and it will auto-add check-in/check-out date "
        "fields) rather than guessing. Returns up to 10 real hotels, each "
        "with `name`, `price_per_night`, `rating`, `reviews`, `hotel_class`, "
        "`image_url`, `link` (the hotel's real page), and `description` -- "
        "any of these besides `name` can be missing for a given hotel, "
        "which is normal; only pass fields that are actually present into "
        "`show_hotel_results`, never invent a replacement. An `error` (no "
        "API key configured, or no hotels found) means don't fabricate a "
        "hotel -- fall back to `free_search`/`browser_inspect_page` to find "
        "a real hotel site instead, per the booking policy."
    )
)
async def search_hotels(
    location: str,
    check_in_date: str,
    check_out_date: str,
    adults: int = 2,
    children: int = 0,
    currency: str = "USD",
) -> dict[str, Any]:
    api_key = config.get("SERPAPI_API_KEY")
    if not api_key:
        return {"location": location, "hotels": [], "error": "SERPAPI_API_KEY is not configured on the server."}

    params: dict[str, Any] = {
        "engine": _HOTELS_ENGINE,
        "q": location,
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "adults": max(1, adults),
        "children": max(0, children),
        "currency": currency,
        "api_key": api_key,
    }
    gl = config.get("SERPAPI_GL")
    if gl:
        params["gl"] = gl
    hl = config.get("SERPAPI_HL")
    if hl:
        params["hl"] = hl

    url = _HOTELS_SEARCH_ENDPOINT + "?" + urlencode(params)
    try:
        async with _http.new_session() as session:
            status, headers, body, _final_url, _truncated = await _http.request(
                session, "GET", url, headers=_REQUEST_HEADERS, timeout_seconds=20, max_bytes=3_000_000
            )
    except Exception as exc:  # noqa: BLE001 -- report the failure, don't crash the tool call
        return {"location": location, "hotels": [], "error": str(exc)}

    text = _decode_response_text(body, content_type=headers.get("Content-Type", ""))
    if status >= 400:
        return {"location": location, "hotels": [], "error": f"SerpApi returned HTTP {status}: {text[:300]}"}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"location": location, "hotels": [], "error": f"Could not parse SerpApi response: {exc}"}

    if data.get("search_metadata", {}).get("status") == "Error":
        error_message = data.get("error") or data.get("search_metadata", {}).get("error") or "unknown error"
        return {"location": location, "hotels": [], "error": f"SerpApi Google Hotels search failed: {error_message}"}

    properties = data.get("properties") or []
    if not properties:
        return {"location": location, "hotels": [], "error": "No hotels found for this search."}

    hotels: list[dict[str, Any]] = []
    for prop in properties[:MAX_HOTEL_RESULTS]:
        name = prop.get("name")
        if not name:
            continue
        images = prop.get("images") or []
        rate = prop.get("rate_per_night") or {}
        hotels.append(
            {
                "name": name,
                "price_per_night": rate.get("lowest"),
                "rating": prop.get("overall_rating"),
                "reviews": prop.get("reviews"),
                "hotel_class": prop.get("hotel_class"),
                "image_url": images[0].get("thumbnail") if images else None,
                "link": prop.get("link"),
                "description": prop.get("description"),
            }
        )

    return {
        "location": location,
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "hotels": hotels,
    }


class HotelResult(BaseModel):
    name: str = Field(description="The hotel's real name, from a prior `search_hotels` call.")
    price_per_night: Optional[str] = Field(
        default=None, description="Real formatted price (e.g. '$548') from `search_hotels`. Omit if it returned null."
    )
    rating: Optional[float] = Field(
        default=None, description="Real rating (e.g. 4.6) from `search_hotels`. Omit if it returned null."
    )
    reviews: Optional[int] = Field(
        default=None, description="Real review count from `search_hotels`, shown alongside `rating` if given."
    )
    hotel_class: Optional[str] = Field(
        default=None, description="Real star-class string (e.g. '5-star hotel') from `search_hotels`."
    )
    image_url: Optional[str] = Field(default=None, description="Real photo URL from `search_hotels`.")
    link: Optional[str] = Field(
        default=None, description="Real URL to the hotel's own page, from `search_hotels` -- the 'View Hotel' button target."
    )
    description: Optional[str] = Field(default=None, description="Real short description from `search_hotels`.")


@tool(
    description=(
        "Render a gallery of real hotel results as an A2UI surface, each in "
        "its own card with a photo, price/rating/class, a short description, "
        "and a 'View Hotel' button that opens the hotel's real page "
        "externally for the user to finish booking there themselves -- this "
        "agent never completes a booking on the user's behalf. Every hotel "
        "must come from a prior `search_hotels` call -- never invent a "
        "hotel, price, rating, or link. Pass through exactly what "
        "`search_hotels` returned, including missing/null fields (just "
        "leave those out of the hotel, don't invent a replacement value)."
    )
)
def show_hotel_results(title: str, hotels: list[HotelResult]) -> dict[str, Any]:
    parsed_hotels = [h if isinstance(h, HotelResult) else HotelResult(**h) for h in hotels]
    if not parsed_hotels:
        return {"text": "I couldn't find any hotels for that search.", "genui": []}

    surface_id = genui.new_surface_id("hotels")
    summary_lines = []
    for h in parsed_hotels:
        line = f"- {h.name}"
        if h.price_per_night:
            line += f" ({h.price_per_night}/night)"
        summary_lines.append(line)
    summary = "\n".join(summary_lines)
    hotel_dicts = [h.model_dump(exclude_none=True) for h in parsed_hotels]
    return {
        "text": f"{title}\n{summary}",
        "genui": genui.hotel_gallery_card(surface_id, title, hotel_dicts),
    }
