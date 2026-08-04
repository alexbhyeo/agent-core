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

# openjiuwen.harness.tools.web.WebFreeSearchTool reads this env var directly at
# call time (it's a harness-wide flag, not one of this app's own _DEFAULTS).
# DuckDuckGo scraping needs no API key, so default it on for this extension
# unless the environment already says otherwise.
os.environ.setdefault("FREE_SEARCH_DDG_ENABLED", "true")

_CERTS_DIR = Path(__file__).resolve().parent / "certs"

_DEFAULTS: dict[str, Any] = {
    "API_KEY": os.getenv("API_KEY", ""),
    "API_BASE": os.getenv("API_BASE", "https://api.deepseek.com/v1"),
    "MODEL_NAME": os.getenv("MODEL_NAME", "deepseek-v4-flash"),
    "MODEL_PROVIDER": os.getenv("MODEL_PROVIDER", "DeepSeek"),
    "LLM_TEMPERATURE": float(os.getenv("LLM_TEMPERATURE", "1.0")),
    "LLM_SEED": int(os.getenv("LLM_SEED", "42")),
    "LLM_SSL_VERIFY": os.getenv("LLM_SSL_VERIFY", "false").lower() == "true",
    "HOST": os.getenv("A2UI_AGENT_HOST", "0.0.0.0"),
    "PORT": int(os.getenv("A2UI_AGENT_PORT", "8090")),
    "CATALOG_ID": os.getenv("A2UI_CATALOG_ID", "https://a2ui.org/specification/v0_9/basic_catalog.json"),
    # TLS for the client<->agent WebSocket, so traffic (including the API key
    # never touching this hop, but chat content and tool output) isn't sent
    # in cleartext. The private key stays on the server (certs/server.key,
    # gitignored); only the certificate (public key) is ever distributed to
    # clients, which pin it instead of relying on a CA -- see the Flutter
    # app's WsService for the client side of this. Regenerate both with
    # certs/generate.sh. Falls back to plain ws:// if either file is absent
    # so a fresh checkout without generated certs still runs for local dev.
    "SSL_CERTFILE": os.getenv("A2UI_SSL_CERTFILE", str(_CERTS_DIR / "server.crt")),
    "SSL_KEYFILE": os.getenv("A2UI_SSL_KEYFILE", str(_CERTS_DIR / "server.key")),
}

_values: dict[str, Any] = dict(_DEFAULTS)


def get(key: str, default: Any = None) -> Any:
    return _values.get(key, default)


def set_value(key: str, value: Any) -> None:
    _values[key] = value
