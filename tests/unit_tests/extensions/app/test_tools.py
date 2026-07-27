# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.tools."""

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


class TestAskBadmintonPreferences:
    @pytest.mark.asyncio
    async def test_returns_form_with_expected_fields(self):
        result = await tools.ask_badminton_preferences.invoke({})
        assert "genui" in result
        update = result["genui"][1]["updateComponents"]
        component_ids = {c["id"] for c in update["components"]}
        assert {"level", "budget", "brand", "junior", "submit"} <= component_ids


class TestAllTools:
    def test_all_tools_exposes_expected_names(self):
        names = {t.card.name for t in tools.ALL_TOOLS}
        assert names == {"get_current_time", "show_card", "ask_badminton_preferences"}
