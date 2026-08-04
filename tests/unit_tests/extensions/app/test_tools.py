# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.tools."""

from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.extensions.app import tools


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


class TestFetchPageImage:
    @pytest.mark.asyncio
    async def test_returns_image_url_on_first_success(self):
        with patch.object(tools, "_fetch_page_once", AsyncMock(return_value="https://example.com/img.jpg")):
            result = await tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result == {"url": "https://example.com", "image_url": "https://example.com/img.jpg"}

    @pytest.mark.asyncio
    async def test_returns_none_image_without_retry_when_page_has_no_image(self):
        mock_fetch = AsyncMock(return_value=None)
        with patch.object(tools, "_fetch_page_once", mock_fetch):
            result = await tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] is None
        mock_fetch.assert_awaited_once()  # a clean "no image" result is not retried

    @pytest.mark.asyncio
    async def test_retries_transient_failures_up_to_the_cap(self):
        mock_fetch = AsyncMock(
            side_effect=[
                tools._RetryableFetchError("timeout"),
                tools._RetryableFetchError("timeout"),
                "https://example.com/img.jpg",
            ]
        )
        with patch.object(tools, "_fetch_page_once", mock_fetch), patch("asyncio.sleep", AsyncMock()):
            result = await tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] == "https://example.com/img.jpg"
        assert mock_fetch.await_count == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts_and_reports_error(self):
        mock_fetch = AsyncMock(side_effect=tools._RetryableFetchError("still down"))
        with patch.object(tools, "_fetch_page_once", mock_fetch), patch("asyncio.sleep", AsyncMock()):
            result = await tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] is None
        assert "still down" in result["error"]
        assert mock_fetch.await_count == tools.MAX_IMAGE_FETCH_ATTEMPTS


class TestAllTools:
    def test_all_tools_exposes_expected_names(self):
        names = {t.card.name for t in tools.ALL_TOOLS}
        assert names == {
            "get_current_time",
            "show_card",
            "show_info_list",
            "ask_preferences_form",
            "fetch_page_image",
        }
