# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight guest agent for Firecracker session VMs.

Runs inside the microVM (stdlib only, Python 3.12) and serves the subset of the
AIO sandbox HTTP API that ``openjiuwen``'s aio provider calls, over vsock:

* ``POST v1/file/read|write|list|glob|upload``, ``GET v1/file/download``
* ``POST v1/shell/exec``

Every API path must start with ``/t/<guest_token>/``; the token comes from
MMDS (V2) at boot, so only the host that configured MMDS can call the agent.
The same token guards the CDP bridge in front of the per-VM headless Chromium.
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import fnmatch
import http.client
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

API_PORT = 8080
CDP_BRIDGE_PORT = 9222
CHROME_PORT = 9223
WORKSPACE = Path(os.environ.get("FC_WORKSPACE", "/workspace"))
MMDS_ADDR = os.environ.get("FC_MMDS_ADDR", "169.254.169.254")
CHROME_BIN = os.environ.get("FC_CHROME_BIN", "/usr/local/bin/fc-chromium")
CHROME_PROFILE = Path(os.environ.get("FC_CHROME_PROFILE", "/var/lib/fc-browser/profile"))
DEFAULT_EXEC_TIMEOUT = 600

_identity: dict[str, str] = {}
_chrome_lock = threading.Lock()
_chrome_proc: Optional[subprocess.Popen] = None


def _log(msg: str) -> None:
    print(f"[fc-guest-agent] {msg}", flush=True)


# --------------------------------------------------------------------------- MMDS


