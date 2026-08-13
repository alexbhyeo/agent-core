# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.uiux_tools.

Image/video-specific tools have their own test modules (test_image_tools.py,
test_video_tools.py) alongside their own source modules (image_tools.py,
video_tools.py) -- this file covers only the tools that stay directly in
uiux_tools.py.
"""

import pytest

from openjiuwen.extensions.app import uiux_tools as tools


class TestGetCurrentTime:
    @pytest.mark.asyncio
    async def test_returns_utc_formatted_string(self):
        result = await tools.get_current_time.invoke({})
        assert result.endswith("UTC")
        assert len(result) == len("2025-01-01 00:00 UTC")


class TestShowCard:
    @pytest.mark.asyncio
    async def test_returns_text_and_genui_messages(self):
        result = await tools.show_card.invoke({"title": "Title", "body": "Body"})
        assert result["text"] == "Body"
        assert len(result["genui"]) == 2
        assert "createSurface" in result["genui"][0]
        assert "updateComponents" in result["genui"][1]

    @pytest.mark.asyncio
    async def test_link_url_adds_open_url_button(self):
        result = await tools.show_card.invoke(
            {"title": "Title", "body": "Body", "link_url": "https://example.com/book", "link_label": "Continue"}
        )
        components = {c["id"]: c for c in result["genui"][1]["updateComponents"]["components"]}
        assert components["linkButton"]["action"]["functionCall"]["args"]["url"] == "https://example.com/book"


class TestShowInfoList:
    @pytest.mark.asyncio
    async def test_returns_text_and_genui_messages(self):
        result = await tools.show_info_list.invoke(
            {"title": "Steps", "items": [{"title": "First"}, {"title": "Second", "subtitle": "details"}]}
        )
        assert "First" in result["text"]
        assert "Second" in result["text"]
        assert len(result["genui"]) == 2


class TestAskPreferencesForm:
    @pytest.mark.asyncio
    async def test_returns_form_with_requested_fields(self):
        result = await tools.ask_preferences_form.invoke(
            {
                "title": "Racket preferences",
                "fields": [
                    {"id": "level", "type": "choice", "label": "Level", "options": [{"label": "Pro", "value": "pro"}]},
                    {"id": "budget", "type": "slider", "label": "Budget", "min_value": 30, "max_value": 500},
                ],
            }
        )
        assert "genui" in result
        component_ids = {c["id"] for c in result["genui"][-1]["updateComponents"]["components"]}
        assert {"level", "budget"} <= component_ids

    @pytest.mark.asyncio
    async def test_hotel_title_auto_inserts_stay_date_fields(self):
        result = await tools.ask_preferences_form.invoke(
            {
                "title": "Hotel booking preferences",
                "fields": [{"id": "budget", "type": "slider", "label": "Budget", "min_value": 0, "max_value": 1000}],
            }
        )
        component_ids = {c["id"] for c in result["genui"][-1]["updateComponents"]["components"]}
        assert {"check_in", "check_out"} <= component_ids


class TestAllTools:
    def test_all_tools_exposes_expected_names(self):
        names = {t.card.name for t in tools.ALL_TOOLS}
        assert names == {
            "get_current_time",
            "show_card",
            "show_info_list",
            "ask_preferences_form",
            "fetch_page_image",
            "search_images",
            "search_youtube_videos",
            "fetch_video_source",
            "show_video_clips",
        }
