# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.map_tools."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.extensions.app import map_tools


def _config_get(values):
    return lambda key, default=None: values.get(key, default)


class TestGeocodePlace:
    @pytest.mark.asyncio
    async def test_returns_error_when_api_key_not_configured(self):
        with patch.object(map_tools.config, "get", side_effect=_config_get({})):
            result = await map_tools.geocode_place.invoke({"query": "Grand Palace, Bangkok"})
        assert result["lat"] is None
        assert "GOOGLE_MAPS_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_coordinates_on_success(self):
        body = json.dumps(
            {
                "status": "OK",
                "results": [
                    {
                        "formatted_address": "Na Phra Lan Rd, Bangkok, Thailand",
                        "geometry": {"location": {"lat": 13.75, "lng": 100.4913}},
                    }
                ],
            }
        ).encode("utf-8")
        mock_request = AsyncMock(return_value=(200, {"Content-Type": "application/json"}, body, "url", False))
        with (
            patch.object(map_tools.config, "get", side_effect=_config_get({"GOOGLE_MAPS_API_KEY": "test-key"})),
            patch.object(map_tools._http, "request", mock_request),
        ):
            result = await map_tools.geocode_place.invoke({"query": "Grand Palace, Bangkok"})
        assert result == {
            "query": "Grand Palace, Bangkok",
            "lat": 13.75,
            "lng": 100.4913,
            "formatted_address": "Na Phra Lan Rd, Bangkok, Thailand",
        }

    @pytest.mark.asyncio
    async def test_returns_error_when_status_not_ok(self):
        body = json.dumps({"status": "ZERO_RESULTS", "results": []}).encode("utf-8")
        mock_request = AsyncMock(return_value=(200, {"Content-Type": "application/json"}, body, "url", False))
        with (
            patch.object(map_tools.config, "get", side_effect=_config_get({"GOOGLE_MAPS_API_KEY": "test-key"})),
            patch.object(map_tools._http, "request", mock_request),
        ):
            result = await map_tools.geocode_place.invoke({"query": "somewhere that doesn't exist"})
        assert result["lat"] is None
        assert "ZERO_RESULTS" in result["error"]

    @pytest.mark.asyncio
    async def test_http_error_returns_error_field(self):
        mock_request = AsyncMock(return_value=(403, {}, b"", "url", False))
        with (
            patch.object(map_tools.config, "get", side_effect=_config_get({"GOOGLE_MAPS_API_KEY": "test-key"})),
            patch.object(map_tools._http, "request", mock_request),
        ):
            result = await map_tools.geocode_place.invoke({"query": "Grand Palace, Bangkok"})
        assert result["lat"] is None
        assert "403" in result["error"]


class TestRenderMapEmbedHtml:
    def test_embeds_places_as_json(self):
        places = [
            map_tools.MapPlace(label="Grand Palace", lat=13.75, lng=100.4913),
            map_tools.MapPlace(label="Wat Arun", lat=13.7437, lng=100.4888),
        ]
        html = map_tools.render_map_embed_html(places, "test-key")
        assert '"label": "Grand Palace"' in html
        assert '"lat": 13.75' in html
        assert '"label": "Wat Arun"' in html

    def test_embeds_api_key_in_script_src(self):
        places = [map_tools.MapPlace(label="Grand Palace", lat=13.75, lng=100.4913)]
        html = map_tools.render_map_embed_html(places, "test-key")
        assert "key=test-key" in html

    def test_embeds_gold_star_icon_url(self):
        places = [map_tools.MapPlace(label="Grand Palace", lat=13.75, lng=100.4913)]
        html = map_tools.render_map_embed_html(places, "test-key")
        assert map_tools._GOLD_STAR_ICON_URL in html

    def test_escapes_closing_script_tag_in_label(self):
        # A place label containing "</script>" must not be able to break out
        # of the page's own <script> block.
        places = [map_tools.MapPlace(label="</script><script>alert(1)</script>", lat=13.75, lng=100.4913)]
        html = map_tools.render_map_embed_html(places, "test-key")
        assert "</script><script>alert(1)</script>" not in html

    def test_uses_text_content_not_inner_html_for_label(self):
        # Marker click handler must build the info window content via
        # textContent, never innerHTML, so a place label can never be
        # interpreted as markup.
        places = [map_tools.MapPlace(label="Grand Palace", lat=13.75, lng=100.4913)]
        html = map_tools.render_map_embed_html(places, "test-key")
        assert "textContent" in html
        assert ".innerHTML" not in html


class TestShowMap:
    @pytest.mark.asyncio
    async def test_returns_no_genui_when_api_key_not_configured(self):
        with patch.object(map_tools.config, "get", side_effect=_config_get({"PUBLIC_BASE_URL": "https://example.com"})):
            result = await map_tools.show_map.invoke(
                {"title": "Bangkok", "places": [{"label": "Grand Palace", "lat": 13.75, "lng": 100.4913}]}
            )
        assert result["genui"] == []

    @pytest.mark.asyncio
    async def test_returns_no_genui_when_public_base_url_not_configured(self):
        with patch.object(map_tools.config, "get", side_effect=_config_get({"GOOGLE_MAPS_API_KEY": "test-key"})):
            result = await map_tools.show_map.invoke(
                {"title": "Bangkok", "places": [{"label": "Grand Palace", "lat": 13.75, "lng": 100.4913}]}
            )
        assert result["genui"] == []

    @pytest.mark.asyncio
    async def test_returns_no_genui_for_empty_places(self):
        config_values = {"GOOGLE_MAPS_API_KEY": "test-key", "PUBLIC_BASE_URL": "https://example.com"}
        with patch.object(map_tools.config, "get", side_effect=_config_get(config_values)):
            result = await map_tools.show_map.invoke({"title": "Bangkok", "places": []})
        assert result["genui"] == []

    @pytest.mark.asyncio
    async def test_renders_map_card_with_embed_url(self):
        config_values = {"GOOGLE_MAPS_API_KEY": "test-key", "PUBLIC_BASE_URL": "https://example.com:8090"}
        with patch.object(map_tools.config, "get", side_effect=_config_get(config_values)):
            result = await map_tools.show_map.invoke(
                {
                    "title": "Top places in Bangkok",
                    "places": [
                        {"label": "Grand Palace", "lat": 13.75, "lng": 100.4913},
                        {"label": "Wat Arun", "lat": 13.7437, "lng": 100.4888},
                    ],
                }
            )
        assert "Grand Palace" in result["text"]
        assert "Wat Arun" in result["text"]
        components = {c["id"]: c for c in result["genui"][1]["updateComponents"]["components"]}
        assert components["map"]["component"] == "MapWeb"
        assert components["map"]["url"].startswith(f"https://example.com:8090{map_tools.MAP_EMBED_ROUTE_PATH}?data=")

    @pytest.mark.asyncio
    async def test_embed_url_strips_trailing_slash_on_base_url(self):
        config_values = {"GOOGLE_MAPS_API_KEY": "test-key", "PUBLIC_BASE_URL": "https://example.com:8090/"}
        with patch.object(map_tools.config, "get", side_effect=_config_get(config_values)):
            result = await map_tools.show_map.invoke(
                {"title": "Bangkok", "places": [{"label": "Grand Palace", "lat": 13.75, "lng": 100.4913}]}
            )
        components = {c["id"]: c for c in result["genui"][1]["updateComponents"]["components"]}
        assert f"https://example.com:8090{map_tools.MAP_EMBED_ROUTE_PATH}?data=" in components["map"]["url"]
        assert "//map-embed" not in components["map"]["url"]
