# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP API of the Playground backend (``/api/v1``) for a separately hosted frontend.

Every ``/api`` request needs ``Authorization: Bearer <PLAYGROUND_TOKEN>`` and
``X-Playground-User``; cross-origin access is limited to ``PLAYGROUND_ALLOWED_ORIGINS``.
User switching is not authenticated: this is a test tool.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from fc_playground.config import Settings
from fc_playground.events import EventHub
from fc_playground.runs import Busy, RunManager, Saturated
from fc_playground.store import PlaygroundStore
from fc_playground.vm import (
    ManagerClient,
    gateway_config,
    ManagerError,
    MessageFollower,
    browse,
    restart_manager,
    run_shell,
    scrub,
)

from openjiuwen.core.runner import Runner
from openjiuwen.extensions.sys_operation.sandbox.launchers import register_session_mcp_server

logger = logging.getLogger(__name__)

API_VERSION = 1
_USER_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_KINDS = ("deepagent", "team")
_CORS_HEADERS = "Authorization, X-Playground-User, Content-Type, Last-Event-ID"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda v: json.dumps(v, default=str))


class Playground:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = PlaygroundStore(settings.data_dir / "playground.db")
        self.hub = EventHub()
        self.manager = ManagerClient(settings)
        self.runs = RunManager(settings, self.store, self.hub)
        self.follower = MessageFollower(self.manager, self.hub,
                                        lambda user: [s["session_id"] for s in self.store.list_sessions(user)])
        self.hub.on_first_subscriber = self.follower.follow_user
        self.hub.on_last_unsubscribe = self.follower.unfollow_user
        self._last_alerts: Optional[str] = None
        self._alert_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------- helpers

    def _owned(self, user: str, session_id: str) -> dict[str, Any]:
        meta = self.store.get_session(session_id)
        if meta is None:
            raise ApiError(404, f"session {session_id} not found")
        if meta["owner"] != user:
            raise ApiError(403, f"session {session_id} belongs to another user")
        return meta

    async def _vm(self, user: str, session_id: str) -> Optional[dict[str, Any]]:
        try:
            return scrub(await self.manager.call("GET", f"/v1/sessions/{session_id}", user, timeout=15))
        except ManagerError as exc:
            if exc.status == 404:
                return {"state": "GONE"}
            if exc.status == 403:
                raise
            return None

    async def describe(self, user: str, meta: dict[str, Any], vm: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {**meta, "vm": vm if vm is not None else await self._vm(user, meta["session_id"]),
                "running_run_id": self.runs.running(meta["session_id"])}

    async def push_session(self, user: str, session_id: str) -> None:
        meta = self.store.get_session(session_id)
        if meta is not None:
            self.hub.publish(user, "session.updated", session_id, session=await self.describe(user, meta))

    async def _create_vm(self, user: str, session_id: str, profile: Optional[dict[str, Any]]) -> dict[str, Any]:
        body = {"session_id": session_id, "profile": profile or dict(self.settings.default_profile)}
        return scrub(await self.manager.call("POST", "/v1/sessions", user, body))

    # ------------------------------------------------------------- endpoints

    async def me(self, user: str, _: web.Request) -> web.Response:
        users = sorted(set(self.settings.known_users) | set(self.store.owners()) | {user})
        return _json({"user": user, "users": users, "api_version": API_VERSION,
                      "features": {"restart_manager": self.settings.docker_socket is not None,
                                   "llm": bool(self._llm_ready())},
                      "max_runs": self.settings.max_runs})

    @staticmethod
    def _llm_ready() -> bool:
        import os

        return bool(os.getenv("API_KEY") and os.getenv("API_BASE") and os.getenv("MODEL_NAME"))

    async def list_sessions(self, user: str, _: web.Request) -> web.Response:
        try:
            vms = {s["session_id"]: scrub(s) for s in
                   (await self.manager.call("GET", "/v1/sessions", user, timeout=15)).get("sessions", [])}
            vm_known = True
        except ManagerError:
            vms, vm_known = {}, False
        out = []
        for meta in self.store.list_sessions(user):
            vm = vms.get(meta["session_id"]) if vm_known else None
            out.append(await self.describe(user, meta, vm if vm is not None else ({"state": "GONE"} if vm_known
                                                                                  else {"state": "UNKNOWN"})))
        known = {m["session_id"] for m in out}
        external = [{"session_id": sid, "owner": user, "kind": "external", "title": sid, "team_size": 0,
                     "created_ts": vm.get("created_ts"), "last_query_ts": None, "vm": vm, "running_run_id": None}
                    for sid, vm in vms.items() if sid not in known]
        return _json({"sessions": out, "external": external, "vm_known": vm_known})

    async def create_session(self, user: str, request: web.Request) -> web.Response:
        body = await _body(request)
        if body.get("adopt"):  # a VM of this user created outside the Playground
            sid = str(body["adopt"])
            if self.store.get_session(sid) is not None:
                raise ApiError(409, f"session {sid} is already in the playground")
            vm = scrub(await self.manager.call("GET", f"/v1/sessions/{sid}", user))
            meta = self.store.add_session(sid, user, "deepagent", f"Imported {sid}"[:120], 0)
            self.follower.follow(user, sid)
            session = await self.describe(user, meta, vm)
            self.hub.publish(user, "session.updated", sid, session=session)
            return _json(session, 201)
        kind = body.get("kind", "deepagent")
        if kind not in _KINDS:
            raise ApiError(400, f"kind must be one of {_KINDS}")
        team_size = max(1, min(int(body.get("team_size") or 3), 5)) if kind == "team" else 0
        session_id = f"pg-{uuid.uuid4().hex[:10]}"
        vm = await self._create_vm(user, session_id, body.get("profile"))
        title = str(body.get("title") or ("New team chat" if kind == "team" else "New chat"))[:120]
        meta = self.store.add_session(session_id, user, kind, title, team_size)
        self.follower.follow(user, session_id)
        session = await self.describe(user, meta, vm)
        self.hub.publish(user, "session.updated", session_id, session=session)
        return _json(session, 201)

    async def get_session(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        session = await self.describe(user, meta)
        session["transcript"] = self.store.transcript(meta["session_id"])
        try:
            page = await self.manager.call("GET", f"/v1/sessions/{meta['session_id']}/messages", user, timeout=15)
            session["vm_messages"] = scrub(page.get("messages", []))
        except ManagerError:
            session["vm_messages"] = []
        return _json(session)

    async def delete_session(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        sid = meta["session_id"]
        self.runs.forget(sid)
        self.follower.forget(user, sid)
        try:
            await self.manager.call("DELETE", f"/v1/sessions/{sid}", user)
        except ManagerError as exc:
            if exc.status != 404:
                raise
        self.store.delete_session(sid)
        self.hub.publish(user, "session.deleted", sid)
        return _json({"deleted": True})

    async def update_session(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        body = await _body(request)
        if "title" in body:
            self.store.update_session(meta["session_id"], title=str(body["title"])[:120] or meta["title"])
        await self.push_session(user, meta["session_id"])
        return _json(await self.describe(user, self.store.get_session(meta["session_id"])))

    async def query(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        text = str((await _body(request)).get("text", "")).strip()
        if not text:
            raise ApiError(400, "text must not be empty")
        return _json({"run_id": self._start(user, meta, text)}, 202)

    def _start(self, user: str, meta: dict[str, Any], text: str) -> str:
        if meta["title"] in ("New chat", "New team chat"):
            meta = {**meta, "title": text[:60]}
            self.store.update_session(meta["session_id"], title=meta["title"])
        try:
            run_id = self.runs.start(user, meta, text)
        except Busy as exc:
            raise ApiError(409, str(exc)) from exc
        except Saturated as exc:
            raise ApiError(429, str(exc)) from exc
        asyncio.create_task(self._watch_run(user, meta["session_id"]))
        return run_id

    async def _watch_run(self, user: str, session_id: str) -> None:
        """Publish session state while a run wakes / uses the VM, and once it ends."""
        await asyncio.sleep(0.5)
        await self.push_session(user, session_id)
        while self.runs.running(session_id):
            await asyncio.sleep(1)
        await self.push_session(user, session_id)

    async def batch_query(self, user: str, request: web.Request) -> web.Response:
        body = await _body(request)
        text = str(body.get("text", "")).strip()
        ids = body.get("session_ids") or []
        if not text or not isinstance(ids, list) or not ids:
            raise ApiError(400, "session_ids (list) and text are required")
        results = []
        for sid in ids:
            try:
                results.append({"session_id": sid, "run_id": self._start(user, self._owned(user, str(sid)), text)})
            except ApiError as exc:
                results.append({"session_id": sid, "error": exc.message, "status": exc.status})
        return _json({"runs": results}, 202)

    async def cancel(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        if not self.runs.cancel(meta["session_id"]):
            raise ApiError(404, "no running query in this session")
        return _json({"cancelled": True})

    async def vm_action(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        action = request.match_info["action"]
        if action not in ("pause", "persist", "resume"):
            raise ApiError(400, "action must be pause, persist or resume")
        body = await _body(request)
        payload = {k: body[k] for k in ("checkpoint_ref", "expected_checkpoint_ref") if k in body} or None
        started = time.monotonic()
        vm = scrub(await self.manager.call("POST", f"/v1/sessions/{meta['session_id']}:{action}", user, payload))
        session = await self.describe(user, meta, vm)
        self.hub.publish(user, "session.updated", meta["session_id"], session=session)
        return _json({**session, "took_s": round(time.monotonic() - started, 2)})

    async def fork(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        count = max(1, min(int((await _body(request)).get("count") or 1), 8))
        new_ids = [f"pg-{uuid.uuid4().hex[:10]}" for _ in range(count)]
        started = time.monotonic()
        page = await self.manager.call("POST", f"/v1/sessions/{meta['session_id']}:fork", user,
                                       {"new_session_ids": new_ids}, timeout=600)
        created = []
        for i, vm in enumerate(page.get("sessions", []), start=1):
            fmeta = self.store.add_session(vm["session_id"], user, meta["kind"], f"{meta['title']} (fork {i})",
                                           meta["team_size"])
            session = await self.describe(user, fmeta, scrub(vm))
            created.append(session)
            self.follower.follow(user, vm["session_id"])
            self.hub.publish(user, "session.updated", vm["session_id"], session=session)
        return _json({"sessions": created, "took_s": round(time.monotonic() - started, 2)}, 201)

    async def workspace(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        sid = meta["session_id"]
        if request.method == "GET":
            vm = await self._vm(user, sid) or {}
            return _json({"checkpoints": vm.get("workspace_checkpoints", []), "workspace_mib": vm.get("workspace_mib")})
        body = await _body(request)
        if body.get("checkpoint"):
            result = await self.manager.call("POST", f"/v1/sessions/{sid}/workspace/checkpoints", user,
                                             {"name": str(body["checkpoint"])})
        elif body.get("swap"):
            result = await self.manager.call("POST", f"/v1/sessions/{sid}/workspace:swap", user,
                                             {"checkpoint": str(body["swap"])})
        else:
            raise ApiError(400, "send {checkpoint: name} or {swap: name}")
        await self.push_session(user, sid)
        return _json(scrub(result))

    async def secrets(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        result = await self.manager.call("PUT", f"/v1/sessions/{meta['session_id']}/secrets", user,
                                         await _body(request))
        await self.push_session(user, meta["session_id"])
        return _json(scrub(result))

    async def mcp(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        body = await _body(request)
        name = str(body.get("name", ""))
        if not _NAME_RE.match(name):
            raise ApiError(400, "name must match [a-z0-9][a-z0-9_-]{0,63}")
        try:
            server = await register_session_mcp_server(
                gateway_config(self.settings, user).launcher_config, meta["session_id"], name,
                str(body.get("command", "")), [str(a) for a in body.get("args") or []],
                {str(k): str(v) for k, v in (body.get("env") or {}).items()})
        except Exception as exc:  # launcher errors carry the manager status
            raise ApiError(_status_of(exc), str(exc)) from exc
        return _json({"name": name, "resource_id": server.server_id, "client_type": server.client_type})

    async def shell(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        body = await _body(request)
        command = str(body.get("command", "")).strip()
        if not command:
            raise ApiError(400, "command must not be empty")
        started = time.monotonic()
        out = await run_shell(self.settings, user, meta["session_id"], command,
                              int(body.get("timeout") or 120))
        await self.push_session(user, meta["session_id"])
        return _json({**out, "took_s": round(time.monotonic() - started, 2)})

    async def files(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        return _json(await browse(self.settings, user, meta["session_id"], request.query.get("path", "/workspace")))

    async def messages(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        page = await self.manager.call("GET", f"/v1/sessions/{meta['session_id']}/messages", user)
        return _json(scrub(page))

    async def reply(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        text = (await _body(request)).get("text")
        if not isinstance(text, str):
            raise ApiError(400, "text must be a string")
        msg = await self.manager.call(
            "POST", f"/v1/sessions/{meta['session_id']}/messages/{int(request.match_info['mid'])}/reply", user,
            {"text": text})
        self.hub.publish(user, "vm.message", meta["session_id"], message=scrub(msg))
        await self.push_session(user, meta["session_id"])
        return _json(scrub(msg))

    async def egress(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        return _json(scrub(await self.manager.call("GET", f"/v1/sessions/{meta['session_id']}/egress?limit=200",
                                                   user)))

    async def debug(self, user: str, request: web.Request) -> web.Response:
        meta = self._owned(user, request.match_info["sid"])
        return _json(scrub(await self.manager.call("GET", f"/v1/sessions/{meta['session_id']}/debug", user)))

    async def ops(self, user: str, _: web.Request) -> web.Response:
        try:
            metrics_text = await self.manager.call("GET", "/metrics", timeout=15)
            alerts = (await self.manager.call("GET", "/v1/alerts", timeout=15)).get("alerts", [])
            manager_ok = True
        except ManagerError as exc:
            metrics_text, alerts, manager_ok = "", [{"name": "manager_unreachable", "severity": "critical",
                                                      "message": exc.message}], False
        metrics = {}
        for line in str(metrics_text).splitlines():
            name, _, value = line.rpartition(" ")
            if name and not line.startswith("#"):
                try:
                    metrics[name] = float(value)
                except ValueError:
                    pass
        per_user = []
        for owner in sorted(set(self.settings.known_users) | set(self.store.owners())):
            try:
                vms = (await self.manager.call("GET", "/v1/sessions", owner, timeout=15)).get("sessions", [])
            except ManagerError:
                vms = []
            states: dict[str, int] = {}
            for vm in vms:
                states[vm["state"]] = states.get(vm["state"], 0) + 1
            per_user.append({"user": owner, "sessions": len(vms), "states": states,
                             "tier": vms[0].get("tier") if vms else None})
        return _json({"manager_ok": manager_ok, "metrics": metrics, "alerts": alerts, "users": per_user,
                      "running_queries": self.runs.count(),
                      "features": {"restart_manager": self.settings.docker_socket is not None}})

    async def restart(self, user: str, _: web.Request) -> web.Response:
        started = time.monotonic()
        await restart_manager(self.settings)
        for _ in range(300):
            try:
                await self.manager.call("GET", "/healthz", timeout=5)
                break
            except ManagerError:
                await asyncio.sleep(1)
        return _json({"restarted": True, "took_s": round(time.monotonic() - started, 1)})

    # ------------------------------------------------------------------ events

    async def events(self, user: str, request: web.Request) -> web.StreamResponse:
        last = request.headers.get("Last-Event-ID") or request.query.get("last_event_id") or "0"
        queue, replay = self.hub.subscribe(user, int(last) if last.isdigit() else 0)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                               "X-Accel-Buffering": "no", **self.cors_headers(request)})
        await response.prepare(request)
        try:
            await response.write(b": connected\n\n")
            for event in replay:
                await _send_event(response, event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await response.write(b": keep-alive\n\n")
                    continue
                await _send_event(response, event)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(user, queue)
        return response

    async def watch_alerts(self) -> None:
        """Publish manager alert changes to every connected user."""
        while True:
            await asyncio.sleep(10)
            try:
                alerts = (await self.manager.call("GET", "/v1/alerts", timeout=10)).get("alerts", [])
            except ManagerError:
                alerts = [{"name": "manager_unreachable", "severity": "critical", "message": "manager unreachable"}]
            key = json.dumps(sorted(a["name"] for a in alerts))
            if key != self._last_alerts:
                self._last_alerts = key
                for owner in set(self.settings.known_users) | set(self.store.owners()):
                    if self.hub.has_subscribers(owner):
                        self.hub.publish(owner, "alert", None, alerts=alerts)

    # ------------------------------------------------------------------ CORS

    def cors_headers(self, request: web.Request) -> dict[str, str]:
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed = self.settings.allowed_origins
        if origin and ("*" in allowed or origin in allowed):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Headers": _CORS_HEADERS,
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Max-Age": "600"}
        return {}


async def _send_event(response: web.StreamResponse, event: dict[str, Any]) -> None:
    data = json.dumps(event, default=str)
    await response.write(f"id: {event['id']}\nevent: {event['type']}\ndata: {data}\n\n".encode())


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    return body


def _status_of(exc: Exception) -> int:
    details = getattr(exc, "details", None) or {}
    status = details.get("http_status") if isinstance(details, dict) else None
    return int(status) if status else 502


Handler = Callable[[str, web.Request], Awaitable[web.StreamResponse]]


def build_app(settings: Settings) -> web.Application:
    pg = Playground(settings)

    @web.middleware
    async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        cors = pg.cors_headers(request)
        if request.method == "OPTIONS":
            return web.Response(status=204 if cors else 403, headers=cors)
        try:
            response = await handler(request)
        except ApiError as exc:
            response = _json({"error": exc.message, "status": exc.status}, exc.status)
        except ManagerError as exc:
            response = _json({"error": exc.message, "status": exc.status}, exc.status)
        except web.HTTPException:
            raise
        except Exception as exc:  # never leak a traceback
            logger.exception("%s %s failed", request.method, request.path)
            response = _json({"error": f"internal error: {type(exc).__name__}", "status": 500}, 500)
        if not response.prepared:
            response.headers.update(cors)
        return response

    def api(handler: Handler) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.token}".encode()):
                raise ApiError(401, "missing or wrong access token")
            user = request.headers.get("X-Playground-User", "").strip()
            if not _USER_RE.match(user):
                raise ApiError(400, "X-Playground-User must match [A-Za-z0-9_.@-]{1,64}")
            return await handler(user, request)

        return wrapped

    async def healthz(_: web.Request) -> web.Response:
        return _json({"status": "ok", "api_version": API_VERSION})

    app = web.Application(middlewares=[guard], client_max_size=4 * 2 ** 20)
    sid, v = "{sid:[^/]+}", "/api/v1"
    routes = [
        ("GET", "/me", pg.me), ("GET", "/sessions", pg.list_sessions), ("POST", "/sessions", pg.create_session),
        ("GET", f"/sessions/{sid}", pg.get_session), ("DELETE", f"/sessions/{sid}", pg.delete_session),
        ("PATCH", f"/sessions/{sid}", pg.update_session),
        ("POST", f"/sessions/{sid}/query", pg.query), ("POST", "/batch-query", pg.batch_query),
        ("POST", f"/sessions/{sid}/cancel", pg.cancel), ("POST", f"/sessions/{sid}/vm/{{action}}", pg.vm_action),
        ("POST", f"/sessions/{sid}/fork", pg.fork), ("GET", f"/sessions/{sid}/workspace", pg.workspace),
        ("POST", f"/sessions/{sid}/workspace", pg.workspace), ("PUT", f"/sessions/{sid}/secrets", pg.secrets),
        ("POST", f"/sessions/{sid}/mcp", pg.mcp), ("POST", f"/sessions/{sid}/shell", pg.shell),
        ("GET", f"/sessions/{sid}/files", pg.files), ("GET", f"/sessions/{sid}/messages", pg.messages),
        ("POST", f"/sessions/{sid}/messages/{{mid:\\d+}}/reply", pg.reply),
        ("GET", f"/sessions/{sid}/egress", pg.egress), ("GET", f"/sessions/{sid}/debug", pg.debug),
        ("GET", "/ops", pg.ops), ("POST", "/ops/restart-manager", pg.restart), ("GET", "/events", pg.events),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, v + path, api(handler))
    async def preflight(_: web.Request) -> web.Response:  # answered by the middleware
        return web.Response(status=204)

    app.router.add_route("OPTIONS", "/api/{tail:.*}", preflight)
    app.router.add_get("/healthz", healthz)
    if settings.serve_frontend is not None:  # same-origin debugging only (D-08)
        frontend_dir = settings.serve_frontend

        async def index(_: web.Request) -> web.FileResponse:
            return web.FileResponse(frontend_dir / "index.html")

        app.router.add_get("/", index)
        app.router.add_static("/", frontend_dir)

    async def on_startup(_: web.Application) -> None:
        await Runner.start()
        await pg.manager.start()
        pg._alert_task = asyncio.create_task(pg.watch_alerts())
        logger.info("playground backend ready: manager %s, origins %s, restart-manager %s",
                    settings.manager_url, list(settings.allowed_origins) or "none",
                    "on" if settings.docker_socket else "off")

    async def on_cleanup(_: web.Application) -> None:
        if pg._alert_task is not None:
            pg._alert_task.cancel()
        await pg.manager.close()
        await Runner.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app["playground"] = pg
    return app
