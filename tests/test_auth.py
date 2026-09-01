"""Sign-in, sessions, cross-device continuity, and what email linking may NOT do."""

from __future__ import annotations

import json
import time

import jwt
import pytest
from jarvis import voice
from jarvis.accounts import AccountStore
from jarvis.auth import AuthError, AuthService


def service(db, settings) -> AuthService:
    return AuthService(db, settings)


# ------------------------------------------------------------------ sessions
def test_tokens_are_never_stored_in_plaintext(db, settings):
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    user = accounts.create_user("Owner", email="owner@example.com")
    issued = auth.issue_session(user["id"], None)
    token = issued["token"]
    assert len(token) >= 32
    # the raw token must appear nowhere in the database file
    with db.write() as conn:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
    dumped = json.dumps([dict(r) for r in sessions]) + json.dumps([tuple(r) for r in rows])
    assert token not in dumped, "plaintext session token leaked into storage"
    assert auth._hash(token) in dumped, "the hash index should be what is stored"
    assert auth.authenticate(token) is not None


def test_expired_and_revoked_sessions_are_rejected(db, settings):
    auth = service(db, settings)
    user = AccountStore(db, settings).create_user("Owner")
    issued = auth.issue_session(user["id"], None)
    with db.write() as conn:
        conn.execute("UPDATE sessions SET expires_at=? WHERE id=?", (time.time() - 1, issued["session_id"]))
    assert auth.authenticate(issued["token"]) is None
    issued2 = auth.issue_session(user["id"], None)
    auth.logout(issued2["session_id"])
    assert auth.authenticate(issued2["token"]) is None


def test_refresh_rotation_invalidates_the_previous_pair(db, settings):
    auth = service(db, settings)
    user = AccountStore(db, settings).create_user("Owner")
    first = auth.issue_session(user["id"], None)
    second = auth.refresh(first["refresh_token"])
    assert second and second["token"] != first["token"]
    assert auth.authenticate(second["token"]) is not None
    assert auth.authenticate(first["token"]) is None, "old access token must die on rotation"
    assert auth.refresh(first["refresh_token"]) is None, "and the consumed refresh token must not be replayable"


def test_logout_everywhere_kicks_all_devices(db, settings):
    auth = service(db, settings)
    user = AccountStore(db, settings).create_user("Owner")
    tokens = [auth.issue_session(user["id"], None)["token"] for _ in range(3)]
    assert all(auth.authenticate(t) for t in tokens)
    auth.logout_everywhere(user["id"])
    assert not any(auth.authenticate(t) for t in tokens)


# ---------------------------------------------------------------- identities
def test_dev_login_creates_account_device_and_basic_session(db, settings):
    auth = service(db, settings)
    result = auth.complete_dev_login(
        email="owner@example.com", display_name="Owner", provider="google",
        device_id="mac-1", device_name="MacBook", platform="macos",
    )
    assert result["created"] is True
    assert result["session"]["scope"] == "basic"
    assert result["user"]["profile"]["email"] == "owner@example.com"
    devices = AccountStore(db, settings).list_devices(result["user"]["id"])
    assert [d["id"] for d in devices] == ["mac-1"]
    assert devices[0]["trust_level"] == "untrusted", "a new device starts untrusted by default"


def test_second_login_with_same_identity_reuses_the_account(db, settings):
    auth = service(db, settings)
    a = auth.complete_dev_login(email="owner@example.com", display_name="Owner", provider="google", device_id="d1", platform="web")
    b = auth.complete_dev_login(email="owner@example.com", display_name="Owner", provider="google", device_id="d2", platform="web")
    assert a["user"]["id"] == b["user"]["id"]
    assert b["created"] is False
    assert {d["id"] for d in AccountStore(db, settings).list_devices(b["user"]["id"])} == {"d1", "d2"}


