# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Entry point: ``python3 -m fc_session_manager``."""

from __future__ import annotations

import logging

from aiohttp import web

from fc_session_manager.api import build_app
from fc_session_manager.config import Settings
from fc_session_manager.sessions import SessionManager


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.jailer_base.mkdir(parents=True, exist_ok=True)
    manager = SessionManager(settings)
    web.run_app(build_app(manager), host=settings.listen_host, port=settings.listen_port, access_log=None)


if __name__ == "__main__":
    main()
