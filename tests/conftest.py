from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from jarvis.accounts import AccountStore
from jarvis.api.app import Jarvis
from jarvis.auth import AuthService
from jarvis.config import Settings
from jarvis.db import Database


@pytest.fixture()
def tmp_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
    yield path


@pytest.fixture()
def settings(tmp_dir: Path) -> Settings:
    st = Settings(
        data_dir=tmp_dir / "device",
        device_id="test-device",
        device_name="Test laptop",
        secret_key="test-secret-key-not-for-production",
        allow_passwordless_dev_idp=True,
        llm_base_url="",
        llm_api_key="",
        image_provider="local",
        video_provider="local",
        asr_provider="offline",
        trait_half_life_days=45.0,
    )
    st.data_dir.mkdir(parents=True, exist_ok=True)
    return st


@pytest.fixture()
def db(settings: Settings) -> Database:
    return Database(settings.db_path)


@pytest.fixture()
def accounts(db: Database, settings: Settings) -> AccountStore:
    return AccountStore(db, settings)


@pytest.fixture()
def auth(db: Database, settings: Settings) -> AuthService:
    return AuthService(db, settings)


@pytest.fixture()
def signed_in(db: Database, settings: Settings) -> dict:
    """A user with a live session, as most tests need."""
    service = AuthService(db, settings)
    result = service.complete_dev_login(
        email="owner@example.com", display_name="Owner", provider="google",
        device_id="test-device", device_name="Test laptop", platform="test",
    )
    result["service"] = service
    return result


@pytest.fixture()
def core(settings: Settings) -> Jarvis:
    return Jarvis(settings)
