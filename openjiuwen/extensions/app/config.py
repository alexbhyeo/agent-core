# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Environment-driven configuration for the A2UI ReAct agent server."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Auto-load a local .env next to this file (never committed -- see .gitignore)
# so `python server.py` works without requiring `uv run --env-file`. Real
# environment variables already set take precedence (override=False default).
load_dotenv(Path(__file__).resolve().parent / ".env")

_DEFAULTS: dict[str, Any] = {
    "API_KEY": os.getenv("API_KEY", ""),
    "API_BASE": os.getenv("API_BASE", "https://api.deepseek.com/v1"),
    "MODEL_NAME": os.getenv("MODEL_NAME", "deepseek-v4-flash"),
    "MODEL_PROVIDER": os.getenv("MODEL_PROVIDER", "DeepSeek"),
    "LLM_SSL_VERIFY": os.getenv("LLM_SSL_VERIFY", "false").lower() == "true",
    "HOST": os.getenv("A2UI_AGENT_HOST", "0.0.0.0"),
    "PORT": int(os.getenv("A2UI_AGENT_PORT", "8090")),
    "CATALOG_ID": os.getenv("A2UI_CATALOG_ID", "https://a2ui.org/specification/v0_9/basic_catalog.json"),
}

_values: dict[str, Any] = dict(_DEFAULTS)


def get(key: str, default: Any = None) -> Any:
    return _values.get(key, default)


def set_value(key: str, value: Any) -> None:
    _values[key] = value
