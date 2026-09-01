"""Connectivity detection and offline-mode policy.

"Offline first" has to be a property of the code path, not a fallback UI string, so
this module answers one question for every feature: *can this be served locally?*
Network-dependent work (LLM answers, real image/video models, remote replica sync)
is attempted only when reachable, and every capability degrades to a declared
local path instead of erroring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PROBE_HOSTS = (("one.one.one.one", 53), ("8.8.8.8", 53), ("1.1.1.1", 53))
CACHE_SECONDS = 10.0


@dataclass
class NetworkState:
    online: bool
    checked_at: float
    method: str
    latency_ms: int | None = None
    detail: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)
    local_engines: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "checked_at": self.checked_at,
            "method": self.method,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "capabilities": self.capabilities,
            "local_engines": self.local_engines,
            # the summary the UI and CLI both show, so "offline" is never ambiguous
            "offline_mode": not self.online,
        }


class Connectivity:
    """Cheap, cached reachability checks. Never blocks a core feature."""

    def __init__(self, *, probe_timeout: float = 0.6, cache_seconds: float = CACHE_SECONDS):
        self.probe_timeout = probe_timeout
        self.cache_seconds = cache_seconds
        self._cached: NetworkState | None = None
        self._probe: Callable[[], tuple[bool, int | None, str]] = self._tcp_probe

    def _tcp_probe(self) -> tuple[bool, int | None, str]:
        import socket

        for host, port in PROBE_HOSTS:
            started = time.time()
            try:
                with socket.create_connection((host, port), timeout=self.probe_timeout):
                    return True, int((time.time() - started) * 1000), f"{host}:{port}"
            except OSError:
                continue
        return False, None, "no reachable probe endpoint (DNS/TCP)"

    def state(self, settings, *, force: bool = False) -> NetworkState:
        now = time.time()
        if self._cached and not force and now - self._cached.checked_at < self.cache_seconds:
            return self._cached
        if os.environ.get("JARVIS_FORCE_OFFLINE") == "1":
            online, latency, detail = False, None, "JARVIS_FORCE_OFFLINE=1 (simulated offline)"
            method = "env"
        else:
            online, latency, detail = self._probe()
            method = "tcp"
        creds = settings.provider_credentials()
        self._cached = NetworkState(
            online=online,
            checked_at=now,
            method=method,
            latency_ms=latency,
            detail=detail,
            capabilities={k: bool(v and online) for k, v in creds.items()},
            local_engines={
                "asr": detect_local_asr(),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "note": "voiceprint + retrieval + sync queue always work locally",
            },
        )
        return self._cached

    def invalidate(self) -> None:
        self._cached = None


def detect_local_asr() -> dict[str, Any]:
    """Report any locally installed speech engine. Absent is a supported state:
    the assistant still works via text, typed commands and offline keyword spotting."""
    out: dict[str, Any] = {}
    for name, cmd in (
        ("whisper.cpp", ["whisper-cli", "--help"]),
        ("vosk", [os.environ.get("VOSK_MODEL_DIR", "")]),
    ):
        if name == "vosk":
            out[name] = bool(cmd[0]) and os.path.isdir(cmd[0])
            continue
        out[name] = shutil.which(cmd[0]) is not None
    try:  # optional Python bindings, if the user installed them
        import vosk  # type: ignore  # noqa: F401

        out["vosk_python"] = True
    except Exception:
        out["vosk_python"] = False
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        out["faster_whisper"] = True
    except Exception:
        out["faster_whisper"] = False
    return out


def run_with_timeout(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """argv-only execution used by the offline ASR adapters. Never shell=True."""
    try:
        # argv-only, no shell: cmd is built from fixed literals, never user text
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, shell=False)  # noqa: S603
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