def test_a_second_provider_links_only_on_a_verified_email(db, settings):
    """Cross-provider account continuity is an email-linking decision, and the wrong
    rule here is account takeover: someone registering your address on a provider that
    does not verify it must not inherit your profile."""
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    first = auth.complete_dev_login(email="shared@example.com", display_name="Owner", provider="google", device_id="d1", platform="web")
    accounts.link_identity(first["user"]["id"], "microsoft", "ms-sub-1", email="shared@example.com", email_verified=False)
    orphan = auth._provision(
        {"sub": "ms-orphan", "email": "shared@example.com", "email_verified": False, "name": "Attacker"},
        provider="microsoft", device_id="d9", device_name=None, platform="web",
    )
    assert orphan["user"]["id"] != first["user"]["id"], "unverified address must not reach the real account"
    linked = auth._provision(
        {"sub": "ms-sub-2", "email": "shared@example.com", "email_verified": True, "name": "Owner"},
        provider="microsoft", device_id="d8", device_name=None, platform="web",
    )
    assert linked["user"]["id"] == first["user"]["id"], "a verified address does join the same account"


def test_id_token_with_alg_none_is_rejected(db, settings):
    auth = service(db, settings)
    forged = jwt.encode({"sub": "x", "iss": "jarvis-dev-idp", "aud": "jarvis-local", "exp": int(time.time()) + 60, "iat": int(time.time())}, key=None, algorithm="none")
    with pytest.raises(AuthError, match="signature algorithm"):
        auth.flow.verify_id_token("dev-idp", forged, nonce=None, client_id="jarvis-local")


def test_hmac_token_from_a_real_provider_is_rejected(db, settings):
    """The classic OIDC confusion: an attacker who knows the JWKS is public tries to
    sign with HS256 using the public key as the secret."""
    auth = service(db, settings)
    token = jwt.encode({"sub": "x"}, key="not-a-real-key-but-long-enough-for-hs256", algorithm="HS256")
    with pytest.raises(AuthError, match="signature algorithm"):
        auth.flow.verify_id_token("google", token, nonce="n", client_id="cid")


def test_nonce_mismatch_is_rejected(db, settings):
    auth = service(db, settings)
    now = int(time.time())
    token = jwt.encode({"sub": "s", "iss": "jarvis-dev-idp", "aud": "jarvis-local", "iat": now, "exp": now + 60, "nonce": "right"}, auth.settings.ensure_secret_key(), algorithm="HS256")
    with pytest.raises(AuthError, match="nonce"):
        auth.flow.verify_id_token("dev-idp", token, nonce="wrong", client_id="jarvis-local")


def test_login_state_is_single_use_and_bound_to_the_device(db, settings):
    auth = service(db, settings)
    begun = auth.flow.begin("google", device_id="d1")
    state = begun["state"]
    auth.flow.take(state)  # consume it
    with pytest.raises(AuthError):
        auth.flow.take(state)
    with pytest.raises(AuthError):
        auth.flow.take("made-up-state")


def test_unknown_provider_is_rejected(db, settings):
    with pytest.raises(AuthError, match="unknown provider"):
        service(db, settings).flow.begin("yahoo", device_id="d1")


def test_dev_idp_can_be_disabled(db, settings):
    settings.allow_passwordless_dev_idp = False
    with pytest.raises(AuthError, match="disabled"):
        service(db, settings).complete_dev_login(email="a@b.c", display_name=None, provider="google", device_id="d", device_name=None, platform="web")


# ---------------------------------------------------------------- step-up
def test_step_up_upgrades_then_expires(db, settings):
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    login = auth.complete_dev_login(email="voice@example.com", display_name="V", provider="google", device_id="d1", device_name=None, platform="web")
    uid, sid = login["user"]["id"], login["session"]["session_id"]
    samples = [voice.embedding_from(voice.decode_audio(voice.synthesize_probe_pcm(f"take {i} for step up", 2.6, 118.0)))[0] for i in range(3)]
    model = voice.enroll(samples)
    model.calibrated = True  # pretend a real trial set validated this threshold
    accounts.put_voiceprint(uid, model)
    candidate = voice.pcm16_to_wav_bytes(voice.synthesize_probe_pcm("take 1 for step up", 2.6, 118.0))
    ok = auth.verify_voice_step_up(uid, sid, candidate)
    assert ok["accepted"] is True, ok
    assert auth.authenticate(login["session"]["token"])["scope"] == "privileged"
    auth.revoke_step_up(sid)
    assert auth.authenticate(login["session"]["token"])["scope"] == "basic"


