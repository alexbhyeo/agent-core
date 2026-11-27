# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Entry point: ``python3 -m fc_playground``."""

from __future__ import annotations

import logging

from aiohttp import web

from fc_playground.config import Settings
from fc_playground.server import build_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    web.run_app(build_app(settings), host=settings.host, port=settings.port, access_log=None)


if __name__ == "__main__":
    main()
