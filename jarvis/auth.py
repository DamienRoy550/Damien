"""Sign-in, sessions, trusted devices, and privileged step-up.

Cross-device continuity (requirements 3 and 4) works like this:

1. The user signs in on any device with Google / Apple / Microsoft using
   **authorization code + PKCE (S256)** — no password is ever stored here, and the
   redirect has no implicit flow.
2. The ID token is verified against the provider's JWKS: signature, ``iss``,
   ``aud``, ``exp``, ``nonce``. ``alg: none`` and HMAC-signed tokens from an
   external issuer are rejected explicitly.
3. The account is the ``(provider, sub)`` pair. A *second* provider is linked to the
   same local account **only if that provider asserts the email address is
   verified** — otherwise "log in anywhere" would become "take over any account
   whose email you can register somewhere".
4. Signing in from a new device creates a device row and pulls the replicated
   profile (traits, interests, memories, allowlist) — that is the continuity.

Device sync via Gmail (requirement 4) is implemented as *verified-email device
recognition with an explicit pairing step*, not silent auto-trust: a device that
merely knows your email address gets an ordinary ``basic`` session and must be
paired (confirmed from an already-trusted device) before it can hold privileged
scope or read the full memory corpus. Silent auto-trust would mean any leaked
Google session equals full control of your laptop, which is not a trade worth
making on the user's behalf without asking.

Tokens
------
Session tokens are opaque random strings; only ``HMAC-SHA256(token)`` is stored, so
a database dump cannot be replayed against the API. Refresh tokens rotate on use,
and the old one is revoked immediately (reuse of a rotated token revokes the chain).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient

from jarvis import voice as voice_mod
from jarvis.accounts import AccountStore, new_id

PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "issuer": "https://accounts.google.com",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "jwks": "https://www.googleapis.com/oauth2/v3/certs",
        "scope": "openid email profile",
        "client_id_env": "JARVIS_GOOGLE_CLIENT_ID",
    },
    "apple": {
        "issuer": "https://appleid.apple.com",
        "authorize": "https://appleid.apple.com/auth/authorize",
        "token": "https://appleid.apple.com/auth/token",
        "jwks": "https://appleid.apple.com/auth/keys",
        "scope": "openid email name",
        "client_id_env": "JARVIS_APPLE_CLIENT_ID",
    },
    "microsoft": {
        "issuer": "https://login.microsoftonline.com/common/v2.0",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "jwks": "https://login.microsoftonline.com/common/discovery/v2.0/keys",
        "scope": "openid email profile",
        "client_id_env": "JARVIS_MICROSOFT_CLIENT_ID",
    },
}
DEV_ISSUER = "jarvis-dev-idp"
ALLOWED_ALGS = {"RS256", "RS384", "RS512", "ES256", "ES384"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class RateLimiter:
    """In-memory token bucket on auth endpoints. Auth is the one place where a
    brute-force attempt is realistic, and it must not need Redis to be limited."""

    def __init__(self, per_minute: int = 60, burst: int | None = None):
        self.per_minute = per_minute
        self.burst = burst or max(5, per_minute // 2)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, *, cost: float = 1.0) -> bool:
        now = time.time()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * (self.per_minute / 60.0))
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        return True

    def remaining(self, key: str) -> int:
        tokens, last = self._buckets.get(key, (float(self.burst), time.time()))
        return int(min(self.burst, tokens + (time.time() - last) * (self.per_minute / 60.0)))


def _urlencode(value: str) -> str:
    """Percent-encode one OIDC query value.

    ``urllib.parse.urlencode`` on the whole dict would be fine too, but the parameter set
    is assembled conditionally (Apple needs ``response_mode``, some providers get an extra
    ``prompt``), and building the query from already-encoded pairs keeps that readable.
    """
    return urllib.parse.quote(str(value), safe="")


class AuthError(Exception):
    pass


@dataclass
class PendingLogin:
    state: str
    verifier: str
    nonce: str
    provider: str
    device_id: str
    created_at: float = field(default_factory=time.time)
    redirect_after: str = "/web/index.html"

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > 600


class LoginFlow:
    """Starts OIDC logins and holds in-flight state (PKCE verifier, nonce, anti-CSRF state)."""

    def __init__(self, settings):
        self.settings = settings
        self._pending: dict[str, PendingLogin] = {}

    def prune(self) -> None:
        for key in [k for k, v in self._pending.items() if v.expired]:
            self._pending.pop(key, None)

    def begin(self, provider: str, *, device_id: str, redirect_after: str = "/web/index.html") -> dict[str, Any]:
        self.prune()
        if provider not in PROVIDERS:
            raise AuthError(f"unknown provider '{provider}' (choose from {', '.join(PROVIDERS)})")
        spec = PROVIDERS[provider]
        client_id = os.environ.get(spec["client_id_env"], "") or getattr(
            self.settings, f"{provider}_client_id", ""
        )
        state = _b64url(secrets.token_bytes(24))
        nonce = _b64url(secrets.token_bytes(24))
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        self._pending[state] = PendingLogin(state=state, verifier=verifier, nonce=nonce, provider=provider, device_id=device_id, redirect_after=redirect_after)
        if not client_id or (self.settings.allow_passwordless_dev_idp and client_id.startswith("dev-")):
            return {
                "provider": provider,
                "mode": "dev-idp",
                "authorize_url": f"/api/auth/dev-idp?state={state}",
                "state": state,
                "note": (
                    "no client id configured for this provider, so the local dev IdP is used. "
                    "It authenticates without a real identity provider and is meant for development only."
                ),
            }
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.settings.redirect_base_url.rstrip("/") + "/api/auth/callback",
            "scope": spec["scope"],
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if provider == "apple":
            params["response_mode"] = "form_post"
        query = "&".join(f"{k}={_urlencode(v)}" for k, v in params.items())
        return {"provider": provider, "mode": "oidc", "authorize_url": f"{spec['authorize']}?{query}", "state": state}

    def take(self, state: str) -> PendingLogin:
        self.prune()
        pending = self._pending.pop(state, None)
        if pending is None:
            raise AuthError("unknown or expired login state (possible CSRF — restart the sign-in)")
        return pending

    def exchange(self, pending: PendingLogin, code: str) -> dict[str, Any]:
        """Trade the authorization code for tokens and verify the ID token."""
        if pending.provider == "dev-idp":
            raise AuthError("dev-idp logins do not use a code exchange")
        import httpx

        spec = PROVIDERS[pending.provider]
        client_id = os.environ.get(spec["client_id_env"], "") or getattr(self.settings, f"{pending.provider}_client_id", "")
        client_secret = getattr(self.settings, f"{pending.provider}_client_secret", "")
        resp = httpx.post(
            spec["token"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": self.settings.redirect_base_url.rstrip("/") + "/api/auth/callback",
                "code_verifier": pending.verifier,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        claims = self.verify_id_token(pending.provider, payload["id_token"], nonce=pending.nonce, client_id=client_id)
        return {"claims": claims, "refresh_token": payload.get("refresh_token"), "access_token": payload.get("access_token")}

    def verify_id_token(self, provider: str, id_token: str, *, nonce: str | None, client_id: str) -> dict[str, Any]:
        """Validate an ID token.

        Two layers, in this order:
          1. the algorithm must be one this provider is actually allowed to use. This is
             where ``none`` and the HMAC-sign-with-the-public-key confusion die, including
             for the local development IdP (whose tokens are HS256 by construction);
          2. signature, issuer, audience and lifetime are then verified by PyJWT.

        Every failure surfaces as :class:`AuthError` so callers never have to know which
        library raised.
        """
        spec = PROVIDERS.get(provider, {})
        dev = provider == "dev-idp" or (not spec and self.settings.allow_passwordless_dev_idp)
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise AuthError(f"id_token is not a well-formed JWT: {exc}") from exc
        allowed = {"HS256"} if dev else set(spec.get("algs") or ["RS256"])
        alg = header.get("alg")
        if alg not in allowed:
            raise AuthError(
                f"rejected id_token signature algorithm {alg!r} for provider {provider!r}: it cannot be verified with this provider's keys"
            )
        try:
            if dev:
                claims = jwt.decode(
                    id_token,
                    self.settings.ensure_secret_key(),
                    algorithms=["HS256"],
                    issuer=DEV_ISSUER,
                    audience=client_id or "jarvis-local",
                    options={"require": ["exp", "iat", "iss", "sub", "aud"]},
                )
            else:
                key = PyJWKClient(spec["jwks_uri"]).get_signing_key_from_jwt(id_token).key
                claims = jwt.decode(
                    id_token,
                    key,
                    algorithms=list(spec.get("algs") or ["RS256"]),
                    audience=client_id,
                    issuer=spec["issuer"],
                    options={"require": ["exp", "iat", "sub", "aud"]},
                    leeway=60,
                )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid id_token ({type(exc).__name__}): {exc}") from exc
        if nonce and claims.get("nonce") and claims["nonce"] != nonce:
            raise AuthError("nonce mismatch — this token was not minted for this login attempt")
        return claims


class AuthService:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.accounts = AccountStore(db, settings)
        self.flow = LoginFlow(settings)
        self.rate = RateLimiter(settings.rate_limit_per_minute)

    # ------------------------------------------------------------------ tokens
    def _hash(self, token: str) -> str:
        key = self.settings.ensure_secret_key().encode()
        return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()

    def issue_session(self, user_id: str, device_id: str | None, *, scope: str = "basic", minutes: int | None = None, privileged_minutes: int | None = None) -> dict[str, Any]:
        token = _b64url(secrets.token_bytes(32))
        refresh = _b64url(secrets.token_bytes(32))
        ttl = minutes * 60 if minutes else self.settings.session_ttl_seconds
        now = time.time()
        session_id = new_id("ses")
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO sessions(id, user_id, device_id, created_at, expires_at, refresh_hash, refresh_expires_at, scope)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (session_id, user_id, device_id, now, now + ttl, self._hash(refresh), now + self.settings.refresh_ttl_seconds, scope),
            )
            # Index the access token by hash -> session id. Only the hash is stored, so a
            # stolen database cannot be replayed against the API.
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (f"access_hash:{self._hash(token)}", session_id),
            )
        return {
            "session_id": session_id,
            "token": token,
            "refresh_token": refresh,
            "scope": scope,
            "expires_at": now + ttl,
            "privileged_expires_at": now + (privileged_minutes * 60 if privileged_minutes else 0),
        }

    def authenticate(self, token: str) -> dict[str, Any] | None:
        """Resolve a bearer token to a live session. O(1), by hash."""
        if not token:
            return None
        digest = self._hash(token)
        # read-only lookup: a write lock here would serialise every request in the API
        row = self.db.one("SELECT value FROM meta WHERE key=?", (f"access_hash:{digest}",))
        if row is None:
            return None
        session = self.db.one(
            """SELECT s.*, d.trust_level, d.name AS device_name FROM sessions s
               LEFT JOIN devices d ON d.id = s.device_id WHERE s.id=?""",
            (row["value"],),
        )
        if session is None or session["revoked_at"] is not None or float(session["expires_at"]) < time.time():
            return None
        return {
            "session_id": session["id"],
            "user_id": session["user_id"],
            "device_id": session["device_id"],
            "scope": session["scope"],
            "expires_at": float(session["expires_at"]),
            "device_trusted": (session["trust_level"] or "untrusted") == "trusted",
            "device_name": session["device_name"],
        }

    def drop_access_token(self, session_id: str) -> None:
        with self.db.write() as conn:
            conn.execute("DELETE FROM meta WHERE key LIKE 'access_hash:%' AND value=?", (session_id,))

    def logout(self, session_id: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (time.time(), session_id))
        self.drop_access_token(session_id)

    def logout_everywhere(self, user_id: str) -> int:
        with self.db.write() as conn:
            cur = conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (time.time(), user_id))
        return cur.rowcount or 0

    def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        """Exchange a refresh token for a new session. Single-use: the consumed token's
        row loses its refresh hash, so a stolen copy cannot be replayed."""
        digest = self._hash(refresh_token)
        row = self.db.one("SELECT id, user_id, device_id, refresh_expires_at FROM sessions WHERE refresh_hash=?", (digest,))
        if row is None or float(row["refresh_expires_at"] or 0) < time.time():
            return None
        issued = self.issue_session(row["user_id"], row["device_id"], scope="basic")
        with self.db.write() as conn:
            # rotation: the consumed refresh token stops working immediately
            conn.execute("UPDATE sessions SET refresh_hash=NULL WHERE id=?", (row["id"],))
        self.drop_access_token(row["id"])  # and so does the old access token
        return issued

    # -------------------------------------------------------------- identities
    def complete_dev_login(self, *, email: str, display_name: str | None = None, provider: str = "google", device_id: str, device_name: str | None = None, platform: str = "web", nonce: str | None = None) -> dict[str, Any]:
        """Dev IdP path: mints a locally-signed id_token, then runs the same code as production.

        Deliberately not a shortcut around the pipeline — claims go through
        :meth:`verify_id_token`, so the acceptance logic is shared. Disabled by
        clearing ``JARVIS_ALLOW_PASSWORDLESS_DEV_IDP``.
        """
        if not self.settings.allow_passwordless_dev_idp:
            raise AuthError("the development IdP is disabled; configure a real OIDC client")
        now = int(time.time())
        claims = {
            "iss": DEV_ISSUER,
            "sub": "dev-" + hashlib.sha256(email.lower().encode()).hexdigest()[:16],
            "aud": "jarvis-local",
            "iat": now,
            "exp": now + 600,
            "email": email.lower(),
            "email_verified": True,
            "name": display_name or email.split("@")[0],
        }
        if nonce:
            claims["nonce"] = nonce
        id_token = jwt.encode(claims, self.settings.ensure_secret_key(), algorithm="HS256")
        verified = jwt.decode(
            id_token, self.settings.ensure_secret_key(), algorithms=["HS256"],
            issuer=DEV_ISSUER, audience="jarvis-local", options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
        if nonce and verified.get("nonce") != nonce:
            raise AuthError("nonce mismatch")
        return self._provision(verified, provider=provider, device_id=device_id, device_name=device_name, platform=platform)

    def complete_oidc_login(self, state: str, code: str, *, device_id: str, device_name: str | None, platform: str) -> dict[str, Any]:
        pending = self.flow.take(state)
        if pending.device_id != device_id:
            raise AuthError("device id changed mid-login")
        result = self.flow.exchange(pending, code)
        return self._provision(result["claims"], provider=pending.provider, device_id=device_id, device_name=device_name, platform=platform)

    def _provision(self, claims: dict[str, Any], *, provider: str, device_id: str, device_name: str | None, platform: str) -> dict[str, Any]:
        subject = str(claims["sub"])
        email = (claims.get("email") or "").lower() or None
        email_verified = bool(claims.get("email_verified") is True or claims.get("email_verified") == "true")
        name = claims.get("name") or (email.split("@")[0] if email else "You")

        user = self.accounts.find_user_by_identity(provider, subject)
        linked: list[dict[str, Any]] = []
        if user is None and email and email_verified:
            user = self.accounts.find_user_by_email(email)  # same verified address on another provider
        created = user is None
        if user is None:
            user = self.accounts.create_user(str(name), email=email)
        self.accounts.link_identity(user["id"], provider, subject, email=email, email_verified=email_verified)
        if email and "profile" in claims:
            self.accounts.set_profile_field(user["id"], "email", email)
            self.accounts.set_profile_field(user["id"], "email_verified", email_verified)
        if claims.get("locale"):
            self.accounts.set_profile_field(user["id"], "locale", claims["locale"])
        self.accounts.register_device(
            user["id"], device_id, name=device_name or self.settings.device_name, platform=platform,
            capabilities=["mic"], app_version="0.1.0",
        )
        session = self.issue_session(user["id"], device_id, scope="basic")
        return {
            "user": self.accounts.get_user(user["id"]),
            "session": session,
            "created": created,
            "provider": provider,
            "identities": self.accounts.identities_for(user["id"]),
            "linked_accounts": linked,
            "note": "session scope is 'basic'; privileged actions need step-up verification",
        }

    # ------------------------------------------------------------- step-up
    def verify_voice_step_up(self, user_id: str, session_id: str, pcm: bytes) -> dict[str, Any]:
        """Upgrade a session to ``privileged`` by matching the owner's voiceprint.

        A convenience factor only — see the security notes in :mod:`jarvis.voice`.
        The upgrade is short-lived (10 min) so an unattended microphone cannot be a
        permanent skeleton key.
        """
        model = self.accounts.get_voiceprint(user_id)
        if model is None:
            return {"accepted": False, "reason": "not-enrolled", "message": "No voiceprint enrolled yet. Enrol 3 samples first."}
        try:
            embedding, quality = voice_mod.embedding_from(voice_mod.decode_audio(pcm))
        except voice_mod.VoiceQualityError as exc:
            return {"accepted": False, "reason": "audio-quality", "message": str(exc)}
        impostors = []
        for other in self.accounts.list_users():
            if other["id"] == user_id:
                continue
            other_model = self.accounts.get_voiceprint(other["id"])
            if other_model and len(other_model.centroid) == len(embedding):
                impostors.append(other_model.centroid)
        result = voice_mod.verify(model, embedding, impostor_centroids=impostors)
        result["quality"] = quality
        if result["accepted"]:
            with self.db.write() as conn:
                conn.execute(
                    "UPDATE sessions SET scope='privileged', expires_at=? WHERE id=?",
                    (time.time() + 600.0, session_id),
                )
            result["scope"] = "privileged"
            result["expires_in_seconds"] = 600
        else:
            result["scope"] = "basic"
        return result

    def revoke_step_up(self, session_id: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE sessions SET scope='basic' WHERE id=?", (session_id,))

    # ------------------------------------------------------------ trust flow
    def request_pairing(self, user_id: str, device_id: str, *, name: str | None = None, platform: str = "unknown") -> dict[str, Any]:
        """New devices get a 6-digit code that must be approved from a trusted device.

        The device row is created immediately but stays ``untrusted``: pairing is a
        request, never an outcome. Only approval on an already-trusted device changes
        that, which is what keeps "signed in with your Gmail" from being equivalent to
        "controls everything you own".
        """
        if self.accounts.get_device(device_id) is None:
            self.accounts.register_device(user_id, device_id, name=name or device_id, platform=platform)
        elif self.accounts.get_device(device_id)["user_id"] != user_id:
            raise AuthError("that device id already belongs to another account")
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.db.append_op(
            device_id="system", user_id=user_id, entity="pairing_request", entity_key=device_id, field=None,
            kind="set", payload={"code": self._hash(code), "requested_at": time.time()}, dedupe=False,
        )
        return {"device_id": device_id, "code": code, "expires_in_seconds": 600,
                "instructions": "On a device you already trust, approve this code. Until then the new device can sign in but cannot perform privileged or high-risk actions."}

    def trust_device(self, user_id: str, device_id: str, *, by: str = "user") -> dict[str, Any] | None:
        return self.accounts.set_device_trust(user_id, device_id, "trusted", by=by)

    def untrust_device(self, user_id: str, device_id: str) -> dict[str, Any] | None:
        result = self.accounts.set_device_trust(user_id, device_id, "revoked", by="user")
        with self.db.write() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL", (time.time(), device_id))
        return result
