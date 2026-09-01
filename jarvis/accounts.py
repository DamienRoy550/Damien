"""Accounts, profiles, and the device registry — the identity substrate for
cross-device continuity.

Everything profile- or device-related is written through the op-log, which is what
lets a new device "become" the user's setup within one sync after sign-in: the
profile, learned traits, interests, memories and device-control allowlist all
arrive as replicated registers.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from jarvis import voice as voice_mod


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AccountStore:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------ users
    def create_user(self, display_name: str, *, user_id: str | None = None, email: str | None = None, locale: str = "en", timezone: str = "UTC") -> dict[str, Any]:
        uid = user_id or new_id("usr")
        now = time.time()
        profile = {"email": email} if email else {}
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO users(id, display_name, created_at, locale, timezone, profile_json) VALUES(?,?,?,?,?,?)",
                (uid, display_name, now, locale, timezone, json.dumps(profile)),
            )
        return self.get_user(uid) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if row is None:
            return None
        return self._row_to_user(row)

    def find_user_by_identity(self, provider: str, subject: str) -> dict[str, Any] | None:
        row = self.db.one(
            """SELECT u.* FROM users u JOIN identities i ON i.user_id=u.id
               WHERE i.provider=? AND i.subject=?""",
            (provider, subject),
        )
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        return [self._row_to_user(r) for r in self.db.query("SELECT * FROM users ORDER BY created_at")]

    def _row_to_user(self, row) -> dict[str, Any]:
        profile = self.db.jloads(row["profile_json"], {})
        model = voice_mod.VoiceprintModel.from_json(row["voiceprint_model"])
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
            "locale": row["locale"],
            "timezone": row["timezone"],
            "profile": profile,
            "voiceprint": {
                "enrolled": model is not None,
                "samples": model.sample_count if model else 0,
                "threshold": round(model.threshold, 4) if model else None,
                "calibrated": bool(model.calibrated) if model else False,
                "provider": model.provider if model else None,
                "fingerprint": model.fingerprint if model else None,
                "needs_more_samples": bool(model.needs_more_samples) if model else True,
                "enrolled_at": model.created_at if model else None,
            }
            if model
            else {"enrolled": False},
        }

    # -------------------------------------------------------------- identities
    def link_identity(self, user_id: str, provider: str, subject: str, *, email: str | None, email_verified: bool) -> None:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO identities(provider, subject, user_id, email, email_verified, linked_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(provider, subject) DO UPDATE SET
                     user_id=excluded.user_id, email=excluded.email,
                     email_verified=excluded.email_verified, linked_at=excluded.linked_at""",
                (provider, subject, user_id, email, 1 if email_verified else 0, time.time()),
            )

    def identities_for(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT provider, subject, email, email_verified, linked_at FROM identities WHERE user_id=?", (user_id,))
        return [
            {"provider": r["provider"], "subject": r["subject"], "email": r["email"], "email_verified": bool(r["email_verified"]), "linked_at": r["linked_at"]}
            for r in rows
        ]

    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Cross-provider account continuity.

        Two logins ("same" Gmail and Microsoft accounts) are only merged when the
        *provider itself asserted the address is verified* — otherwise this would be
        an account-takeover primitive (register an address you don't own elsewhere).
        """
        row = self.db.one(
            """SELECT user_id FROM identities WHERE lower(email)=lower(?) AND email_verified=1
               GROUP BY user_id HAVING COUNT(*)>=1 ORDER BY MAX(linked_at) DESC LIMIT 1""",
            (email,),
        )
        return self.get_user(row["user_id"]) if row else None

    # ------------------------------------------------------------ profile ops
    def set_profile_field(self, user_id: str, field: str, value: Any) -> None:
        """Field-level register: (user_id, field) so concurrent edits on two devices
        resolve per field rather than clobbering the whole profile."""
        with self.db.write() as conn:
            row = conn.execute("SELECT profile_json FROM users WHERE id=?", (user_id,)).fetchone()
            profile = json.loads(row["profile_json"]) if row and row["profile_json"] else {}
            profile[field] = value
            conn.execute("UPDATE users SET profile_json=? WHERE id=?", (json.dumps(profile, sort_keys=True), user_id))
            if field == "display_name" and value:
                conn.execute("UPDATE users SET display_name=? WHERE id=?", (str(value), user_id))
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=user_id,
            entity="user_profile",
            entity_key=f"{user_id}/{field}",
            field=field,
            kind="delete" if value is None else "set",
            payload={"value": value},
        )

    # ---------------------------------------------------------------- devices
    def register_device(self, user_id: str, device_id: str, *, name: str, platform: str = "unknown", capabilities: list[str] | None = None, app_version: str | None = None) -> dict[str, Any]:
        now = time.time()
        with self.db.write() as conn:
            existing = conn.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE devices SET user_id=?, name=?, platform=?, last_seen=?, capabilities=?, app_version=?
                       WHERE id=?""",
                    (user_id, name, platform, now, json.dumps(sorted(capabilities or [])), app_version, device_id),
                )
            else:
                conn.execute(
                    """INSERT INTO devices(id, user_id, name, platform, last_seen, trust_level, capabilities, app_version)
                       VALUES(?,?,?,?,?, 'untrusted', ?, ?)""",
                    (device_id, user_id, name, platform, now, json.dumps(sorted(capabilities or [])), app_version),
                )
        return self.get_device(device_id) or {}

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if row is None:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "platform": row["platform"],
            "last_seen": row["last_seen"],
            "trust_level": row["trust_level"],
            "trusted_by": row["trusted_by"],
            "app_version": row["app_version"],
            "capabilities": list(self.db.jloads(row["capabilities"], [])),
        }

    def list_devices(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM devices WHERE user_id=? ORDER BY last_seen DESC", (user_id,))
        out = [self.get_device(r["id"]) for r in rows]
        return [d for d in out if d]

    def set_device_trust(self, user_id: str, device_id: str, trust_level: str, *, by: str = "user") -> dict[str, Any] | None:
        if trust_level not in {"untrusted", "trusted", "revoked"}:
            raise ValueError(f"unknown trust level: {trust_level}")
        device = self.get_device(device_id)
        if device is None or device["user_id"] != user_id:
            return None
        with self.db.write() as conn:
            conn.execute(
                "UPDATE devices SET trust_level=?, trusted_by=?, last_seen=? WHERE id=?",
                (trust_level, by, time.time(), device_id),
            )
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=user_id,
            entity="device_trust",
            entity_key=device_id,
            field=None,
            kind="set" if trust_level != "revoked" else "delete",
            payload={"trust_level": trust_level, "trusted_by": by},
        )
        return self.get_device(device_id)

    # --------------------------------------------------- voiceprint (cached)
    def put_voiceprint(self, user_id: str, model: voice_mod.VoiceprintModel) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE users SET voiceprint_model=?, voiceprint_enrolled_at=? WHERE id=?",
                (model.to_json(), model.created_at, user_id),
            )
        # The embedding centroid syncs deliberately: it is what lets a freshly
        # installed device verify the owner offline. It is not invertible to audio.
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=user_id,
            entity="voice_enrollment",
            entity_key=user_id,
            field=None,
            kind="set",
            payload={"model": json.loads(model.to_json())},
        )

    def get_voiceprint(self, user_id: str) -> voice_mod.VoiceprintModel | None:
        user = self.get_user(user_id)
        if not user:
            return None
        raw = self.db.scalar("SELECT voiceprint_model FROM users WHERE id=?", (user_id,))
        return voice_mod.VoiceprintModel.from_json(raw)

    def drop_voiceprint(self, user_id: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE users SET voiceprint_model=NULL, voiceprint_enrolled_at=NULL WHERE id=?", (user_id,))
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=user_id,
            entity="voice_enrollment",
            entity_key=user_id,
            field=None,
            kind="delete",
            payload={},
        )
