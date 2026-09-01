"""HTTP surface for Jarvis: REST API + the web client, with offline-capable serving.

Authentication model
--------------------
Bearer token in an ``Authorization`` header only — no session cookies. That is a
deliberate choice: without ambient credentials, cross-site request forgery has
nothing to ride on, so no CSRF token machinery is needed (and none to get wrong).
The token is kept in ``localStorage`` by the web client and in a file by the CLI.

Auth is scoped per request:
``basic`` covers reading your own data and low-risk control;
``privileged`` is required for medium/high-risk device control and is only granted
by step-up verification (see :meth:`jarvis.auth.AuthService.verify_voice_step_up`).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field

from jarvis import voice as voice_mod
from jarvis.accounts import AccountStore
from jarvis.adaptive import TRAIT_LABELS, AdaptiveModel
from jarvis.assistant import Assistant, LLMClient
from jarvis.auth import AuthError, AuthService
from jarvis.config import Settings, get_settings
from jarvis.db import Database
from jarvis.devctl import CATALOG, RISK_ORDER, ControlService
from jarvis.media import MediaService
from jarvis.memory import MemoryStore
from jarvis.net import Connectivity
from jarvis.sync import LocalReplicaTransport, SyncClient, SyncServer

REPO_ROOT = Path(__file__).resolve().parents[2]  # the checkout root: jarvis/api/app.py -> jarvis -> root
_WEB_CANDIDATES = [REPO_ROOT / "web", Path(__file__).resolve().parent.parent / "web"]


def _web_dir() -> Path:
    """The app shell ships next to the source tree; an installed wheel may carry it
    inside the package instead. Either is fine, and a missing one is visible in
    ``/api/health`` rather than a silent 404 for every asset."""
    override = os.environ.get("JARVIS_WEB_DIR")
    if override:
        return Path(override)
    for candidate in _WEB_CANDIDATES:
        if candidate.exists():
            return candidate
    return _WEB_CANDIDATES[0]


WEB_DIR = _web_dir()

SECURITY_HEADERS = {
    # no inline scripts; the PWA ships one JS file and one CSS file
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob: data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(self), camera=(), storage-access=()",
    "Cache-Control": "no-store",
}


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = None
    provider: Literal["google", "apple", "microsoft"] = "google"
    device_id: str | None = None
    device_name: str | None = None
    platform: str = "web"


class NameRequest(BaseModel):
    """Typed, so an empty or absurd name is a 422 rather than a silently ignored body.

    ``name`` is accepted as an alias: the first client written against this route sent
    ``name``, the handler read ``display_name``, and the two agreed to disagree with a 200.
    """

    model_config = {"populate_by_name": True}

    display_name: str = Field(min_length=1, max_length=80, validation_alias=AliasChoices("display_name", "name"))


class AssistantRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    engagement_seconds: float | None = Field(default=None, ge=0, le=3600)
    channel: str = "web"


class FeedbackRequest(BaseModel):
    valence: int
    interaction_id: str | None = None
    note: str | None = None


class TraitRequest(BaseModel):
    key: str
    value: float = Field(ge=-1, le=1)


class RememberRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list)


class ControlRequest(BaseModel):
    capability: str
    args: dict[str, Any] = Field(default_factory=dict)
    device_id: str | None = None
    dry_run: bool = False
    confirmation: str | None = None


class RegisterDeviceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = "laptop"
    endpoint: str = "local"
    capabilities: list[str] = Field(default_factory=list)
    pairing_verified: bool = False


class RuleRequest(BaseModel):
    capability: str
    max_risk: str | None = None
    allow: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class GenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    width: int | None = Field(default=None, ge=64, le=2048)
    height: int | None = Field(default=None, ge=64, le=2048)
    style: str = "poster"
    seconds: float | None = Field(default=None, ge=0.5, le=10)
    fps: int | None = Field(default=None, ge=1, le=24)
    force_offline: bool = False


class AudioRequest(BaseModel):
    """base64 WAV or raw s16le at 16 kHz — browsers can produce the latter without a codec."""

    audio_b64: str = Field(min_length=8, max_length=14_000_000)
    sample_rate: int = 16000


class CommandEnrollRequest(AudioRequest):
    phrase: str = Field(min_length=1, max_length=80)
    # replace=True throws away the previous takes instead of adding another one: a bad
    # recording must be fixable without deleting the whole phrase and re-doing every take
    replace: bool = False


class TaskRequest(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    due: float | None = None


class SyncPushBody(BaseModel):
    ops: list[dict[str, Any]] = Field(default_factory=list)
    observed_lamport: int = 0


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------
class Jarvis:
    """Wires the cores together. One instance per process."""

    def __init__(self, settings: Settings, *, replica_db: Database | None = None):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.replica_db = replica_db or self.db  # the same db acts as the peer replica by default
        self.accounts = AccountStore(self.db, settings)
        self.auth = AuthService(self.db, settings)
        self.media = MediaService(self.db, settings)
        self.control = ControlService(self.db, settings)
        self.connectivity = Connectivity()
        self.sync_transport = LocalReplicaTransport(self.replica_db)
        self._commands: dict[str, voice_mod.VoiceprintModel] = {}
        self.spotter = None

    def spotter_engine(self):
        from jarvis.asr import CommandSpotter

        if self.spotter is None:
            path = self.settings.data_dir / "commands.json"
            self.spotter = CommandSpotter()
            if path.exists():
                try:
                    self.spotter.load(json.loads(path.read_text()))
                except Exception:
                    pass
        return self.spotter

    def save_commands(self) -> None:
        (self.settings.data_dir / "commands.json").write_text(json.dumps(self.spotter_engine().export()))

    def sync_client(self, device_id: str | None = None) -> SyncClient:
        return SyncClient(self.db, device_id or self.settings.device_id, self.sync_transport, batch_limit=self.settings.sync_batch_limit)

    def services_for(self, session: dict[str, Any]) -> dict[str, Any]:
        uid = session["user_id"]
        return {
            "user": self.accounts.get_user(uid),
            "adaptive": AdaptiveModel(self.db, self.settings, uid),
            "memory": MemoryStore(self.db, self.settings, uid),
            "assistant": Assistant(self.db, self.settings, uid),
            "llm": LLMClient(self.settings),
        }

    def status(self, session: dict[str, Any] | None) -> dict[str, Any]:
        state = self.connectivity.state(self.settings)
        creds = self.settings.provider_credentials()
        capabilities = {
            "adaptive_learning": {"offline": True, "label": "learning, style directives, interests"},
            "voice_recognition": {"offline": True, "label": "speaker verification runs locally"},
            "offline": {"offline": True, "label": "local SQLite store + outbox queue"},
            "sync": {"offline": False if not creds else True, "label": "queued while unreachable, converges on reconnect"},
            "cross_device_access": {"offline": not creds["identity"], "label": "google / apple / microsoft sign-in"},
            "device_control": {"offline": True, "label": "local executor; remote targets via adapters"},
            "image_generation": {"offline": True, "label": "procedural PNG locally; model-based when online" if creds["image"] else "procedural PNG (offline renderer)"},
            "video_generation": {"offline": True, "label": "animated GIF locally; mp4 when a provider is set" if creds["video"] else "animated GIF (offline renderer)"},
            "web_access": {"offline": True, "label": "same origin, installable, app shell cached"},
            "dictation": {"offline": bool(state.local_engines.get("asr")), "label": "no local engine — voiceprint and typed input unaffected" if not any(state.local_engines.get(k) for k in ("whisper.cpp", "vosk", "faster_whisper")) else "local engine detected"},
        }
        out = {
            "ok": True,
            "app": "jarvis-core",
            "version": "0.1.0",
            "device_id": self.settings.device_id,
            "network": state.as_dict(),
            "credentials": creds,
            "capabilities": capabilities,
            "pending_ops": int(self.db.scalar("SELECT COUNT(*) FROM outbox", (), 0) or 0),
            "signed_in": session is not None,
            "session_scope": (session or {}).get("scope"),
            "voiceprint": None,
            "sync": None,
        }
        if session:
            uid = session["user_id"]
            out["voiceprint"] = (self.accounts.get_user(uid) or {}).get("voiceprint")
            cursor_row = self.db.one("SELECT last_applied_seq, last_pull_at FROM sync_state WHERE peer='server'")
            out["sync"] = {
                "pending": int(self.db.scalar("SELECT COUNT(*) FROM outbox WHERE device_id=?", (session.get("device_id") or self.settings.device_id,), 0) or 0),
                "cursor": int(cursor_row["last_applied_seq"]) if cursor_row else 0,
                "last_pull_at": cursor_row["last_pull_at"] if cursor_row else None,
                "mode": "in-process replica" if self.replica_db is self.db else "shared replica db",
            }
        return out


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
def create_app(jarvis: Jarvis | None = None, *, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    jarvis = jarvis or Jarvis(settings)
    app = FastAPI(title="Jarvis core", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.jarvis = jarvis

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > settings.max_upload_bytes:
            return JSONResponse({"detail": f"payload larger than {settings.max_upload_bytes} bytes"}, status_code=413)
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if response.status_code == 401:
            # a bare 401 in the browser console reads like a bug; this says what is missing
            response.headers["WWW-Authenticate"] = 'Bearer realm="jarvis", error="invalid_token"'
        return response

    # ------------------------------------------------------------- security
    def session_or_401(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        session = jarvis.auth.authenticate(token) if token else None
        if session is None:
            raise HTTPException(status_code=401, detail="sign in first (Authorization: Bearer <token>)")
        return session

    def privileged_or_403(session: dict[str, Any]) -> None:
        if session.get("scope") != "privileged":
            raise HTTPException(
                status_code=403,
                detail={"error": "step-up-required", "message": "This action needs a step-up verified session. Verify by voice at POST /api/voice/verify."},
            )

    def uid(session: dict[str, Any]) -> str:
        return session["user_id"]

    def guarded(call: Any, *args: Any, **kwargs: Any) -> Any:
        """Policy code signals a bad request with ValueError. Left alone that becomes a
        500 with a traceback in the server log and a useless error in the UI, so every
        route that calls into it converts it to a 400 with the policy's own wording."""
        try:
            return call(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --------------------------------------------------------------- health
    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        session = jarvis.auth.authenticate(token) if token else None
        return jarvis.status(session)

    # ------------------------------------------------------------ auth flow
    @app.post("/api/auth/login/begin")
    def begin_login(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
        provider = payload.get("provider", "google")
        if not jarvis.auth.rate.allow(f"auth:{request.client.host if request else 'local'}", cost=1.0):
            raise HTTPException(429, "too many sign-in attempts; wait a minute")
        try:
            return jarvis.auth.flow.begin(provider, device_id=payload.get("device_id") or settings.device_id)
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/auth/dev-login")
    def dev_login(payload: LoginRequest = Body(...)) -> dict[str, Any]:
        if not jarvis.auth.rate.allow(f"devlogin:{payload.email.lower()}", cost=2.0):
            raise HTTPException(429, "too many attempts")
        if not settings.allow_passwordless_dev_idp:
            raise HTTPException(403, "development IdP is disabled; use the real OIDC flow")
        try:
            return jarvis.auth.complete_dev_login(
                email=payload.email, display_name=payload.display_name, provider=payload.provider,
                device_id=payload.device_id or settings.device_id, device_name=payload.device_name, platform=payload.platform,
            )
        except Exception as exc:
            raise HTTPException(400, f"sign-in failed: {exc}") from exc

    @app.get("/api/auth/callback")
    def oidc_callback(code: str = Query(...), state: str = Query(...), request: Request = None) -> Response:
        device_id = request.headers.get("x-jarvis-device", settings.device_id) if request else settings.device_id
        try:
            result = jarvis.auth.complete_oidc_login(state, code, device_id=device_id, device_name=None, platform="web")
        except Exception as exc:  # AuthError (bad/expired state) and anything else both mean "no login"
            raise HTTPException(400, f"login could not be completed: {exc}") from exc
        # no inline CSS/JS here: the API's CSP is 'self'-only, so this page links the
        # app stylesheet and hands the token to the SPA, which keeps it in storage.
        payload = json.dumps(result).replace("<", "\\u003c")
        return HTMLResponse(
            "<!doctype html><html><head><meta charset=utf-8><title>Signed in to Jarvis</title>"
            "<link rel=stylesheet href='/web/styles.css'></head><body class=callback>"
            "<main class=dev-card><h2>Signed in</h2><p>Handing the session back to Jarvis…</p>"
            "<p class=muted>If nothing happens, <a href='/web/index.html'>open the app</a> and paste the token below.</p>"
            f"<pre id=token>{payload}</pre></main>"
            "<script src='/web/auth-return.js'></script></body></html>"
        )

    @app.post("/api/auth/logout")
    def logout(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        jarvis.auth.logout(session["session_id"])
        return {"ok": True, "revoked": session["session_id"]}

    @app.post("/api/auth/logout-all")
    def logout_all(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        return {"ok": True, "revoked_sessions": jarvis.auth.logout_everywhere(uid(session))}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        session = session_or_401(request)
        s = jarvis.services_for(session)
        directive, profile = s["adaptive"].style_directive()
        return {
            "session": {k: v for k, v in session.items() if k != "token"},
            "user": s["user"],
            "devices": jarvis.accounts.list_devices(uid(session)),
            "identities": jarvis.accounts.identities_for(uid(session)),
            "style_directive": directive,
            "profile": profile,
            "pending_ops": int(jarvis.db.scalar("SELECT COUNT(*) FROM outbox WHERE device_id=?", (session.get("device_id") or settings.device_id,), 0) or 0),
        }

    @app.post("/api/me/name")
    def set_name(payload: NameRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        jarvis.accounts.set_profile_field(uid(session), "display_name", payload.display_name)
        return jarvis.accounts.get_user(uid(session)) or {}

    # ------------------------------------------------------------- assistant
    @app.post("/api/assistant")
    def assistant_turn(payload: AssistantRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        llm = s["llm"] if s["llm"] and jarvis.connectivity.state(settings).capabilities.get("llm") else None
        reply = s["assistant"].respond(payload.text, channel=payload.channel, engagement_seconds=payload.engagement_seconds, llm_client=llm)
        return reply.as_dict()

    @app.post("/api/assistant/feedback")
    def assistant_feedback(payload: FeedbackRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return s["adaptive"].record_feedback(payload.valence, interaction_id=payload.interaction_id, note=payload.note or "")

    # ------------------------------------------------------- adaptive prefs
    @app.get("/api/preferences")
    def preferences(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        _, profile = s["adaptive"].style_directive()
        return {
            "traits": [
                {
                    **profile["traits"][key],
                    "key": key,
                    "low": TRAIT_LABELS[key][0],
                    "high": TRAIT_LABELS[key][1],
                    "aspect": TRAIT_LABELS[key][2],
                }
                for key in TRAIT_LABELS
            ],
            "interests": profile["interests"],
            "observations": profile["observations"],
            "feedback_count": profile["feedback_count"],
            "directives": profile["directives"],
        }

    @app.post("/api/preferences/trait")
    def set_trait(payload: TraitRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        if payload.key not in TRAIT_LABELS:
            raise HTTPException(400, f"unknown trait '{payload.key}'")
        s = jarvis.services_for(session)
        state = s["adaptive"].set_trait(payload.key, payload.value)
        directive, _ = s["adaptive"].style_directive()
        return {"trait": state.as_dict(), "style_directive": directive}

    @app.post("/api/preferences/reset")
    def reset_prefs(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        for key in TRAIT_LABELS:
            s["adaptive"].set_trait(key, 0.0)
        return {"ok": True, "cleared": list(TRAIT_LABELS)}

    @app.get("/api/recommendations")
    def recommendations(items: str = Query("", max_length=4000), limit: int = Query(5, ge=1, le=50), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        """Rank candidate topics against the learned profile.

        GET with a comma-separated ``items`` list rather than a JSON body: a body on GET
        is legal-ish in HTTP but is not something a browser can send, and this endpoint
        is exactly the kind of thing a widget would prefetch.
        """
        s = jarvis.services_for(session)
        candidates = [i.strip() for i in items.split(",") if i.strip()][:200]
        return {"ranked": s["adaptive"].recommend(candidates, limit=limit)}

    # ---------------------------------------------------------------- voice
    @app.get("/api/voice/status")
    def voice_status(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        user = jarvis.accounts.get_user(uid(session)) or {}
        model = jarvis.accounts.get_voiceprint(uid(session))
        return {
            "enrolled": model is not None,
            "samples": model.sample_count if model else 0,
            "threshold": round(model.threshold, 4) if model else None,
            "calibrated": bool(model.calibrated) if model else False,
            "provider": model.provider if model else None,
            "fingerprint": model.fingerprint if model else None,
            "needs_more_samples": bool(model.needs_more_samples) if model else True,
            "intra_sims": [round(v, 4) for v in (model.intra_sims if model else [])],
            "quality": model.quality if model else [],
            "other_speakers_enrolled": sum(1 for u in jarvis.accounts.list_users() if u["id"] != user.get("id") and jarvis.accounts.get_voiceprint(u["id"])),
        }

    @app.post("/api/voice/enroll")
    def voice_enroll(payload: AudioRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        try:
            pcm = base64.b64decode(payload.audio_b64, validate=True)
            embedding, quality = voice_mod.embedding_from(voice_mod.decode_audio(pcm, fallback_rate=payload.sample_rate))
        except voice_mod.VoiceQualityError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(400, f"could not read the audio: {exc}") from exc
        uid_ = uid(session)
        existing = jarvis.accounts.get_voiceprint(uid_)
        samples = [list(e) for e in (existing.embeddings if existing else [])]
        samples.append(embedding)
        model = voice_mod.enroll(samples, provider=voice_mod.default_provider().name, quality=[*(existing.quality if existing else []), quality])
        jarvis.accounts.put_voiceprint(uid_, model)
        return {
            "enrolled_samples": model.sample_count,
            "threshold": round(model.threshold, 4),
            "quality": quality,
            "intra_sims": [round(v, 4) for v in model.intra_sims],
            "needs_more_samples": model.needs_more_samples,
            "calibrated": model.calibrated,
            "message": "Voiceprint sample added." + (" Add at least 3 samples for a usable threshold." if model.needs_more_samples else ""),
        }

    def step_up_budget(request: Request, session: dict[str, Any]) -> None:
        """A voiceprint is the gate on privileged actions, so verification attempts get their own
        budget instead of inheriting the auth limiter's. Cost is deliberately coarse: a human
        retries two or three times, a search over candidate utterances does not stop."""
        if not jarvis.auth.rate.allow(f"voice:{session['session_id']}", cost=4.0):
            jarvis.control.policy.audit(
                uid(session), action="session.step-up", target=session["session_id"], args={},
                decision="denied", reason="too many voice verification attempts", risk="medium",
            )
            raise HTTPException(
                status_code=429,
                detail={"error": "too-many-attempts", "message": "too many voice verifications — wait a moment and try again"},
                headers={"Retry-After": "10"},
            )

    @app.post("/api/voice/verify")
    def voice_verify(payload: AudioRequest, request: Request, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        step_up_budget(request, session)
        try:
            pcm = base64.b64decode(payload.audio_b64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "audio_b64 is not valid base64") from exc
        result = jarvis.auth.verify_voice_step_up(uid(session), session["session_id"], pcm)
        if result.get("accepted"):
            jarvis.control.policy.audit(uid(session), action="session.step-up", target=session["session_id"], args={}, decision="executed", reason="voiceprint matched", risk="medium")
        return result

    @app.post("/api/voice/verify-simulated")
    def voice_verify_sim(request: Request, payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        """Demo helper: verify against synthesised speech so the flow is testable with no mic.
        Same rate limit as the real endpoint — it grants the same privilege."""
        step_up_budget(request, session)
        pcm = voice_mod.synthesize_probe_pcm(str(payload.get("phrase", "hello jarvis")), 2.6, float(payload.get("f0", 118.0)))
        result = jarvis.auth.verify_voice_step_up(uid(session), session["session_id"], pcm)
        result["simulated"] = True
        return result

    @app.post("/api/voice/synth")
    def voice_synth(payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        """Returns base64 WAV of synthesised speech, so the mic-dependent endpoints can be
        exercised through exactly the same code path without a microphone."""
        if not settings.allow_passwordless_dev_idp:
            raise HTTPException(403, "synthesised audio is disabled outside development")
        text = str(payload.get("text", "hello jarvis"))[:200]
        f0 = float(payload.get("f0", 118.0))
        seconds = float(payload.get("seconds", 2.6))
        pcm = voice_mod.synthesize_probe_pcm(text, max(1.4, min(6.0, seconds)), max(70.0, min(320.0, f0)))
        return {"audio_b64": base64.b64encode(voice_mod.pcm16_to_wav_bytes(pcm)).decode(), "seconds": seconds, "f0": f0}

    @app.post("/api/voice/reset")
    def voice_reset(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        jarvis.accounts.drop_voiceprint(uid(session))
        return {"ok": True, "message": "Voiceprint deleted from this account and all synced devices."}

    @app.post("/api/voice/calibrate")
    def voice_calibrate(payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        """Feed labelled genuine/impostor similarity scores to set a defensible threshold."""
        model = jarvis.accounts.get_voiceprint(uid(session))
        if model is None:
            raise HTTPException(400, "enrol a voiceprint first")
        report = voice_mod.CalibrationReport.from_trials(
            [float(v) for v in payload.get("genuine", [])], [float(v) for v in payload.get("impostor", [])],
            target_far=float(payload.get("target_far", 0.01)),
        )
        model.threshold = report.threshold
        model.calibrated = report.usable_for_privileged
        jarvis.accounts.put_voiceprint(uid(session), model)
        return {"report": report.as_dict(), "threshold": round(model.threshold, 5), "usable_for_privileged": model.calibrated}

    # ------------------------------------------------------- commands (KWS)
    @app.get("/api/commands")
    def commands_list(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        engine = jarvis.spotter_engine()
        return {"templates": [t.phrase for t in engine.templates], "threshold": engine.distance_threshold,
                "takes": engine.takes(), "calibrated": engine.calibrated,
                "calibrated_threshold_suggestion": engine.suggest_threshold()}

    @app.post("/api/commands/enroll")
    def command_enroll(payload: CommandEnrollRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        try:
            pcm = base64.b64decode(payload.audio_b64, validate=True)
            report = jarvis.spotter_engine().enroll(payload.phrase, pcm, replace=payload.replace)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        jarvis.save_commands()
        return report

    @app.post("/api/commands/recognize")
    def command_recognize(payload: AudioRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        try:
            pcm = base64.b64decode(payload.audio_b64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "audio_b64 is not valid base64") from exc
        try:
            result = jarvis.spotter_engine().recognize(pcm)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        if result.get("matched"):
            result["executed"] = _run_offline_command(jarvis, uid(session), result["matched"])
        return result

    @app.delete("/api/commands/{phrase}")
    def command_forget(phrase: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        if not jarvis.spotter_engine().remove(phrase):
            raise HTTPException(404, f"no enrolled command called {phrase!r}")
        jarvis.save_commands()
        engine = jarvis.spotter_engine()
        return {"removed": phrase.lower().strip(), "remaining": engine.phrases(),
                "threshold": engine.distance_threshold}

    @app.post("/api/commands/auto-calibrate")
    def command_calibrate(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        engine = jarvis.spotter_engine()
        value = engine.apply_calibrated_threshold()
        jarvis.save_commands()
        return {"threshold": value, "note": "derived from your own enrolment takes" if value else "record a second take of any phrase to calibrate"}

    @app.post("/api/asr/transcribe")
    def transcribe(payload: AudioRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        from jarvis.asr import Transcriber

        try:
            pcm = base64.b64decode(payload.audio_b64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "audio_b64 is not valid base64") from exc
        return Transcriber(settings).transcribe(pcm).as_dict()

    # -------------------------------------------------------------- memory
    @app.get("/api/memory")
    def memory_list(limit: int = Query(100, ge=1, le=1000), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return {"items": s["memory"].all(limit=limit)}

    @app.post("/api/memory")
    def memory_add(payload: RememberRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        mid = s["memory"].remember(payload.body, tags=payload.tags)
        return {"id": mid, "search_preview": s["memory"].search(payload.body, limit=1)}

    @app.delete("/api/memory/{memory_id}")
    def memory_delete(memory_id: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return {"deleted": s["memory"].forget(memory_id)}

    @app.get("/api/memory/search")
    def memory_search(q: str = Query(..., min_length=1, max_length=500), limit: int = Query(6, ge=1, le=25), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return {"query": q, "hits": s["memory"].search(q, limit=limit)}

    # --------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    def tasks(status: str = Query("open", pattern="^(open|done)$"), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return {"status_filter": status, "items": s["assistant"].tasks(status=status)}

    @app.post("/api/tasks")
    def task_add(payload: TaskRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return s["assistant"].add_task(payload.description, due=payload.due)

    @app.post("/api/tasks/{task_id}/complete")
    def task_complete(task_id: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        s = jarvis.services_for(session)
        return {"completed": s["assistant"].complete_task(task_id)}

    # ------------------------------------------------------------- devices
    @app.get("/api/devices")
    def devices(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        rows = jarvis.accounts.list_devices(uid(session))
        for row in rows:
            row["sessions"] = len(jarvis.db.query("SELECT id FROM sessions WHERE device_id=? AND revoked_at IS NULL", (row["id"],)))
        return {"devices": rows, "current": session.get("device_id"), "sync_note": "sign in on any device with the same verified account and your profile, memory and settings replicate on first sync"}

    @app.post("/api/devices/pair")
    def pair(payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        """Gmail-linked device pairing: the new device gets a code, an existing trusted device approves it."""
        device_id = str(payload.get("device_id") or "")
        if not device_id:
            raise HTTPException(400, "device_id required")
        jarvis.accounts.register_device(uid(session), device_id, name=str(payload.get("device_name") or device_id), platform=str(payload.get("platform") or "unknown"))
        return jarvis.auth.request_pairing(uid(session), device_id, name=payload.get("device_name"), platform=payload.get("platform", "unknown"))

    @app.post("/api/devices/{device_id}/trust")
    def trust(device_id: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        privileged_or_403(session)
        result = jarvis.auth.trust_device(uid(session), device_id, by=session.get("device_id"))
        if result is None:
            raise HTTPException(404, "unknown device")
        jarvis.control.policy.audit(uid(session), action="device.trust", target=device_id, args={}, decision="executed", reason="trusted", risk="high")
        return {"device": result}

    @app.post("/api/devices/{device_id}/revoke")
    def revoke(device_id: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        result = jarvis.auth.untrust_device(uid(session), device_id)
        if result is None:
            raise HTTPException(404, "unknown device")
        return {"device": result, "note": "sessions on that device were revoked too"}

    # ------------------------------------------------------------ control
    @app.get("/api/control/catalog")
    def control_catalog(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        return {
            "capabilities": jarvis.control.catalog(uid(session)),
            "risk_order": RISK_ORDER,
            "rules": jarvis.control.policy.rules(uid(session)),
            "targets": jarvis.control.policy.list(uid(session)),
            "paused": jarvis.control.policy.kill_switch_state(uid(session)),
            "policy": "risky actions need an explicit rule + a privileged session + a trusted device; irreversible ones also need a one-time confirmation token",
        }

    @app.post("/api/control/targets")
    def control_target(payload: RegisterDeviceRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        privileged_or_403(session)
        return guarded(
            jarvis.control.policy.register,
            uid(session),
            name=payload.name,
            kind=payload.kind,
            endpoint=payload.endpoint,
            capabilities=payload.capabilities or sorted(CATALOG),
            pairing_verified=payload.pairing_verified,
        )

    @app.delete("/api/control/targets/{target_id}")
    def control_target_delete(target_id: str, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        privileged_or_403(session)
        if not jarvis.control.policy.remove(uid(session), target_id):
            raise HTTPException(404, "no such device on this account")
        jarvis.control.policy.audit(uid(session), action="device.unregister", target=target_id, args={}, decision="executed", reason="device removed from the allowlist", risk="medium")
        return {"removed": target_id}

    @app.post("/api/control/rules")
    def control_rule(payload: RuleRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        privileged_or_403(session)
        # an unknown capability is the user's typo, not a server fault: 400, not 500
        if payload.allow:
            return guarded(jarvis.control.policy.allow, uid(session), payload.capability, max_risk=payload.max_risk, config=payload.config)
        return guarded(jarvis.control.policy.deny, uid(session), payload.capability)

    @app.post("/api/control/execute")
    def control_execute(payload: ControlRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        return jarvis.control.execute(
            uid(session), payload.capability, payload.args, device_id=payload.device_id,
            session=session, confirmation=payload.confirmation, dry_run=payload.dry_run,
        )

    @app.post("/api/control/pause")
    def control_pause(payload: dict[str, Any] = Body(...), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        if "paused" in payload:
            enabled = bool(payload["paused"])
        elif "enabled" in payload:  # the CLI/HTTP docs phrase this as enable/disable
            enabled = not bool(payload["enabled"])
        else:
            # no default: a client that forgets the field must not silently pause (or
            # resume) control of every device the user owns
            raise HTTPException(422, "body must carry {paused: true|false}")
        jarvis.control.policy.kill_switch(uid(session), enabled=enabled)
        return {"paused": enabled, "note": "syncs to every device; all control requests are refused while paused"}

    @app.get("/api/control/audit")
    def control_audit(limit: int = Query(50, ge=1, le=500), decision: str | None = None, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        return {"entries": jarvis.control.policy.history(uid(session), limit=limit, decision=decision)}

    # --------------------------------------------------------------- media
    @app.post("/api/media/image")
    def media_image(payload: GenerationRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        params = {k: v for k, v in {"width": payload.width, "height": payload.height, "style": payload.style}.items() if v is not None}
        return jarvis.media.generate(uid(session), "image", payload.prompt, params=params, force_offline=payload.force_offline)

    @app.post("/api/media/video")
    def media_video(payload: GenerationRequest, session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        params = {k: v for k, v in {"width": payload.width, "height": payload.height, "seconds": payload.seconds, "fps": payload.fps}.items() if v is not None}
        return jarvis.media.generate(uid(session), "video", payload.prompt, params=params, force_offline=payload.force_offline)

    @app.get("/api/artifacts")
    def artifacts(limit: int = Query(40, ge=1, le=200), session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        return {"items": jarvis.media.list(uid(session), limit=limit)}

    @app.get("/api/artifacts/{artifact_id}/file")
    def artifact_file(artifact_id: str, request: Request, session: dict[str, Any] = Depends(session_or_401)) -> Response:
        if artifact_id.find(".") >= 0 or not artifact_id.startswith("art_"):
            raise HTTPException(400, "bad artifact id")
        blob = jarvis.media.read_blob(artifact_id)
        if blob is None:
            raise HTTPException(404, "artifact bytes are not on this device (they live where they were generated; metadata has synced)")
        row = jarvis.db.one("SELECT user_id FROM artifacts WHERE id=?", (artifact_id,))
        if row is None or row["user_id"] != uid(session):
            raise HTTPException(403, "not your artifact")
        return Response(content=blob[0], media_type=blob[1], headers={"Cache-Control": "private, max-age=86400"})

    # ---------------------------------------------------------------- sync
    @app.get("/api/sync/status")
    def sync_status(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        client = jarvis.sync_client(session.get("device_id"))
        pending = client.pending_count()
        row = jarvis.db.one("SELECT * FROM sync_state WHERE peer='server'")
        return {
            "device_id": session.get("device_id") or settings.device_id,
            "pending_ops": pending,
            "cursor": client.cursor(),
            "last_pull_at": row["last_pull_at"] if row else None,
            "last_push_at": row["last_push_at"] if row else None,
            "oplog_size": int(jarvis.db.scalar("SELECT COUNT(*) FROM oplog", (), 0) or 0),
            "dead_letters": client.dead_letter_count(),
            "dead_letter_detail": client.dead_letters(5),
            "last_error": client.last_error(),
            "online": jarvis.connectivity.state(settings).online,
            "behaviour": "writes are applied locally first and journalled; push is retried and pulls are idempotent, so N devices in any arrival order end identical",
        }

    @app.post("/api/sync/run")
    def sync_run(session: dict[str, Any] = Depends(session_or_401)) -> dict[str, Any]:
        client = jarvis.sync_client(session.get("device_id"))
        return client.sync(uid(session)).as_dict()

    @app.post("/api/sync/push")
    def sync_push(payload: SyncPushBody, request: Request) -> dict[str, Any]:
        # devices push their outbox; identity comes from the bearer session so a
        # client cannot journal ops into somebody else's account
        session = session_or_401(request)
        device_id = session.get("device_id") or settings.device_id
        for op in payload.ops:
            op["user_id"] = uid(session)
            op["device_id"] = device_id
        return SyncServer(jarvis.replica_db).ingest(device_id, payload.ops, observed_lamport=payload.observed_lamport)

    @app.get("/api/sync/pull")
    def sync_pull(cursor: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000), request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
        session = session_or_401(request)  # type: ignore[arg-type]
        return SyncServer(jarvis.replica_db).changes_since(cursor, uid(session), limit)

    # --------------------------------------------------------------- demo
    @app.post("/api/demo/reset")
    def demo_reset(request: Request) -> dict[str, Any]:
        if os.environ.get("JARVIS_ENABLE_DEMO_RESET") != "1":
            raise HTTPException(404, "not found")
        jarvis.db.close()
        if jarvis.db.path.exists():
            jarvis.db.path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(jarvis.db.path) + suffix)
            if p.exists():
                p.unlink()
        return {"ok": True, "restart_required": True}

    # ----------------------------------------------------------------- web
    if WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

        @app.get("/", include_in_schema=False)
        def root() -> Response:
            return RedirectResponse("/web/index.html")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def manifest() -> Response:
            return Response(content=(WEB_DIR / "manifest.webmanifest").read_bytes(), media_type="application/manifest+json")

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> Response:
            # scoped at root, so it must be served from root with a wide scope header
            return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    return app


def _run_offline_command(jarvis: Jarvis, user_id: str, phrase: str) -> dict[str, Any]:
    """What an offline-recognised command may do.

    Restricted to actions with no external side effects. Anything that touches a
    device goes through the normal policy path with a ``basic`` session, so a
    mis-heard phrase can never perform a risky action - the policy engine refuses
    and the refusal is audited, which is the correct outcome for "I think it said
    shut everything down".
    """
    key = phrase.strip().lower()
    if key in {"start sync", "sync now", "sync"}:
        return {"action": "sync", "result": jarvis.sync_client().sync(user_id).as_dict()}
    if key in {"pause device control", "stop device control", "safety on"}:
        jarvis.control.policy.kill_switch(user_id, enabled=True)
        return {"action": "kill_switch", "result": {"paused": True}}
    if key in {"resume device control", "safety off"}:
        # resuming control is itself a risky change: it needs the step-up path, not a voice command
        return {"action": "kill_switch", "result": {"paused": True, "refused": "resume requires an authenticated UI action with a privileged session"}}
    return {"action": "none", "result": {"note": f"'{phrase}' has no offline action bound; bind it in Settings"}}


def build_default_app() -> FastAPI:
    """Entry point for ``uvicorn jarvis.api.app:build_default_app --factory``."""
    settings = get_settings()
    settings.ensure_secret_key()
    return create_app(Jarvis(settings), settings=settings)
