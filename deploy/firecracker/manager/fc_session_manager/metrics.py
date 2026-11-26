# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""In-process metrics (Prometheus text format) and simple alert rules."""

from __future__ import annotations

import collections
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

_WINDOW = 1000  # samples kept per duration metric
_EVENT_WINDOW_S = 600  # failures counted for alerts


def _quantile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = collections.defaultdict(float)
        self.durations: dict[str, collections.deque] = {}
        self.duration_totals: dict[str, tuple[int, float]] = {}
        self._failures: collections.deque = collections.deque(maxlen=1000)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self.counters[(name, tuple(sorted(labels.items())))] += value
        if name.endswith("_failures"):
            self._failures.append(time.time())

    def observe(self, name: str, seconds: float) -> None:
        self.durations.setdefault(name, collections.deque(maxlen=_WINDOW)).append(seconds)
        count, total = self.duration_totals.get(name, (0, 0.0))
        self.duration_totals[name] = (count + 1, total + seconds)

    def quantile(self, name: str, q: float) -> float:
        return _quantile(list(self.durations.get(name, ())), q)

    def recent_failures(self) -> int:
        cutoff = time.time() - _EVENT_WINDOW_S
        return sum(1 for ts in self._failures if ts >= cutoff)

    def render(self, gauges: dict[str, Any]) -> str:
        lines: list[str] = []
        for (name, labels), value in sorted(self.counters.items()):
            label_text = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"fc_{name}_total{{{label_text}}} {value:g}")
        for name in sorted(self.durations):
            for q in (0.5, 0.95, 0.99):
                lines.append(f'fc_{name}_seconds{{quantile="{q}"}} {self.quantile(name, q):.4f}')
            count, total = self.duration_totals[name]
            lines.append(f"fc_{name}_seconds_count {count}")
            lines.append(f"fc_{name}_seconds_sum {total:.4f}")
        for name, value in sorted(gauges.items()):
            if isinstance(value, dict):
                for label, item in sorted(value.items()):
                    lines.append(f'fc_{name}{{state="{label}"}} {item:g}')
            else:
                lines.append(f"fc_{name} {value:g}")
        return "\n".join(lines) + "\n"


def host_memory() -> tuple[int, int]:
    """(MemTotal, MemAvailable) in MiB, from /proc/meminfo."""
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0]) // 1024
    except OSError:
        return 0, 0
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def evaluate_alerts(metrics: Metrics, gauges: dict[str, Any], max_vms: int, data_dir: Path,
                    create_p95_limit_s: float) -> list[dict[str, Any]]:
    """Alert rules over the current gauges; each alert has name, severity and message."""
    alerts: list[dict[str, Any]] = []

    def add(name: str, severity: str, message: str) -> None:
        alerts.append({"name": name, "severity": severity, "message": message})

    live = gauges.get("vm_slots_used", 0)
    if max_vms and live >= 0.9 * max_vms:
        add("vm_slots_low", "warning", f"{live}/{max_vms} VM slots in use")
    total, available = gauges.get("host_mem_total_mib", 0), gauges.get("host_mem_available_mib", 0)
    if total and available < 0.1 * total:
        add("host_memory_low", "critical", f"host has {available} MiB of {total} MiB available")
    usage = shutil.disk_usage(data_dir) if data_dir.exists() else None
    if usage is not None and usage.free < 0.1 * usage.total:
        add("data_disk_low", "critical", f"data volume has {usage.free // 2 ** 20} MiB free")
    samples = metrics.durations.get("session_create", ())
    p95 = metrics.quantile("session_create", 0.95)
    if len(samples) >= 5 and p95 > create_p95_limit_s:
        add("session_create_slow", "warning", f"create P95 {p95:.2f}s > {create_p95_limit_s:.2f}s")
    failures = metrics.recent_failures()
    if failures:
        add("operation_failures", "warning", f"{failures} boot/restore/persist failure(s) in the last 10 minutes")
    if gauges.get("warm_pool_target", 0) and not gauges.get("warm_pool_ready", 0):
        add("warm_pool_empty", "info", "no pre-booted VM is ready; creates boot from scratch")
    return alerts


def load_average() -> Optional[float]:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None
