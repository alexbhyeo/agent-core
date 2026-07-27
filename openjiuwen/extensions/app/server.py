# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""A2UI ReAct agent server: a single openJiuwen ReActAgent exposed over one
WebSocket endpoint, streaming A2UI (GenUI) JSON to any client that speaks
the envelope protocol -- in particular the Flutter ``a2ui_mobile_app``.

Run:
    python -m openjiuwen.extensions.app.server
    # or
    uvicorn openjiuwen.extensions.app.server:create_app --factory --host 0.0.0.0 --port 8090

Then point the Flutter app at it, e.g.:
    flutter run --dart-define=A2UI_WS_URL=ws://10.0.2.2:8090/ws
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from openjiuwen.core.runner import Runner
from openjiuwen.extensions.app import config as app_config
from openjiuwen.extensions.app.agent import build_agent
from openjiuwen.extensions.app.ws_session import ConnectionSession


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await Runner.start()
        app.state.agent = await build_agent()
        yield
        await Runner.stop()

    app = FastAPI(
        title="A2UI ReAct Agent",
        description=(
            "Minimal single-ReActAgent WebSocket server built on openJiuwen, speaking the A2UI/GenUI envelope protocol."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, token: str = "") -> None:
        # Dev-mode: no real auth, matching the Flutter client which connects
        # with no token today. Swap in real auth before shipping this.
        user_id = token or "dev-user"
        session = ConnectionSession(websocket, app.state.agent, user_id)
        await session.run()

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app(),
        host=app_config.get("HOST"),
        port=app_config.get("PORT"),
    )
