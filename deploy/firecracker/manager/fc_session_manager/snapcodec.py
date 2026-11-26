# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Seal (compress, then encrypt) and unseal snapshot files at rest.

Sealing turns ``<name>`` into ``<name>.sealed``: ``zstd`` (optional) piped into
``openssl enc -aes-256-ctr -pbkdf2`` (optional) with a key file only the manager
reads. Unsealing restores the original (sparse) file. Snapshot memory files hold
whatever the guest had in RAM, including credentials it read, so cold snapshots
should not sit on disk in clear text.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Optional

SEALED_SUFFIX = ".sealed"
# Sealing runs in the background: low CPU priority and two zstd threads, so it never
# starves the Firecracker processes and API calls of running sessions.
_NICE = ["nice", "-n", "15"]


class SealError(RuntimeError):
    pass


def ensure_key(path: Path) -> Path:
    """Create a random 256-bit key file (mode 0600) if it does not exist yet."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return path


def _pipeline(commands: list[list[str]], src: Path, dst: Path) -> None:
    """Run ``commands`` as a pipe from ``src`` to ``dst``; raise SealError on any failure."""
    procs: list[subprocess.Popen] = []
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        stdin = fin
        for i, cmd in enumerate(commands):
            last = i == len(commands) - 1
            proc = subprocess.Popen(cmd, stdin=stdin, stdout=fout if last else subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            if stdin is not fin:
                stdin.close()  # let the previous stage see SIGPIPE
            procs.append(proc)
            stdin = proc.stdout  # type: ignore[assignment]
        errors = []
        for proc in procs:
            _, err = proc.communicate()
            if proc.returncode != 0:
                errors.append(f"{proc.args[0]}: {err.decode(errors='replace').strip()}")
    if errors:
        dst.unlink(missing_ok=True)
        raise SealError("; ".join(errors))


def seal_dir(snap_dir: Path, names: tuple[str, ...], compress: bool, key_file: Optional[Path]) -> dict[str, Any]:
    """Seal ``names`` in ``snap_dir``; returns the ``sealed`` metadata entry."""
    stages: list[list[str]] = []
    if compress:
        stages.append(_NICE + ["zstd", "-q", "-3", "-T2", "-c"])
    if key_file is not None:
        stages.append(_NICE + ["openssl", "enc", "-aes-256-ctr", "-pbkdf2", "-salt", "-pass", f"file:{key_file}"])
    if not stages:
        return {}
    sizes = {}
    for name in names:
        src, dst = snap_dir / name, snap_dir / f"{name}{SEALED_SUFFIX}"
        if not src.exists():
            continue
        tmp = dst.with_name(f"{dst.name}.partial")
        _pipeline(stages, src, tmp)
        os.chmod(tmp, 0o600)
        sizes[name] = tmp.stat().st_size
        os.replace(tmp, dst)  # a sealed file is either complete or absent
        src.unlink()
    return {"compress": "zstd" if compress else None,
            "encrypt": "aes-256-ctr" if key_file is not None else None, "sealed_bytes": sizes}


def unseal_dir(snap_dir: Path, sealed: dict[str, Any], key_file: Optional[Path]) -> None:
    stages: list[list[str]] = []
    if sealed.get("encrypt"):
        if key_file is None or not key_file.exists():
            raise SealError("snapshot is encrypted but the key file is missing")
        stages.append(["openssl", "enc", "-d", "-aes-256-ctr", "-pbkdf2", "-pass", f"file:{key_file}"])
    if sealed.get("compress"):
        stages.append(["zstd", "-q", "-d", "-c"])
    for name in sealed.get("sealed_bytes", {}):
        src, dst = snap_dir / f"{name}{SEALED_SUFFIX}", snap_dir / name
        tmp = dst.with_name(f"{name}.unsealing")
        _pipeline(stages, src, tmp)
        # Re-sparsify the memory image (zero pages need no disk space).
        subprocess.run(["fallocate", "--dig-holes", str(tmp)], capture_output=True, check=False)
        os.replace(tmp, dst)
        src.unlink()
