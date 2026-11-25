# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""REST API of FcSessionManager.

Every request needs ``Authorization: Bearer <FCM_API_TOKEN>`` (the agent-core
service credential) and, for session routes, ``X-Owner-User-Id`` naming the
end user the call is made for. Ownership is checked on every session call.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from aiohttp import web

from fc_session_manager.sessions import SessionError, SessionManager

logger = logging.getLogger(__name__)
OWNER_HEADER = "X-Owner-User-Id"
_SID = "{session_id:[^/:]+}"


def _owner(request: web.Request) -> str:
    owner = request.headers.get(OWNER_HEADER, "").strip()
    if not owner:
        raise web.HTTPBadRequest(text=f"missing {OWNER_HEADER} header")
    return owner


def build_app(manager: SessionManager) -> web.Application:
    token = manager.settings.api_token

    @web.middleware
    async def auth_and_errors(request: web.Request,
                              handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.path != "/healthz":
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except SessionError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)
        except web.HTTPException:
            raise
        except Exception as exc:  # report, never leak a traceback to clients
            logger.exception("request %s %s failed", request.method, request.path)
            return web.json_response({"error": f"internal error: {type(exc).__name__}"}, status=500)

    async def healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def create(request: web.Request) -> web.Response:
        body: dict[str, Any] = await request.json()
        owner = _owner(request)
        if body.get("owner_user_id") not in (None, owner):
            return web.json_response({"error": "owner_user_id does not match the caller"}, status=403)
        info, created = await manager.create(str(body.get("session_id", "")), owner, body.get("profile"))
        return web.json_response(info, status=201 if created else 200)

    async def list_sessions(request: web.Request) -> web.Response:
        owner = _owner(request)
        if request.query.get("owner") not in (None, owner):
            return web.json_response({"error": "can only list your own sessions"}, status=403)
        return web.json_response({"sessions": await manager.list(owner)})

    async def get(request: web.Request) -> web.Response:
        return web.json_response(await manager.get(request.match_info["session_id"], _owner(request)))

    async def pause(request: web.Request) -> web.Response:
        return web.json_response(await manager.pause(request.match_info["session_id"], _owner(request)))

    async def resume(request: web.Request) -> web.Response:
        return web.json_response(await manager.resume(request.match_info["session_id"], _owner(request)))

    async def delete(request: web.Request) -> web.Response:
        await manager.destroy(request.match_info["session_id"], _owner(request))
        return web.json_response({"deleted": True})

    async def debug(request: web.Request) -> web.Response:
        return web.json_response(await manager.debug(request.match_info["session_id"], _owner(request)))

    app = web.Application(middlewares=[auth_and_errors])
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/v1/sessions", create)
    app.router.add_get("/v1/sessions", list_sessions)
    app.router.add_get(f"/v1/sessions/{_SID}", get)
    app.router.add_post(f"/v1/sessions/{_SID}:pause", pause)
    app.router.add_post(f"/v1/sessions/{_SID}:resume", resume)
    app.router.add_delete(f"/v1/sessions/{_SID}", delete)
    app.router.add_get(f"/v1/sessions/{_SID}/debug", debug)

    async def on_startup(_: web.Application) -> None:
        await manager.startup()

    async def on_cleanup(_: web.Application) -> None:
        await manager.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
