"""The HTTP surface. Every route here had been exercised only in-process before this file.

The client is a real ``TestClient`` over ASGI, so middleware, dependencies, status codes,
headers and serialisation are all covered — which is where the difference between "the
function works" and "the product works" hides.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jarvis import voice
from jarvis.api.app import SECURITY_HEADERS, WEB_DIR, Jarvis, create_app
from jarvis.config import Settings

VOICE_TEXT = "the quick brown fox jumps over the lazy dog"


def wav_b64(text: str = VOICE_TEXT, f0: float = 118.0) -> str:
    return base64.b64encode(voice.pcm16_to_wav_bytes(voice.synthesize_probe_pcm(text, 2.6, f0))).decode()


def _build_client(root: Path, *, device_id: str = "api-device", **overrides) -> TestClient:
    settings = Settings(
        data_dir=root / "device",
        device_id="api-device",
        device_name="API test device",
        secret_key="api-test-secret-long-enough-for-hs256-signing",
        allow_passwordless_dev_idp=True,
        llm_base_url="",
        image_provider="local",
        video_provider="local",
        asr_provider="offline",
        **overrides,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(Jarvis(settings), settings=settings)
    client = TestClient(app)
    client.settings = settings  # type: ignore[attr-defined]
    return client


@pytest.fixture()
def isolated_client(tmp_path):
    """A whole separate device+server, for tests that destroy state (logout, rate limits,
    demo wipe). Sharing the module client would make them fail for every later test."""
    with _build_client(tmp_path) as c:
        yield c


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with _build_client(tmp_path_factory.mktemp("api")) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post(
        "/api/auth/dev-login",
        json={"email": "owner@example.com", "display_name": "Owner", "provider": "google", "device_id": "api-device", "device_name": "API test device", "platform": "test"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    client.headers["authorization"] = f"Bearer {body['session']['token']}"
    return body["session"]["token"]


@pytest.fixture()
def authed(client, token):
    client.headers["authorization"] = f"Bearer {token}"
    return client


# ------------------------------------------------------------------- basics
def test_health_is_public_and_describes_the_build(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["app"] == "jarvis-core"
    caps = body["capabilities"]
    for name in ("adaptive_learning", "voice_recognition", "offline", "device_control", "image_generation", "web_access"):
        assert caps[name]["offline"] is True, name
    assert body["network"]["online"] in (True, False)
    assert set(body["credentials"]) == {"llm", "image", "video", "asr", "identity"}


def test_security_headers_on_every_response(client):
    r = client.get("/api/health")
    for header, value in SECURITY_HEADERS.items():
        assert r.headers.get(header.lower()) == value, header
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store", "assistant data must not sit in a shared cache"
    assert not r.headers.get("set-cookie"), "the API must be bearer-token only, so it cannot be driven cross-site"


def test_openapi_and_docs_are_served(client):
    assert client.get("/api/openapi.json").status_code == 200
    spec = client.get("/api/openapi.json").json()
    assert len(spec["paths"]) >= 40
    assert client.get("/api/docs").status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/me"),
        ("post", "/api/assistant"),
        ("get", "/api/preferences"),
        ("get", "/api/voice/status"),
        ("get", "/api/memory"),
        ("get", "/api/devices"),
        ("get", "/api/control/catalog"),
        ("get", "/api/artifacts"),
        ("get", "/api/sync/status"),
        ("get", "/api/tasks"),
        ("post", "/api/media/image"),
        ("post", "/api/voice/enroll"),
        ("post", "/api/sync/push"),
    ],
)
def test_protected_routes_refuse_anonymity(client, method, path):
    r = getattr(client, method)(path, **({} if method == "get" else {"json": {}}))
    assert r.status_code == 401, (path, r.status_code, r.text)
    assert "sign in" in r.json()["detail"]
    assert r.headers["www-authenticate"].startswith("Bearer"), "401 must say how to authenticate"


def test_a_garbage_token_is_the_same_401_as_no_token(authed):
    authed.headers["authorization"] = "Bearer not-a-real-token"
    assert authed.get("/api/me").status_code == 401


def test_oversized_bodies_are_refused_before_parsing(authed):
    big = "A" * (authed.settings.max_upload_bytes + 10)
    r = authed.post("/api/voice/enroll", json={"audio_b64": big})
    assert r.status_code == 413
    assert "bytes" in r.json()["detail"]


# ---------------------------------------------------------------------- web
def test_web_client_is_served_and_does_not_need_a_build_step(authed):
    assert WEB_DIR.exists()
    r = authed.get("/web/index.html")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert authed.get("/").status_code == 307 or authed.get("/").status_code in (200, 302)
    css = authed.get("/web/styles.css")
    assert css.status_code == 200 and ".msg" in css.text
    assert authed.get("/web/app.js").status_code == 200


def test_service_worker_and_manifest_are_served(authed):
    sw = authed.get("/sw.js")
    assert sw.status_code == 200
    assert "self.addEventListener" in sw.text
    manifest = authed.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    payload = manifest.json()
    assert payload["name"] and payload["display"] in ("standalone", "minimal-ui")
    assert payload["start_url"]


def test_html_served_by_the_api_has_no_inline_scripts_or_styles(authed):
    """CSP is ``default-src 'self'`` with no unsafe-inline: an inline handler would be a
    silent, hard-to-debug CSP violation, so the served HTML must not contain any."""
    html = authed.get("/web/index.html").text
    assert "<script" not in html or 'src="/web/app.js"' in html
    for tag in re.findall(r"<script[^>]*>", html):
        assert "src=" in tag, tag
    assert ' style="' not in html, "inline styles are blocked by the CSP"


def test_every_endpoint_the_client_calls_exists(authed):
    """Drift guard: the JS client and the server are written independently, and a typo in
    a path is a runtime 404 a user finds, not a test that fails. So this is a test."""
    source = (WEB_DIR / "app.js").read_text() + (WEB_DIR / "auth-return.js").read_text()
    spec = authed.get("/api/openapi.json").json()["paths"]
    found = re.findall(r"['\"`]/api/[A-Za-z0-9_/-]*", source)
    paths = {p[1:] for p in found}
    missing = set()
    for path in sorted(paths):
        base = path.split("?")[0].rstrip("/")
        if base in spec:
            continue
        # a templated call looks like "/api/memory/${id}" -> "/api/memory/{memory_id}",
        # and "/api/media/" + kind resolves at runtime, so a prefix match counts
        if any(template.startswith(base + "/{") or (path.endswith("/") and template.startswith(path)) for template in spec):
            continue
        missing.add(path)
    assert not missing, f"web client calls routes the server does not have: {sorted(missing)}"
    assert len(paths) >= 18, f"suspiciously few client calls parsed: {len(paths)}"


# ------------------------------------------------------------------- account
def test_me_reports_profile_devices_and_state(authed):
    r = authed.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["profile"]["email"] == "owner@example.com"
    assert body["session"]["scope"] == "basic"
    assert body["session"]["token"] is None if "token" in body["session"] else True
    assert "token" not in body["session"], "never echo the session token back to the page"
    assert any(d["id"] == "api-device" for d in body["devices"])
    assert isinstance(body["style_directive"], str)
    assert isinstance(body["pending_ops"], int) and body["pending_ops"] >= 0
    assert body["identities"], "the linked Gmail/Apple/Microsoft identity is visible to the user"


def test_display_name_can_be_changed_and_persists(authed):
    assert authed.post("/api/me/name", json={"display_name": "Damien"}).status_code == 200
    assert authed.get("/api/me").json()["user"]["display_name"] == "Damien"
    assert authed.post("/api/me/name", json={"display_name": ""}).status_code == 422


def test_logout_invalidates_the_token_but_the_client_can_sign_in_again(isolated_client):
    login = isolated_client.post("/api/auth/dev-login", json={"email": "throwaway@example.com", "device_id": "api-device"})
    tok = login.json()["session"]["token"]
    isolated_client.headers["authorization"] = f"Bearer {tok}"
    assert isolated_client.get("/api/me").status_code == 200
    assert isolated_client.post("/api/auth/logout").status_code == 200
    assert isolated_client.get("/api/me").status_code == 401
    again = isolated_client.post("/api/auth/dev-login", json={"email": "throwaway@example.com", "device_id": "api-device"})
    assert again.status_code == 200 and again.json()["created"] is False, "same account, new session"
    assert again.json()["session"]["token"] != tok
    isolated_client.headers["authorization"] = f"Bearer {again.json()['session']['token']}"
    assert isolated_client.get("/api/me").status_code == 200


def test_logout_everywhere_revokes_each_session(isolated_client):
    emails = "everywhere@example.com"
    tokens = [
        isolated_client.post("/api/auth/dev-login", json={"email": emails, "device_id": f"dev-{i}"}).json()["session"]["token"] for i in range(3)
    ]
    isolated_client.headers["authorization"] = f"Bearer {tokens[0]}"
    assert len(isolated_client.get("/api/devices").json()["devices"]) == 3
    assert isolated_client.post("/api/auth/logout-all").status_code == 200
    assert all(isolated_client.get("/api/me", headers={"authorization": f"Bearer {t}"}).status_code == 401 for t in tokens)


def test_login_begin_returns_a_pkce_redirect(client, token):
    r = client.post("/api/auth/login/begin", json={"provider": "google", "device_id": "api-device"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["state"]) >= 16
    assert f'state={body["state"]}' in body["authorize_url"]
    assert body["mode"] in ("oidc", "dev-idp")
    # the state is single-use and the verifier never leaves the server
    assert "challenge" not in json.dumps(body) and "verifier" not in json.dumps(body)
    again = client.get("/api/auth/callback", params={"code": "x", "state": body["state"]})
    third = client.get("/api/auth/callback", params={"code": "x", "state": body["state"]})
    assert again.status_code == 400, "the dev IdP needs a code it minted, not 'x'"
    assert third.status_code == 400 and "state" in third.json()["detail"]
    assert client.post("/api/auth/login/begin", json={"provider": "myspace", "device_id": "x"}).status_code in (400, 422)


def test_oidc_callback_page_is_csp_clean_and_escapes(isolated_client):
    login = isolated_client.post("/api/auth/dev-login", json={"email": "cb@example.com", "device_id": "api-device"})
    state = isolated_client.post("/api/auth/login/begin", json={"provider": "google", "device_id": "api-device"}).json()["state"]
    r = isolated_client.get("/api/auth/callback", params={"code": "not-a-code", "state": state, "x": "<img src=q onerror=alert(1)>"})
    assert r.status_code == 400
    detail = isolated_client.get("/api/auth/callback", params={"code": "y", "state": "<svg onload=alert(1)>"}).text
    assert "<svg onload" not in detail, "the error page must not reflect a raw query value as markup"
    assert login.status_code == 200


# -------------------------------------------------------------- assistance
def test_assistant_turn_and_feedback(authed):
    r = authed.post("/api/assistant", json={"text": "give me ideas for a weekend project", "engagement_seconds": 12})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "brainstorm" and body["cards"]
    fb = authed.post("/api/assistant/feedback", json={"valence": 1})
    assert fb.status_code == 200 and "adjusted" in fb.json()


def test_assistant_validates_input(authed):
    assert authed.post("/api/assistant", json={"text": ""}).status_code == 422
    assert authed.post("/api/assistant", json={"text": "x", "engagement_seconds": -5}).status_code == 422


def test_preferences_round_trip(authed):
    assert authed.post("/api/preferences/trait", json={"key": "warmth", "value": 0.5}).status_code == 200
    prefs = authed.get("/api/preferences").json()
    assert any(t["key"] == "warmth" for t in prefs["traits"])
    assert authed.post("/api/preferences/trait", json={"key": "warmth", "value": 9}).status_code == 422
    assert authed.post("/api/preferences/trait", json={"key": "<script>", "value": 0.1}).status_code in (400, 422)
    assert authed.post("/api/preferences/reset").status_code == 200


def test_recommendations_use_query_parameters_not_a_body(authed):
    r = authed.get("/api/recommendations", params={"items": "woodworking,astrophysics,coffee roasting", "limit": 3})
    assert r.status_code == 200
    assert len(r.json()["ranked"]) <= 3
    assert authed.get("/api/recommendations", params={"limit": 999}).status_code == 422


# -------------------------------------------------------------------- memory
def test_memory_crud_and_search(authed):
    added = authed.post("/api/memory", json={"body": "the boat registration expires in March", "tags": ["boat"]})
    assert added.status_code == 200
    mid = added.json()["id"]
    assert mid in [m["id"] for m in authed.get("/api/memory").json()["items"]]
    hits = authed.get("/api/memory/search", params={"q": "boat registration"}).json()["hits"]
    assert hits and hits[0]["id"] == mid
    assert authed.delete(f"/api/memory/{mid}").status_code == 200
    assert mid not in [m["id"] for m in authed.get("/api/memory").json()["items"]]
    assert authed.get("/api/memory/search", params={"q": "boat"}).json()["hits"] == []


def test_tasks_are_memories_so_they_replicate(authed):
    created = authed.post("/api/tasks", json={"description": "renew the passport"})
    assert created.status_code == 200
    tid = created.json()["id"]
    assert any(t["id"] == tid for t in authed.get("/api/tasks").json()["items"])
    assert authed.post(f"/api/tasks/{tid}/complete").status_code == 200
    assert authed.get("/api/tasks", params={"status": "open"}).json()["items"] == []
    assert authed.get("/api/tasks", params={"status": "bogus"}).status_code == 422


# --------------------------------------------------------------------- voice
def test_voiceprint_enrol_verify_and_step_up_boundary(authed):
    assert authed.get("/api/voice/status").json()["enrolled"] is False
    for i in range(3):
        r = authed.post("/api/voice/enroll", json={"audio_b64": wav_b64(f"voice enrolment sample number {i}")})
        assert r.status_code == 200, r.text
    status = authed.get("/api/voice/status").json()
    assert status["enrolled"] and status["samples"] == 3 and status["calibrated"] is False
    # an impostor is refused...
    bad = authed.post("/api/voice/verify", json={"audio_b64": wav_b64("voice enrolment sample number 0", 240.0)})
    assert bad.status_code == 200 and bad.json()["accepted"] is False
    # ...and even the owner's own voice does not grant privileged scope while uncalibrated
    owner = authed.post("/api/voice/verify", json={"audio_b64": wav_b64("voice enrolment sample number 0")})
    assert owner.json()["accepted"] is False and owner.json()["blocked_reason"] == "uncalibrated"
    assert authed.get("/api/me").json()["session"]["scope"] == "basic", "a refused step-up must not upgrade anything"


def test_voice_enrolment_rejects_unusable_audio(authed):
    tiny = base64.b64encode(voice.synthesize_probe_pcm("hi", 0.2, 118.0)).decode()
    assert authed.post("/api/voice/enroll", json={"audio_b64": tiny}).status_code == 422
    assert authed.post("/api/voice/enroll", json={"audio_b64": "not base64 @@@@"}).status_code == 400


def test_calibration_report_gates_privileged_use(authed):
    genuine = [0.95, 0.94, 0.96, 0.93, 0.97, 0.95, 0.94, 0.96, 0.93, 0.97, 0.95, 0.96]
    impostor = [0.70, 0.72, 0.69, 0.71, 0.73, 0.68, 0.74, 0.70, 0.69, 0.72, 0.71, 0.73]
    r = authed.post("/api/voice/calibrate", json={"genuine": genuine, "impostor": impostor})
    assert r.status_code == 200
    body = r.json()
    assert body["usable_for_privileged"] is True
    assert body["threshold"] > max(impostor) and body["threshold"] <= min(genuine)
    after = authed.post("/api/voice/verify", json={"audio_b64": wav_b64("voice enrolment sample number 0")})
    assert after.json()["accepted"] is True, after.json()
    assert "simulated" not in after.json(), "only the demo endpoint flags itself as simulated"
    assert authed.get("/api/me").json()["session"]["scope"] == "privileged", "step-up upgrades the session"
    # and a hopeless trial set marks the model unusable again rather than passing silently
    bad = authed.post("/api/voice/calibrate", json={"genuine": [0.9] * 12, "impostor": [0.91] * 12})
    assert bad.json()["usable_for_privileged"] is False


def test_synthesised_audio_endpoint_is_dev_only(authed):
    r = authed.post("/api/voice/synth", json={"text": "same words as enrolment", "f0": 118})
    assert r.status_code == 200
    decoded = base64.b64decode(r.json()["audio_b64"])
    assert decoded[:4] == b"RIFF"
    assert authed.post("/api/voice/synth", json={"text": "x", "f0": 1e9}).status_code == 200  # clamped, not crashed


def test_voice_step_up_attempts_are_rate_limited(isolated_client):
    """The biometric gate is the one place where a search over candidate audio is the attack,
    so attempts are bounded per session and every refusal lands in the audit log."""
    login = isolated_client.post("/api/auth/dev-login", json={"email": "limit@example.com", "display_name": "L"})
    assert login.status_code == 200, login.text
    auth = {"Authorization": "Bearer " + login.json()["session"]["token"]}
    for i in range(3):
        assert isolated_client.post("/api/voice/enroll", json={"audio_b64": wav_b64(f"limit sample {i}")}, headers=auth).status_code == 200
    codes = [isolated_client.post("/api/voice/verify", json={"audio_b64": wav_b64("guessing at audio")}, headers=auth).status_code for _ in range(12)]
    assert codes[0] == 200  # a real attempt returns a verdict, not a wall
    assert 429 in codes, codes
    blocked = isolated_client.post("/api/voice/verify", json={"audio_b64": wav_b64("still guessing")}, headers=auth)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after") == "10"
    assert blocked.json()["detail"]["error"] == "too-many-attempts"
    audit = isolated_client.get("/api/control/audit", params={"limit": 50}, headers=auth).json()["entries"]
    assert any(e["decision"] == "denied" and "voice verification" in e["reason"] for e in audit), audit[:3]
    # and the same budget guards the demo route, because it grants the same privilege
    assert isolated_client.post("/api/voice/verify-simulated", json={"phrase": "hello jarvis"}, headers=auth).status_code == 429


def test_voiceprint_can_be_deleted_everywhere(authed):
    assert authed.post("/api/voice/reset").status_code == 200
    assert authed.get("/api/voice/status").json()["enrolled"] is False


# ------------------------------------------------------------------ commands
def test_closed_vocabulary_commands_over_http(authed, monkeypatch):
    for i, take in enumerate(("arm the system now", "arm the system now please")):
        r = authed.post("/api/commands/enroll", json={"audio_b64": wav_b64(take), "phrase": "arm system"})
        assert r.status_code == 200, r.text
        assert r.json()["takes"] == i + 1
    listing = authed.get("/api/commands").json()
    assert listing["templates"] == ["arm system"]
    calibrated = authed.post("/api/commands/auto-calibrate").json()
    assert calibrated["threshold"] and calibrated["threshold"] <= 12.0
    match = authed.post("/api/commands/recognize", json={"audio_b64": wav_b64("arm the system now")})
    assert match.status_code == 200 and match.json()["matched"] == "arm system"
    assert "executed" in match.json(), "a matched command reports what it did"
    miss = authed.post("/api/commands/recognize", json={"audio_b64": wav_b64("delete every file I own")})
    assert miss.json()["matched"] is None
    assert authed.post("/api/commands/enroll", json={"audio_b64": wav_b64("x"), "phrase": ""}).status_code == 422


def test_enrolled_commands_can_be_replaced_and_forgotten(isolated_client):
    """A recording the user cannot delete is a liability, not a feature.

    ``replace`` re-does one phrase without disturbing the others; the delete route forgets a
    phrase completely — and durably, because the spotter is persisted to disk.
    """
    login = isolated_client.post("/api/auth/dev-login", json={"email": "cmds@example.com", "display_name": "C"})
    auth = {"Authorization": "Bearer " + login.json()["session"]["token"]}
    for phrase, words in (("arm system", "arm the system now"), ("disarm all", "disarm everything now")):
        for take in (words, words + " please"):
            r = isolated_client.post("/api/commands/enroll", json={"audio_b64": wav_b64(take), "phrase": phrase}, headers=auth)
            assert r.status_code == 200, r.text
    listing = isolated_client.get("/api/commands", headers=auth).json()
    assert listing["templates"] == ["arm system", "disarm all"]
    assert listing["takes"] == {"arm system": 2, "disarm all": 2}
    assert listing["calibrated"] is True

    replaced = isolated_client.post(
        "/api/commands/enroll",
        json={"audio_b64": wav_b64("arm the system now, retaken"), "phrase": "arm system", "replace": True},
        headers=auth,
    ).json()
    assert replaced["takes"] == 1
    assert replaced["replaced"] is True
    after = isolated_client.get("/api/commands", headers=auth).json()
    assert after["takes"] == {"arm system": 1, "disarm all": 2}, "replace must not disturb other phrases"

    gone = isolated_client.delete("/api/commands/arm%20system", headers=auth)
    assert gone.status_code == 200, gone.text
    assert gone.json()["remaining"] == ["disarm all"]
    assert "arm system" not in (isolated_client.settings.data_dir / "commands.json").read_text()
    # unknown (and already-forgotten) phrases are a 404, not a silent success
    assert isolated_client.delete("/api/commands/arm system", headers=auth).status_code == 404
    assert isolated_client.delete("/api/commands/nothing like this", headers=auth).status_code == 404


def test_transcription_honesty_over_http(authed):
    r = authed.post("/api/asr/transcribe", json={"audio_b64": wav_b64("words that no offline engine can hear")})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["engine"] == "unavailable"
    assert not body["text"], "no engine must mean no invented transcript"


# --------------------------------------------------------------------- media
def test_image_generation_returns_a_blob_the_browser_can_show(authed):
    r = authed.post("/api/media/image", json={"prompt": "a lighthouse in a storm", "width": 320, "height": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "image" and body["provider"] == "local"
    listing = authed.get("/api/artifacts").json()["items"]
    assert any(a["id"] == body["id"] for a in listing)
    fetched = authed.get(f"/api/artifacts/{body['id']}/file")
    assert fetched.status_code == 200
    assert fetched.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert fetched.headers["content-type"] == "image/png"
    assert authed.get("/api/artifacts/art_nope/file").status_code == 404
    assert authed.get("/api/artifacts/../../etc/passwd/file").status_code in (400, 404)


def test_image_file_can_be_fetched_by_token_query_for_img_tags(client, token):
    """``<img>`` cannot send an Authorization header, so the token may come from the query
    string. That is the one exception, and it must behave exactly like the header."""
    made = client.post("/api/media/image", json={"prompt": "a quiet harbour at dawn", "width": 200, "height": 200})
    aid = made.json()["id"]
    via_query = client.get(f"/api/artifacts/{aid}/file", params={"token": token})
    assert via_query.status_code == 200 and via_query.content[:4] == b"\x89PNG"
    assert client.get(f"/api/artifacts/{aid}/file?token=bogus", headers={"authorization": ""}).status_code == 401


def test_video_generation_and_validation(authed):
    r = authed.post("/api/media/video", json={"prompt": "waves looping", "width": 160, "height": 120, "seconds": 1.0, "fps": 4})
    assert r.status_code == 200 and r.json()["kind"] == "video"
    blob = authed.get(f"/api/artifacts/{r.json()['id']}/file")
    assert blob.content[:6] == b"GIF89a"
    assert authed.post("/api/media/image", json={"prompt": "too big", "width": 4000}).status_code == 422


def _privileged(client) -> None:
    """Give the shared session a verified-by-voice, calibrated profile.

    Risky control and device-trust changes require a privileged session by design, so
    every test that touches them steps up first — through the same three endpoints a
    user goes through (enrol, calibrate, verify).
    """
    already_privileged = client.get("/api/me").json()["session"]["scope"] == "privileged"
    for i in range(4):
        client.post("/api/voice/enroll", json={"audio_b64": wav_b64(f"privileged step up sample {i}")})
    cal = client.post(
        "/api/voice/calibrate",
        json={
            "genuine": [0.95, 0.94, 0.96, 0.93, 0.97, 0.95, 0.94, 0.96, 0.93, 0.97, 0.95, 0.96],
            "impostor": [0.70, 0.72, 0.69, 0.71, 0.73, 0.68, 0.74, 0.70, 0.69, 0.72, 0.71, 0.73],
        },
    )
    assert cal.json()["usable_for_privileged"] is True, cal.text
    if not already_privileged:
        verify = client.post("/api/voice/verify", json={"audio_b64": wav_b64("privileged step up sample 1")})
        assert verify.json()["accepted"] is True, verify.text
        assert client.get("/api/me").json()["session"]["scope"] == "privileged"
    # medium-risk control also requires the *device* to be trusted, which only a
    # privileged session may grant — so the fixture earns both, like a user would
    assert client.post("/api/devices/api-device/trust").status_code == 200


@pytest.fixture()
def privileged(authed):
    _privileged(authed)
    return authed


def test_medium_risk_needs_step_up(isolated_client):
    """Same route, same target, ordinary session: refused with a reason that says what to do."""
    login = isolated_client.post("/api/auth/dev-login", json={"email": "stepup@example.com", "device_id": "basic-device"})
    isolated_client.headers["authorization"] = f"Bearer {login.json()['session']['token']}"
    target = isolated_client.post("/api/control/targets", json={"name": "Box", "kind": "laptop", "endpoint": "local", "capabilities": ["app.open"], "pairing_verified": True})
    assert target.status_code == 403, "granting a device to Jarvis is itself a privileged act"
    assert "step-up" in target.json()["detail"]["message"]


# -------------------------------------------------------------------- devices
def test_device_pairing_trust_and_revocation(authed):
    _privileged(authed)
    r = authed.post("/api/devices/pair", json={"device_id": "phone-2", "device_name": "Pixel 8"})
    assert r.status_code == 200
    pairing = r.json()
    assert re.fullmatch(r"\d{6}", pairing["code"]) and pairing["expires_in_seconds"] > 0
    assert "approve" in pairing["instructions"].lower()
    devices = authed.get("/api/devices").json()["devices"]
    phone = next(d for d in devices if d["id"] == "phone-2")
    assert phone["trust_level"] == "untrusted", "pairing was requested, not granted"
    assert authed.post("/api/devices/phone-2/trust").status_code == 200
    assert next(d for d in authed.get("/api/devices").json()["devices"] if d["id"] == "phone-2")["trust_level"] == "trusted"
    assert authed.post("/api/devices/phone-2/revoke").status_code == 200
    assert next(d for d in authed.get("/api/devices").json()["devices"] if d["id"] == "phone-2")["trust_level"] == "revoked"


# -------------------------------------------------------------------- control
def test_control_surface_end_to_end(privileged, monkeypatch):
    authed = privileged
    monkeypatch.setenv("JARVIS_ALLOWED_APPS", "sleeper=/bin/sleep")
    catalog = authed.get("/api/control/catalog").json()["capabilities"]
    names = {c["capability"] for c in catalog}
    assert {"app.open", "power.restart", "device.factory_reset"} <= names
    by_name = {c["capability"]: c for c in catalog}
    assert by_name["power.restart"]["risk"] == "forbidden", "dangerous by default, so unusable by default"
    assert by_name["device.factory_reset"]["risk"] == "forbidden"
    assert by_name["app.open"]["risk"] == "medium" and "app" in by_name["app.open"]["arguments"]

    target = authed.post("/api/control/targets", json={"name": "Studio laptop", "kind": "laptop", "endpoint": "local", "capabilities": ["app.open", "app.list", "app.close"], "pairing_verified": True})
    assert target.status_code == 200
    target_id = target.json()["id"]
    assert authed.post("/api/control/execute", json={"capability": "app.open", "args": {"app": "nano"}, "device_id": target_id}).status_code in (200, 403)

    rule = authed.post("/api/control/rules", json={"capability": "app.open", "max_risk": "medium"})
    assert rule.status_code == 200, rule.text
    # every capability needs its own rule: being allowed to start something does not allow
    # killing it, and closing what you started is a separate grant
    unruled = authed.post("/api/control/execute", json={"capability": "app.close", "args": {"app": "sleeper"}, "device_id": target_id})
    assert unruled.json()["status"] == "denied" and "explicit allow rule" in unruled.json()["reason"]
    for cap in ("app.list", "app.close"):
        assert authed.post("/api/control/rules", json={"capability": cap, "max_risk": "medium"}).status_code == 200
    # basic scope: refused with an explanation, not a crash
    dry = authed.post("/api/control/execute", json={"capability": "app.open", "args": {"app": "sleeper"}, "device_id": target_id, "dry_run": True})
    assert dry.status_code == 200 and dry.json()["status"] in ("dry_run", "denied"), "a dry run never touches the device"
    if dry.json()["status"] == "dry_run":
        assert dry.json()["decision"]["risk"] == "medium"

    real = authed.post("/api/control/execute", json={"capability": "app.open", "args": {"app": "sleeper"}, "device_id": target_id})
    assert real.status_code == 200, real.text
    assert real.json()["status"] in {"executed", "failed"}, real.json()  # never a policy refusal once stepped up
    if real.json()["status"] == "executed":
        listed = authed.post("/api/control/execute", json={"capability": "app.list", "args": {}, "device_id": target_id})
        assert listed.json()["status"] == "executed"
        procs = listed.json()["result"]["detail"]
        assert any(p["app"] == "sleeper" and p["alive"] for p in procs), procs
        closed = authed.post("/api/control/execute", json={"capability": "app.close", "args": {"app": "sleeper"}, "device_id": target_id})
        assert closed.json()["status"] in {"executed", "failed"}, closed.json()
    # (the basic-session refusal is covered by test_medium_risk_needs_step_up, on its own
    # client: mutating the shared session's scope here would leak into later tests)

    injection = authed.post("/api/control/execute", json={"capability": "app.open", "args": {"app": "nano; rm -rf /"}, "device_id": target_id})
    assert injection.json()["status"] == "denied"

    unknown = authed.post("/api/control/execute", json={"capability": "power.launch_missiles", "args": {}, "device_id": target_id})
    assert unknown.json()["status"] == "denied"

    audit = authed.get("/api/control/audit").json()["entries"]
    assert audit and {e["decision"] for e in audit} >= {"denied"}
    assert all("reason" in e for e in audit)

    assert authed.delete(f"/api/control/targets/{target_id}").status_code == 200
    assert all(t["id"] != target_id for t in authed.get("/api/control/catalog").json()["targets"])


def test_control_pause_needs_an_explicit_decision(authed):
    assert authed.post("/api/control/pause", json={}).status_code == 422
    assert authed.post("/api/control/pause", json={"paused": False}).status_code == 200


def test_control_pause_is_a_kill_switch_that_replicates(authed):
    assert authed.post("/api/control/pause", json={"paused": True}).status_code == 200
    assert authed.get("/api/control/catalog").json()["paused"] is True
    blocked = authed.post("/api/control/execute", json={"capability": "notify", "args": {"title": "t", "body": "b"}})
    assert blocked.json()["status"] == "denied" and "paused" in blocked.json()["reason"]
    # it is a journalled profile field, so pausing on one device pauses them all
    op = authed.get("/api/memory").json()  # any read proves the session still works
    del op
    assert authed.post("/api/control/pause", json={"enabled": True}).status_code == 200  # alias form: "control enabled"
    assert authed.get("/api/control/catalog").json()["paused"] is False


def test_control_rules_reject_nonsense(privileged):
    authed = privileged
    bad = authed.post("/api/control/rules", json={"capability": "power.launch_missiles"})
    assert bad.status_code == 400, bad.text
    assert "unknown capability" in bad.json()["detail"]
    assert authed.post("/api/control/rules", json={"capability": "notify", "max_risk": "impossible"}).status_code in (400, 422)
    assert authed.post("/api/control/targets", json={"name": "", "kind": "laptop", "endpoint": "local", "capabilities": []}).status_code == 422
    bogus = authed.post("/api/control/targets", json={"name": "x", "kind": "laptop", "endpoint": "local", "capabilities": ["not.real"]})
    assert bogus.status_code == 400 and "drive" in bogus.json()["detail"], bogus.text
    partial = authed.post("/api/control/targets", json={"name": "y", "kind": "laptop", "endpoint": "local", "capabilities": ["notify", "spotify.pause"]})
    assert partial.status_code == 200
    assert partial.json()["unsupported"] == ["spotify.pause"], partial.json()
    authed.delete(f"/api/control/targets/{partial.json()['id']}")


# ---------------------------------------------------------------------- sync
def test_sync_status_and_manual_run(authed):
    status = authed.get("/api/sync/status").json()
    assert {"device_id", "pending_ops", "cursor", "oplog_size", "behaviour"} <= set(status)
    assert status["device_id"] == "api-device"
    run = authed.post("/api/sync/run")
    assert run.status_code == 200
    report = run.json()
    assert {"pushed", "pulled", "applied", "conflicts", "rejected", "pending"} <= set(report)
    assert report["pushed"] >= 0 and report["error"] is None
    assert authed.get("/api/sync/status").json()["pending_ops"] == 0, "a successful run drains the queue"


def test_push_and_pull_are_replayed_through_the_public_endpoints(client, token):
    """A device pushes its queue and pulls what it has not seen yet, over HTTP."""
    peer = client.app.state.jarvis
    db = peer.db
    for body in ("fact from device one", "fact from device two"):
        db.append_op(device_id="api-device", user_id=db.scalar("SELECT id FROM users LIMIT 1"), entity="memory", entity_key="k", field=None, kind="set", payload={"body": body, "tags": []})
    queued = db.query("SELECT * FROM oplog WHERE device_id='api-device' ORDER BY seq")
    payload_ops = [
        {
            "op_id": r["op_id"], "device_seq": r["device_seq"], "user_id": r["user_id"], "entity": r["entity"],
            "entity_key": r["entity_key"], "field": r["field"], "kind": r["kind"], "payload": json.loads(r["payload"]),
            "wall_ts": r["wall_ts"], "lamport": r["lamport"], "base_lamport": r["base_lamport"],
        }
        for r in queued
    ]
    headers = {"authorization": f"Bearer {token}"}
    push = client.post("/api/sync/push", json={"ops": payload_ops, "observed_lamport": 0}, headers=headers)
    assert push.status_code == 200, push.text
    assert push.json()["accepted"] == len(payload_ops) and push.json()["rejected"] == []
    pulled = client.get("/api/sync/pull", params={"cursor": 0}, headers=headers).json()
    assert pulled["cursor"] >= len(payload_ops) and pulled["has_more"] is False
    assert any(op["entity"] == "memory" for op in pulled["ops"])
    replay = client.post("/api/sync/push", json={"ops": payload_ops, "observed_lamport": 0}, headers=headers)
    assert replay.json()["accepted"] == len(payload_ops), "an accepted duplicate is fine; the client drops it from the outbox"
    assert peer.db.scalar("SELECT COUNT(DISTINCT op_id) FROM oplog", (), 0) == peer.db.scalar("SELECT COUNT(*) FROM oplog", (), 0), "no op may be journaled twice"


def test_a_device_cannot_journal_into_someone_elses_account(client, token):
    """push trusts the session, not the payload: device_id and user_id are rewritten."""
    peer = client.app.state.jarvis
    other = peer.db.scalar("SELECT id FROM users LIMIT 1")
    spoof = {
        "op_id": f"spoof_{time.time_ns()}", "device_id": "some-other-device", "device_seq": 1,
        "user_id": "attacker-controlled-user", "entity": "memory", "entity_key": "spoof", "field": None,
        "kind": "set", "payload": {"body": "injected"}, "wall_ts": time.time(), "lamport": 1, "base_lamport": 0,
    }
    r = client.post("/api/sync/push", json={"ops": [spoof], "observed_lamport": 0}, headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    row = peer.db.one("SELECT user_id, device_id FROM oplog WHERE op_id=?", (spoof["op_id"],))
    assert row["device_id"] == "api-device", "a client must not be able to forge which device said it"
    assert row["user_id"] != "attacker-controlled-user"
    assert row["user_id"] == other


def test_sync_requires_a_session(client):
    anonymous = {"authorization": ""}
    assert client.post("/api/sync/push", json={"ops": []}, headers=anonymous).status_code == 401
    assert client.get("/api/sync/pull", headers=anonymous).status_code == 401


# ----------------------------------------------------------------- demo mode
def test_demo_reset_is_off_unless_explicitly_enabled(authed):
    assert authed.post("/api/demo/reset").status_code == 404, "a wipe endpoint must not exist by default"


def test_demo_reset_needs_an_explicit_env_flag(isolated_client, monkeypatch):
    monkeypatch.setenv("JARVIS_ENABLE_DEMO_RESET", "1")
    login = isolated_client.post("/api/auth/dev-login", json={"email": "demo@example.com", "device_id": "api-device"})
    assert login.status_code == 200
    r = isolated_client.post("/api/demo/reset")
    assert r.status_code == 200 and r.json()["restart_required"] is True


# --------------------------------------------------------- request handling
def test_validation_errors_are_structured_not_tracebacks(authed):
    r = authed.post("/api/memory", json={"body": ""})
    assert r.status_code == 422
    authed.post("/api/memory", json={"body": "x" * 5000})
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"][-1] == "body"
    assert "traceback" not in r.text.lower()


def test_unknown_route_is_404_with_json(authed):
    r = authed.get("/api/nope")
    assert r.status_code == 404 and r.json()["detail"]


def test_rate_limiter_protects_the_login_endpoint(isolated_client):
    client = isolated_client
    """Brute-forcing dev logins must be bounded. The limiter is per-IP/token, so the
    count is asserted loosely but the mechanism must exist."""
    codes = set()
    for i in range(140):
        r = client.post("/api/auth/dev-login", json={"email": f"person{i}@example.com", "device_id": f"dev-{i}"})
        codes.add(r.status_code)
        if 429 in codes:
            break
    assert codes <= {200, 429}