def test_step_up_refuses_another_voice_and_an_uncalibrated_model(db, settings):
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    login = auth.complete_dev_login(email="voice2@example.com", display_name="V", provider="google", device_id="d1", device_name=None, platform="web")
    uid, sid = login["user"]["id"], login["session"]["session_id"]
    samples = [voice.embedding_from(voice.decode_audio(voice.synthesize_probe_pcm(f"unlock the door {i}", 2.6, 118.0)))[0] for i in range(3)]
    accounts.put_voiceprint(uid, voice.enroll(samples))  # NOT calibrated
    stranger = voice.pcm16_to_wav_bytes(voice.synthesize_probe_pcm("unlock the door 0", 2.6, 215.0))
    result = auth.verify_voice_step_up(uid, sid, stranger)
    assert result["accepted"] is False
    assert auth.authenticate(login["session"]["token"])["scope"] == "basic"
    owner = voice.pcm16_to_wav_bytes(voice.synthesize_probe_pcm("unlock the door 0", 2.6, 118.0))
    still_locked = auth.verify_voice_step_up(uid, sid, owner)
    assert still_locked["accepted"] is False and still_locked["blocked_reason"] == "uncalibrated"


def test_step_up_without_enrolment_explains_itself(db, settings):
    auth = service(db, settings)
    login = auth.complete_dev_login(email="fresh@example.com", display_name="F", provider="google", device_id="d1", device_name=None, platform="web")
    result = auth.verify_voice_step_up(login["user"]["id"], login["session"]["session_id"], b"junk" * 8000)
    assert result["accepted"] is False and result["reason"] in {"not-enrolled", "audio-quality"}


# ---------------------------------------------------------------- devices
def test_trusting_and_revoking_a_device_kills_its_sessions(db, settings):
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    login = auth.complete_dev_login(email="dev@example.com", display_name="D", provider="google", device_id="phone-1", device_name="Pixel", platform="android")
    uid = login["user"]["id"]
    token = login["session"]["token"]
    assert auth.authenticate(token)["device_trusted"] is False
    accounts.set_device_trust(uid, "phone-1", "trusted")
    assert auth.authenticate(token)["device_trusted"] is True
    accounts.set_device_trust(uid, "phone-1", "revoked")
    auth.untrust_device(uid, "phone-1")
    assert auth.authenticate(token) is None, "revoking a device must end its sessions"


def test_pairing_returns_a_code_and_does_not_auto_trust(db, settings):
    auth = service(db, settings)
    login = auth.complete_dev_login(email="pair@example.com", display_name="P", provider="google", device_id="laptop", device_name=None, platform="web")
    result = auth.request_pairing(login["user"]["id"], "new-phone")
    assert len(result["code"]) == 6 and result["code"].isdigit()
    assert auth.accounts.get_device("new-phone")["trust_level"] == "untrusted", "requesting pairing is not pairing"


def test_voiceprint_syncs_as_an_op_and_is_not_reversible_to_audio(db, settings):
    auth = service(db, settings)
    accounts = AccountStore(db, settings)
    login = auth.complete_dev_login(email="syncvp@example.com", display_name="S", provider="google", device_id="d1", device_name=None, platform="web")
    uid = login["user"]["id"]
    model = voice.enroll([voice.embedding_from(voice.decode_audio(voice.synthesize_probe_pcm(f"shared {i}", 2.6, 130.0)))[0] for i in range(3)])
    accounts.put_voiceprint(uid, model)
    op = db.one("SELECT payload, entity FROM oplog WHERE entity='voice_enrollment' ORDER BY seq DESC LIMIT 1")
    assert op is not None
    payload = json.loads(op["payload"])
    assert payload["model"]["centroid"], "the centroid replicates so other devices can verify offline"
    assert "audio" not in json.dumps(payload).lower() and len(payload["model"]["centroid"]) < 200
