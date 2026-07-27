# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Unit tests for openjiuwen.extensions.app.config."""

from openjiuwen.extensions.app import config


class TestConfig:
    def test_get_returns_default_for_unknown_key(self):
        assert config.get("NOT_A_REAL_KEY") is None
        assert config.get("NOT_A_REAL_KEY", "fallback") == "fallback"

    def test_get_returns_known_defaults(self):
        assert config.get("MODEL_PROVIDER") is not None
        assert config.get("PORT") == int(config.get("PORT"))

    def test_set_value_then_get_round_trips(self):
        config.set_value("API_KEY", "mock-api-key-for-tests")
        try:
            assert config.get("API_KEY") == "mock-api-key-for-tests"
        finally:
            config.set_value("API_KEY", "")
