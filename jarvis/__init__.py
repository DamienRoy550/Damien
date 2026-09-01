"""Jarvis — an offline-first, adaptive, cross-device personal assistant.

The package is deliberately import-light: ``import jarvis`` must work on a machine with
nothing but the standard library plus numpy, because the offline core (adaptive profile,
memory, voiceprint, device control policy, media rendering) is the part that has to run
without a network. Provider-backed capabilities (LLM chat, cloud image/video, hosted ASR)
are optional and degrade to local behaviour when absent.

See ``REQUIREMENTS.md`` for the capability list this implements and ``ARCHITECTURE.md``
for how the pieces fit together.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "capabilities", "describe"]


def capabilities(settings=None) -> dict[str, dict[str, object]]:
    """Capability matrix, also used by ``/api/health`` and ``jarvis doctor``.

    Importing lazily keeps ``import jarvis`` cheap and avoids an import cycle with
    :mod:`jarvis.api.app`.
    """
    from jarvis.api.app import Jarvis

    jarvis = Jarvis(settings) if settings is not None else None
    if jarvis is None:
        from jarvis.config import get_settings

        jarvis = Jarvis(get_settings())
    return jarvis.status(None)["capabilities"]


def describe() -> str:
    """Human-readable capability report for the CLI and README examples."""
    caps = capabilities()
    lines = []
    for name, info in caps.items():
        mark = "offline" if info.get("offline") else "online"
        lines.append(f"{name:24s} [{mark:7s}] {info.get('label', '')}")
    return "\n".join(lines)
