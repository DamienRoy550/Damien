"""Runtime configuration.

Every feature must have a working offline path, so nothing in the default
configuration requires a network. Provider credentials are optional and, when
absent, the corresponding capability falls back to its local implementation
instead of failing.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _data_dir() -> Path:
    env = os.environ.get("JARVIS_DATA_DIR")
    path = Path(env).expanduser() if env else Path.cwd() / "var"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=".env", extra="ignore")

    # --- identity / storage -------------------------------------------------
    data_dir: Path = Field(default_factory=_data_dir)
    device_id: str = Field(default_factory=lambda: os.environ.get("JARVIS_DEVICE_ID", "local-device"))
    device_name: str = Field(default_factory=lambda: os.environ.get("JARVIS_DEVICE_NAME", "This machine"))

    # --- security -----------------------------------------------------------
    # Signing key for local session/CSRF tokens. Persisted so sessions survive a restart.
    secret_key: str = Field(default_factory=lambda: os.environ.get("JARVIS_SECRET_KEY", ""))
    session_ttl_seconds: int = 8 * 3600
    refresh_ttl_seconds: int = 30 * 24 * 3600
    enforce_voiceprint_for_privileged: bool = True
    rate_limit_per_minute: int = 60
    max_upload_bytes: int = 8 * 1024 * 1024

    # --- authentication providers (all optional) --------------------------
    allow_passwordless_dev_idp: bool = True
    google_client_id: str = ""
    google_client_secret: str = ""
    apple_client_id: str = ""
    apple_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    redirect_base_url: str = "http://localhost:8000"

    # --- capability providers (optional, offline fallbacks exist) ---------
    llm_base_url: str = ""          # any OpenAI-compatible endpoint
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    image_provider: str = "local"   # local | openai | replicate
    video_provider: str = "local"   # local | replicate
    asr_provider: str = "offline"   # offline | whisper-http
    asr_endpoint: str = ""

    # --- adaptive learning --------------------------------------------------
    trait_half_life_days: float = 45.0
    trait_reinforcement: float = 0.35
    trait_penalty: float = 0.25
    trait_bounds: tuple[float, float] = (-1.0, 1.0)

    # --- sync ---------------------------------------------------------------
    sync_batch_limit: int = 500
    auto_sync_interval_seconds: int = 300

    @field_validator("data_dir", mode="after")
    @classmethod
    def _make_data_dir(cls, value: Path) -> Path:
        # mkdir here rather than in the default factory: when data_dir comes from
        # JARVIS_DATA_DIR the factory never runs, and the first thing that touches the
        # directory (the secret key, the database) would fail with a bare FileNotFoundError.
        value.mkdir(parents=True, exist_ok=True)
        return value

    def ensure_secret_key(self) -> str:
        if self.secret_key:
            return self.secret_key
        key_file = self.data_dir / "secret.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            self.secret_key = key_file.read_text().strip()
        else:
            self.secret_key = secrets.token_urlsafe(48)
            key_file.write_text(self.secret_key)
            key_file.chmod(0o600)
        return self.secret_key

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    @property
    def blob_dir(self) -> Path:
        path = self.data_dir / "blobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def provider_credentials(self) -> dict[str, bool]:
        """Which capabilities can reach the network. Drives the online/offline label."""
        return {
            "llm": bool(self.llm_base_url and self.llm_api_key),
            "image": self.image_provider != "local",
            "video": self.video_provider != "local",
            "asr": bool(self.asr_provider != "offline" and self.asr_endpoint),
            "identity": bool(
                self.google_client_id or self.apple_client_id or self.microsoft_client_id
            ),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
