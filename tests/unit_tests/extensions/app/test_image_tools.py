# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.image_tools."""

from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.extensions.app import image_tools


class TestFetchPageImage:
    @pytest.mark.asyncio
    async def test_returns_image_url_on_first_success(self):
        with patch.object(image_tools, "_fetch_page_once", AsyncMock(return_value="https://example.com/img.jpg")):
            result = await image_tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result == {"url": "https://example.com", "image_url": "https://example.com/img.jpg"}

    @pytest.mark.asyncio
    async def test_returns_none_image_without_retry_when_page_has_no_image(self):
        mock_fetch = AsyncMock(return_value=None)
        with patch.object(image_tools, "_fetch_page_once", mock_fetch):
            result = await image_tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] is None
        mock_fetch.assert_awaited_once()  # a clean "no image" result is not retried

    @pytest.mark.asyncio
    async def test_retries_transient_failures_up_to_the_cap(self):
        mock_fetch = AsyncMock(
            side_effect=[
                image_tools._RetryableFetchError("timeout"),
                image_tools._RetryableFetchError("timeout"),
                "https://example.com/img.jpg",
            ]
        )
        with patch.object(image_tools, "_fetch_page_once", mock_fetch), patch("asyncio.sleep", AsyncMock()):
            result = await image_tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] == "https://example.com/img.jpg"
        assert mock_fetch.await_count == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts_and_reports_error(self):
        mock_fetch = AsyncMock(side_effect=image_tools._RetryableFetchError("still down"))
        with patch.object(image_tools, "_fetch_page_once", mock_fetch), patch("asyncio.sleep", AsyncMock()):
            result = await image_tools.fetch_page_image.invoke({"url": "https://example.com"})
        assert result["image_url"] is None
        assert "still down" in result["error"]
        assert mock_fetch.await_count == image_tools.MAX_IMAGE_FETCH_ATTEMPTS
