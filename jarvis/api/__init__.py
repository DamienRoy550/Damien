"""HTTP layer for Jarvis.

The API is a thin, stateless shell over the engine: everything a user can do over HTTP a
user can also do through :mod:`jarvis.cli`, and both go through the same op-log. Nothing
here owns business rules — policy lives in :mod:`jarvis.devctl`, identity in
:mod:`jarvis.auth`, learning in :mod:`jarvis.adaptive`.
"""

from __future__ import annotations

from jarvis.api.app import SECURITY_HEADERS, Jarvis, build_default_app, create_app

__all__ = ["SECURITY_HEADERS", "Jarvis", "build_default_app", "create_app"]