def load_identity(retries: int = 60) -> dict[str, str]:
    """Fetch session identity from MMDS V2 (token first, then the data)."""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                f"http://{MMDS_ADDR}/latest/api/token",
                method="PUT",
                headers={"X-metadata-token-ttl-seconds": "21600"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                token = resp.read().decode()
            req = urllib.request.Request(
                f"http://{MMDS_ADDR}/latest/meta-data",
                headers={"X-metadata-token": token, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
            return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:  # MMDS not ready yet
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"MMDS identity unavailable: {last_error}")


# ---------------------------------------------------------------------- helpers


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _file_info(path: Path, include_size: bool = True, include_permissions: bool = False) -> dict[str, Any]:
    st = path.lstat()
    is_dir = path.is_dir()
    info: dict[str, Any] = {
        "name": path.name,
        "path": str(path),
        "is_directory": is_dir,
        "modified_time": _iso(st.st_mtime),
        "extension": path.suffix or None,
    }
    if include_size:
        info["size"] = 0 if is_dir else st.st_size
    if include_permissions:
        info["permissions"] = oct(st.st_mode & 0o777)[2:]
    return info


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else WORKSPACE / p


class ApiFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "message": "ok", "data": data}


# ------------------------------------------------------------------ API handlers


def api_file_read(body: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(body["file"])
    if not path.is_file():
        raise ApiFailure(404, f"File not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    start, end = body.get("start_line"), body.get("end_line")
    if start is not None or end is not None:
        lines = text.splitlines(keepends=True)
        text = "".join(lines[(start or 0):end])
    return _ok({"content": text, "file": str(path)})


def api_file_write(body: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(body["file"])
    content = body.get("content", "")
    encoding = body.get("encoding") or "utf-8"
    if encoding == "base64":
        data = base64.b64decode(content)
    else:
        if body.get("leading_newline"):
            content = "\n" + content
        if body.get("trailing_newline"):
            content = content + "\n"
        data = content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab" if body.get("append") else "wb") as fh:
        fh.write(data)
    return _ok({"file": str(path), "bytes_written": len(data)})


def _walk(root: Path, recursive: bool, max_depth: Optional[int], show_hidden: bool):
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        depth = len(cur.parts) - base_depth
        if not show_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(dirnames) + sorted(filenames):
            if not show_hidden and name.startswith("."):
                continue
            yield cur / name
        if not recursive or (max_depth is not None and depth + 1 >= max_depth):
            dirnames[:] = []


def api_file_list(body: dict[str, Any]) -> dict[str, Any]:
    root = _resolve(body.get("path") or str(WORKSPACE))
    if not root.is_dir():
        raise ApiFailure(404, f"Directory not found: {root}")
    file_types = body.get("file_types")
    files = []
    for p in _walk(root, bool(body.get("recursive")), body.get("max_depth"), bool(body.get("show_hidden"))):
        if file_types and not p.is_dir() and p.suffix.lstrip(".") not in {t.lstrip(".") for t in file_types}:
            continue
        files.append(_file_info(p, include_permissions=bool(body.get("include_permissions"))))
    sort_by = body.get("sort_by") or "name"
    files.sort(key=lambda f: f.get(sort_by) or "", reverse=bool(body.get("sort_desc")))
    dirs = sum(1 for f in files if f["is_directory"])
    return _ok({
        "path": str(root),
        "files": files,
        "total_count": len(files),
        "directory_count": dirs,
        "file_count": len(files) - dirs,
    })


def api_file_glob(body: dict[str, Any]) -> dict[str, Any]:
    root = _resolve(body.get("path") or str(WORKSPACE))
    pattern = body.get("pattern") or "*"
    excludes = body.get("exclude") or []
    files_only = body.get("files_only", True)
    max_results = body.get("max_results") or 10000
    matches = []
    for p in sorted(root.glob(pattern)):
        rel = str(p.relative_to(root))
        if not body.get("include_hidden") and any(part.startswith(".") for part in Path(rel).parts):
            continue
        if any(fnmatch.fnmatch(rel, ex) for ex in excludes):
            continue
        if files_only and p.is_dir():
            continue
        matches.append(_file_info(p))
    truncated = len(matches) > max_results
    return _ok({
        "path": str(root),
        "pattern": pattern,
        "files": matches[:max_results],
        "total_count": len(matches),
        "truncated": truncated,
    })


def api_file_upload(content_type: str, raw: bytes) -> dict[str, Any]:
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + raw
    )
    target: Optional[str] = None
    payload: Optional[bytes] = None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "path":
            target = part.get_content().strip() if part.get_content_maintype() == "text" else part.get_payload(
                decode=True).decode()
        elif name == "file":
            payload = part.get_payload(decode=True) or b""
    if target is None or payload is None:
        raise ApiFailure(400, "upload requires 'file' and 'path' fields")
    path = _resolve(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _ok({"file_path": str(path), "file_size": len(payload), "success": True})


def api_shell_exec(body: dict[str, Any]) -> dict[str, Any]:
    """Run a command; complete when the shell exits, not when every pipe closes.

    Background jobs started by the command may keep the output pipe open
    forever, so output is read in a thread and given a short grace period
    after the shell exits instead of waiting for EOF.
    """
    command = body["command"]
    cwd = body.get("exec_dir") or str(WORKSPACE)
    timeout = body.get("hard_timeout") or body.get("timeout") or DEFAULT_EXEC_TIMEOUT
    env = dict(os.environ, HOME="/root", TERM="dumb")
    proc = subprocess.Popen(
        ["/bin/bash", "-c", command],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    chunks: list[bytes] = []

    def _reader() -> None:
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            chunks.append(chunk)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=float(timeout))
        status, exit_code = "completed", proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait()
        status, exit_code = "hard_timeout", -1
    reader.join(timeout=1.0)
    output = b"".join(chunks).decode("utf-8", errors="replace")
    return _ok({
        "session_id": body.get("id") or "default",
        "command": command,
        "status": status,
        "output": output,
        "exit_code": exit_code,
    })


JSON_ROUTES = {
    ("POST", "v1/file/read"): api_file_read,
    ("POST", "v1/file/write"): api_file_write,
    ("POST", "v1/file/list"): api_file_list,
    ("POST", "v1/file/glob"): api_file_glob,
    ("POST", "v1/shell/exec"): api_shell_exec,
}


# ----------------------------------------------------------------- HTTP server


class VsockThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_VSOCK
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_name, self.server_port = "vsock", self.server_address[1]


class ApiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fc-guest-agent/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self) -> tuple[Optional[str], dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        prefix = f"/t/{_identity.get('guest_token', '')}/"
        if not _identity.get("guest_token") or not parsed.path.startswith(prefix):
            return None, {}
        return parsed.path[len(prefix):].strip("/"), urllib.parse.parse_qs(parsed.query)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _handle(self, method: str) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        route, query = self._route()
        body = self._read_body() if method == "POST" else b""
        if route is None:
            self._send_json(403, {"success": False, "message": "forbidden"})
            return
        try:
            if route == "fc/identity" and method == "GET":
                self._send_json(200, _ok({"session_id": _identity.get("session_id"),
                                          "owner_user_id": _identity.get("owner_user_id"),
                                          "hostname": socket.gethostname()}))
            elif route == "v1/file/download" and method == "GET":
                self._download(query)
            elif route == "v1/file/upload" and method == "POST":
                self._send_json(200, api_file_upload(self.headers.get("Content-Type", ""), body))
            elif (method, route) in JSON_ROUTES:
                self._send_json(200, JSON_ROUTES[(method, route)](json.loads(body or b"{}")))
            else:
                self._send_json(404, {"success": False, "message": f"unknown route {method} {route}"})
        except ApiFailure as exc:
            self._send_json(exc.status, {"success": False, "message": exc.message})
        except (KeyError, ValueError, TypeError) as exc:
            self._send_json(400, {"success": False, "message": f"bad request: {exc}"})
        except OSError as exc:
            self._send_json(500, {"success": False, "message": str(exc)})

    def _download(self, query: dict[str, list[str]]) -> None:
        path = _resolve((query.get("path") or [""])[0])
        if not path.is_file():
            raise ApiFailure(404, f"File not found: {path}")
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")


# ------------------------------------------------------------------ CDP bridge


def ensure_chrome() -> None:
    """Start headless Chromium on first use; downloads land in the workspace."""
    global _chrome_proc
    with _chrome_lock:
        if _chrome_proc is not None and _chrome_proc.poll() is None:
            return
        prefs = CHROME_PROFILE / "Default" / "Preferences"
        downloads = WORKSPACE / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        if not prefs.exists():
            prefs.parent.mkdir(parents=True, exist_ok=True)
            prefs.write_text(json.dumps({"download": {"default_directory": str(downloads),
                                                      "prompt_for_download": False}}))
        _chrome_proc = subprocess.Popen(
            [CHROME_BIN, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
             "--no-first-run", "--no-default-browser-check", f"--user-data-dir={CHROME_PROFILE}",
             "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={CHROME_PORT}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", CHROME_PORT), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("chromium did not open its debugging port")


def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _reply(conn: socket.socket, status: str, body: bytes, content_type: str = "text/plain") -> None:
    conn.sendall(
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n".encode() + body
    )


def handle_cdp(conn: socket.socket) -> None:
    """Token-checked bridge: rewrite /json* bodies, splice websocket upgrades."""
    try:
        head, rest = _read_head(conn)
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        prefix = f"/t/{_identity.get('guest_token', '')}"
        if not _identity.get("guest_token") or not target.startswith(prefix + "/"):
            _reply(conn, "403 Forbidden", b"forbidden")
            return
        inner = target[len(prefix):]
        ensure_chrome()
        if headers.get("upgrade", "").lower() == "websocket":
            upstream = socket.create_connection(("127.0.0.1", CHROME_PORT))
            new_lines = [f"{method} {inner} {version}"]
            for line in lines[1:]:
                if line.lower().startswith("host:"):
                    line = f"Host: 127.0.0.1:{CHROME_PORT}"
                new_lines.append(line)
            upstream.sendall("\r\n".join(new_lines).encode("latin-1") + b"\r\n\r\n" + rest)
            t = threading.Thread(target=_pipe, args=(upstream, conn), daemon=True)
            t.start()
            _pipe(conn, upstream)
            t.join()
            return
        # Plain HTTP (/json/version etc.): ask Chrome with a localhost Host header,
        # then point the websocket URLs back at this bridge (with the token prefix).
        public_host = headers.get("host", f"127.0.0.1:{CDP_BRIDGE_PORT}")
        client = http.client.HTTPConnection("127.0.0.1", CHROME_PORT, timeout=10)
        client.request(method, inner, headers={"Host": f"127.0.0.1:{CHROME_PORT}"})
        resp = client.getresponse()
        body = resp.read()
        for local in (f"127.0.0.1:{CHROME_PORT}", f"localhost:{CHROME_PORT}"):
            body = body.replace(f"ws://{local}/".encode(), f"ws://{public_host}{prefix}/".encode())
        _reply(conn, f"{resp.status} {resp.reason}", body, resp.getheader("Content-Type", "application/json"))
    except Exception as exc:  # keep the bridge alive for other clients
        _log(f"cdp bridge error: {exc}")
        try:
            _reply(conn, "502 Bad Gateway", str(exc).encode())
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_cdp_bridge() -> None:
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((socket.VMADDR_CID_ANY, CDP_BRIDGE_PORT))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_cdp, args=(conn,), daemon=True).start()


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    os.chdir(WORKSPACE)
    _identity.update(load_identity())
    _log(f"identity loaded for session {_identity.get('session_id')}")
    threading.Thread(target=serve_cdp_bridge, daemon=True).start()
    server = VsockThreadingHTTPServer((socket.VMADDR_CID_ANY, API_PORT), ApiHandler)
    _log(f"serving AIO-compatible API on vsock port {API_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
