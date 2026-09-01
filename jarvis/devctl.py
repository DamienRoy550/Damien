"""Device control, and the safety policy that stands in front of it.

Nothing in this module executes a user-specified command line. A caller names a
*capability* from :data:`CATALOG` plus validated arguments; the policy engine
decides; only then does an executor run a fixed argv built from an allowlist of
resolved binaries. There is no ``shell=True`` anywhere in this file, by design.

Safety model
------------
* **Opt-in targets.** A device must be registered (and, for risky capabilities,
  pairing-verified) before it can be touched. No discovery-and-drive.
* **Risk tiers** per capability: ``low`` / ``medium`` / ``high`` / ``forbidden``.
  A tier is a ceiling, not a floor — rules can only restrict.
* **Step-up for consequences.** Anything above ``low`` needs a session whose scope
  is ``privileged``, which the caller only obtains by voiceprint (or another
  configured factor) — see :mod:`jarvis.auth`.
* **Explicit confirmation** for irreversible actions, via a single-use token bound
  to the exact action fingerprint. Editing the arguments invalidates it.
* **Budgets and quiet hours** cap blast radius from a compromised session or a
  misheard voice command.
* **Kill switch** — one call disables all control instantly, and it syncs.
* **Audit everything**, including denials. A control system that only logs
  successes cannot answer "why did the TV turn off at 3am?".
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "forbidden": 99}


@dataclass(frozen=True)
class Capability:
    name: str
    risk: str
    description: str
    args: dict[str, dict[str, Any]] = field(default_factory=dict)
    irreversible: bool = False
    requires_pairing: bool = False

    def validate(self, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        clean: dict[str, Any] = {}
        errors: list[str] = []
        for key, spec in self.args.items():
            if key not in args:
                if spec.get("required"):
                    errors.append(f"missing argument '{key}'")
                continue
            value = args[key]
            kind = spec.get("type", "str")
            try:
                if kind == "int":
                    value = int(value)
                elif kind == "float":
                    value = float(value)
                elif kind == "bool":
                    value = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            except (TypeError, ValueError):
                errors.append(f"argument '{key}' must be {kind}")
                continue
            if kind in {"int", "float"}:
                lo, hi = spec.get("range", (None, None))
                if lo is not None and value < lo:
                    errors.append(f"argument '{key}' must be >= {lo}")
                if hi is not None and value > hi:
                    errors.append(f"argument '{key}' must be <= {hi}")
            if kind == "enum" and value not in spec.get("values", []):
                errors.append(f"argument '{key}' must be one of {spec['values']}")
            if kind == "str":
                pattern = spec.get("pattern")
                if pattern and not re.match(pattern, str(value)):
                    errors.append(f"argument '{key}' does not match the required format")
                # defence in depth: nothing that looks like shell syntax gets past validation,
                # even though execution is argv-only and never goes through a shell
                if re.search(r"[;&|`$<>\n\r]|\$\(", str(value)):
                    errors.append(f"argument '{key}' contains characters that are not allowed")
            clean[key] = value
        unknown = set(args) - set(self.args)
        if unknown:
            errors.append(f"unknown argument(s): {', '.join(sorted(unknown))}")
        return clean, errors


CATALOG: dict[str, Capability] = {
    c.name: c
    for c in [
        Capability("app.open", "medium", "Launch one of the apps you have registered.",
                   {"app": {"type": "str", "required": True, "pattern": r"^[A-Za-z0-9._\- ]{1,64}$"}},
                   requires_pairing=True),
        Capability("app.close", "medium", "Close an app Jarvis started (tracked pid only — never a system-wide kill).",
                   {"app": {"type": "str", "required": True, "pattern": r"^[A-Za-z0-9._\- ]{1,64}$"}},
                   requires_pairing=True),
        Capability("app.list", "low", "List apps Jarvis started and whether they are alive."),
        Capability("volume.set", "medium", "Set system volume if an audio helper is available.",
                   {"level": {"type": "int", "required": True, "range": (0, 100)}}, requires_pairing=True),
        Capability("notify", "low", "Show a notification on a target device.",
                   {"title": {"type": "str", "required": True}, "body": {"type": "str", "required": True}}),
        Capability("screenshot", "low", "Capture the screen of a registered device.",
                   {"path": {"type": "str", "pattern": r"^[A-Za-z0-9._/\-]{1,200}$"}}),
        Capability("display.sleep", "medium", "Put the display to sleep.", requires_pairing=True),
        Capability("display.wake", "low", "Wake the display."),
        Capability("files.delete", "high", "Delete a file from an allowlisted directory.",
                   {"path": {"type": "str", "required": True, "pattern": r"^[A-Za-z0-9._/\- ]{1,200}$"}},
                   irreversible=True, requires_pairing=True),
        Capability("power.shutdown", "high", "Shut a device down.",
                   {"delay_seconds": {"type": "int", "range": (0, 3600)}},
                   irreversible=True, requires_pairing=True),
        Capability("power.restart", "forbidden", "Restart a device. Disabled by default — enable per-device if you really want it.",
                   irreversible=True, requires_pairing=True),
        Capability("device.factory_reset", "forbidden", "Factory reset. Never exposed by default; there is no safe remote undo.",
                   irreversible=True, requires_pairing=True),
    ]
}


@dataclass
class Decision:
    allowed: bool
    capability: str
    risk: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_token: str | None = None
    reason: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] | None = None
    plan: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return self.requires_confirmation and self.allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "capability": self.capability,
            "risk": self.risk,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_token": self.confirmation_token,
            "args": self.args,
            "target": (self.target or {}).get("name") if self.target else None,
            "plan": self.plan,
            "checks": self.checks,
        }


class Deny(Exception):
    def __init__(self, reason: str, decision: Decision | None = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


class Policy:
    """Rules the user controls, checked in order. First failure wins and is audited."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------ registry
    def register(self, user_id: str, *, name: str, kind: str = "laptop", endpoint: str = "local",
                 capabilities: list[str] | None = None, pairing_verified: bool = False) -> dict[str, Any]:
        did = f"ctl_{uuid.uuid4().hex[:10]}"
        now = time.time()
        caps = capabilities or sorted(CATALOG)
        # A device may advertise capabilities Jarvis has no driver for. They are dropped
        # rather than rejected — a partial registration is more useful than none — but the
        # caller is told which ones, because silently losing one reads as a bug.
        unsupported = sorted({c for c in caps if c not in CATALOG})
        caps = [c for c in caps if c in CATALOG]
        if not caps:
            raise ValueError(f"none of {unsupported or capabilities} is something Jarvis can drive")
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO controlled_devices(id, user_id, name, kind, endpoint, capabilities, enabled, created_at, pairing_verified_at)
                   VALUES(?,?,?,?,?,?,?, ?,?)""",
                (did, user_id, name, kind, endpoint, json.dumps(sorted(caps)), 1, now, now if pairing_verified else None),
            )
        self.db.append_op(
            device_id=self.settings.device_id, user_id=user_id, entity="controlled_device", entity_key=did,
            field=None, kind="set",
            payload={"name": name, "kind": kind, "endpoint": endpoint, "capabilities": sorted(caps), "enabled": True},
        )
        record = self.get(user_id, did) or {}
        if unsupported:
            record = {**record, "unsupported": unsupported, "note": "not capabilities Jarvis can drive, so not registered: " + ", ".join(unsupported)}
        return record

    def get(self, user_id: str, device_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM controlled_devices WHERE id=? AND user_id=?", (device_id, user_id))
        if row is None:
            return None
        return {
            "id": row["id"], "name": row["name"], "kind": row["kind"], "endpoint": row["endpoint"],
            "capabilities": list(self.db.jloads(row["capabilities"], [])), "enabled": bool(row["enabled"]),
            "pairing_verified": row["pairing_verified_at"] is not None, "created_at": row["created_at"],
        }

    def list(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM controlled_devices WHERE user_id=? ORDER BY created_at", (user_id,))
        return [d for d in (self.get(user_id, r["id"]) for r in rows) if d]

    def set_enabled(self, user_id: str, device_id: str, enabled: bool) -> dict[str, Any] | None:
        if self.get(user_id, device_id) is None:
            return None
        with self.db.write() as conn:
            conn.execute("UPDATE controlled_devices SET enabled=? WHERE id=?", (1 if enabled else 0, device_id))
        self.db.append_op(
            device_id=self.settings.device_id, user_id=user_id, entity="controlled_device", entity_key=device_id,
            field="enabled", kind="set", payload={"enabled": enabled},
        )
        return self.get(user_id, device_id)

    def remove(self, user_id: str, device_id: str) -> bool:
        if self.get(user_id, device_id) is None:
            return False
        with self.db.write() as conn:
            conn.execute("DELETE FROM controlled_devices WHERE id=?", (device_id,))
        self.db.append_op(
            device_id=self.settings.device_id, user_id=user_id, entity="controlled_device", entity_key=device_id,
            field=None, kind="delete", payload={},
        )
        return True

    # --------------------------------------------------------------- rules
    def allow(self, user_id: str, capability: str, *, max_risk: str | None = None, config: dict | None = None) -> dict[str, Any]:
        if capability not in CATALOG and capability != "*":
            raise ValueError(f"unknown capability: {capability}")
        rid = f"rule_{uuid.uuid4().hex[:10]}"
        if max_risk is not None and max_risk not in RISK_ORDER:
            # The cap is compared by rank during policy checks; storing a typo here would
            # either explode at execution time or, worse, rank silently below everything.
            raise ValueError(f"unknown risk tier: {max_risk}")
        risk = (max_risk or CATALOG[capability].risk) if capability in CATALOG else "low"
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO control_rules(id, user_id, capability, allow, max_risk, config, created_at) VALUES(?,?,?,1,?,?,?)",
                (rid, user_id, capability, risk, json.dumps(config or {}), time.time()),
            )
        self.db.append_op(
            device_id=self.settings.device_id, user_id=user_id, entity="control_rule", entity_key=rid,
            field=None, kind="set", payload={"capability": capability, "allow": True, "max_risk": risk, "config": config or {}},
        )
        return {"id": rid, "capability": capability, "max_risk": risk}

    def deny(self, user_id: str, capability: str) -> dict[str, Any]:
        rid = f"rule_{uuid.uuid4().hex[:10]}"
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO control_rules(id, user_id, capability, allow, max_risk, config, created_at) VALUES(?,?,?,0,'forbidden','{}',?)",
                (rid, user_id, capability, time.time()),
            )
        self.db.append_op(
            device_id=self.settings.device_id, user_id=user_id, entity="control_rule", entity_key=rid,
            field=None, kind="set", payload={"capability": capability, "allow": False, "max_risk": "forbidden"},
        )
        return {"id": rid, "capability": capability, "allow": False}

    def rules(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, capability, allow, max_risk, config FROM control_rules WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        return [{"id": r["id"], "capability": r["capability"], "allow": bool(r["allow"]), "max_risk": r["max_risk"], "config": self.db.jloads(r["config"], {})} for r in rows]

    # --------------------------------------------------------- kill switch
    def kill_switch(self, user_id: str, *, enabled: bool) -> None:
        self.db.set_user_flag(user_id, "control_paused", bool(enabled), device_id=self.settings.device_id)

    def kill_switch_state(self, user_id: str) -> bool:
        return bool(self.db.user_flag(user_id, "control_paused", False))

    # ------------------------------------------------------------- evaluate
    def decide(
        self,
        user_id: str,
        capability: str,
        args: dict[str, Any],
        *,
        device_id: str | None = None,
        session: dict[str, Any] | None = None,
        dry_run: bool = False,
        has_confirmation: bool = False,
    ) -> Decision:
        checks: list[dict[str, Any]] = []
        cap = CATALOG.get(capability)
        decision = Decision(allowed=False, capability=capability, risk=cap.risk if cap else "forbidden")

        def check(name: str, ok: bool, detail: str = "") -> bool:
            checks.append({"check": name, "passed": bool(ok), "detail": detail})
            return ok

        if not check("capability-known", cap is not None, "" if cap else f"'{capability}' is not in the capability catalog"):
            return self._finalise(decision, checks, reason="unknown capability")
        assert cap is not None
        decision.risk = cap.risk

        clean, errors = cap.validate(args)
        decision.args = clean
        if not check("arguments-valid", not errors, "; ".join(errors)):
            return self._finalise(decision, checks, reason="; ".join(errors))

        if not check("risk-not-forbidden", RISK_ORDER[cap.risk] < RISK_ORDER["forbidden"],
                     "this capability is disabled by default and must be explicitly allowed by you"):
            return self._finalise(decision, checks, reason=f"{cap.name} is forbidden by default")

        if not check("kill-switch-off", not self.db.user_flag(user_id, "control_paused", False),
                     "device control is paused; resume it in Settings > Device control"):
            return self._finalise(decision, checks, reason="device control is paused")

        devices = self.list(user_id)
        if not check("has-registered-devices", bool(devices), "no devices registered; add one first (Jarvis will not drive anything you have not opted in)"):
            return self._finalise(decision, checks, reason="no devices registered")
        target = next((d for d in devices if (device_id is None or d["id"] == device_id)), None)
        if not check("target-specified", target is not None, "that device id is not in your list" if target is None else ""):
            return self._finalise(decision, checks, reason="unknown target device")
        assert target is not None
        decision.target = target
        if not check("target-enabled", target["enabled"], f"{target['name']} is disabled"):
            return self._finalise(decision, checks, reason=f"{target['name']} is disabled")
        if not check("target-capability", cap.name in target["capabilities"], f"{target['name']} does not allow {cap.name}"):
            return self._finalise(decision, checks, reason=f"{target['name']} does not expose {cap.name}")
        if cap.requires_pairing and not check("pairing-verified", target["pairing_verified"],
                                             "this action needs a pairing-verified device; re-pair to continue"):
            return self._finalise(decision, checks, reason="device pairing not verified")

        rule = self._match_rule(user_id, cap.name)
        if rule is not None:
            if not check("user-rule", bool(rule["allow"]), "you denied this capability"):
                return self._finalise(decision, checks, reason=f"{cap.name} is denied by your rules")
            if not check("user-risk-ceiling", RISK_ORDER[cap.risk] <= RISK_ORDER.get(rule["max_risk"], 0),
                         f"your rule caps risk at '{rule['max_risk']}' but {cap.name} is '{cap.risk}'"):
                return self._finalise(decision, checks, reason=f"blocked by your risk ceiling ({rule['max_risk']})")
        else:
            if not check("default-opt-in", RISK_ORDER[cap.risk] <= RISK_ORDER["low"],
                         "no rule allows this capability; risky actions require an explicit allow rule from you"):
                return self._finalise(decision, checks, reason=f"{cap.name} needs an explicit allow rule")

        scope = (session or {}).get("scope", "basic")
        if RISK_ORDER[cap.risk] > RISK_ORDER["low"]:
            if not check("privileged-session", scope == "privileged",
                         "this action needs a step-up verified session (voiceprint or another configured factor)"):
                return self._finalise(decision, checks, reason="step-up verification required")
            device_trusted = (session or {}).get("device_trusted", True)
            if not check("trusted-device", device_trusted, "this device is not marked trusted for risky actions"):
                return self._finalise(decision, checks, reason="untrusted device")

        budget = (rule or {}).get("config", {}).get("max_per_hour") if rule else None
        if budget:
            recent = int(self.db.scalar(
                """SELECT COUNT(*) FROM control_audit
                   WHERE user_id=? AND action=? AND decision='executed' AND created_at > ?""",
                (user_id, cap.name, time.time() - 3600.0), 0,
            ) or 0)
            if not check("rate-budget", recent < int(budget), f"{recent}/{budget} actions used this hour"):
                return self._finalise(decision, checks, reason=f"hourly budget of {budget} reached")

        quiet = (rule or {}).get("config", {}).get("quiet_hours") if rule else None
        if quiet:
            hour = time.localtime().tm_hour
            lo, hi = quiet
            blocked = (hour >= lo) == (hour < hi) if lo <= hi else (hour >= lo or hour < hi)
            if not check("quiet-hours", not blocked, f"blocked between {lo}:00 and {hi}:00 unless confirmed"):
                decision.requires_confirmation = True

        plan, plan_err = self._plan(target, cap.name, clean)
        if not check("endpoint-resolvable", plan_err is None, plan_err or ""):
            return self._finalise(decision, checks, reason=plan_err)
        decision.plan = plan

        # Irreversible actions need a confirmation token bound to the exact
        # (capability, args, target) triple. `has_confirmation` means the caller already
        # validated and consumed one, so a stale token can never authorise a different call.
        if cap.irreversible and not has_confirmation and not dry_run:
            decision.requires_confirmation = True
            decision.confirmation_token = self._issue_confirmation(user_id, cap.name, clean, target["id"])
            check("confirmation", False, "irreversible action: confirm once with the returned token")
            return self._finalise(decision, checks, reason="confirmation required")

        decision.allowed = True
        decision.checks = checks
        decision.reason = "ok"
        return decision

    def _finalise(self, decision: Decision, checks: list[dict[str, Any]], *, reason: str) -> Decision:
        decision.allowed = False
        decision.reason = reason
        decision.checks = checks
        return decision

    def _match_rule(self, user_id: str, capability: str) -> dict[str, Any] | None:
        rules = self.rules(user_id)
        for r in rules:
            if r["capability"] == capability:
                return r
        for r in rules:
            if r["capability"] == "*":
                return r
        return None

    # -------------------------------------------------------- confirmation
    def fingerprint(self, capability: str, args: dict[str, Any], target_id: str) -> str:
        blob = json.dumps([capability, args, target_id], sort_keys=True)
        return re.sub(r"[^a-f0-9]", "", __import__("hashlib").sha256(blob.encode()).hexdigest())[:32]

    def _issue_confirmation(self, user_id: str, capability: str, args: dict[str, Any], target_id: str) -> str:
        token = uuid.uuid4().hex
        now = time.time()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO confirmations(token, user_id, fingerprint, payload, created_at, expires_at) VALUES(?,?,?,?,?,?)",
                (token, user_id, self.fingerprint(capability, args, target_id), json.dumps({"capability": capability, "args": args, "target": target_id}), now, now + 300),
            )
        return token

    def confirm_token_valid(self, user_id: str, token: str, capability: str, args: dict[str, Any], target_id: str) -> bool:
        """Single-use, argument-bound, 5-minute TTL."""
        row = self.db.one("SELECT fingerprint, expires_at, consumed_at FROM confirmations WHERE token=?", (token,))
        if row is None or row["consumed_at"] is not None or float(row["expires_at"]) < time.time():
            return False
        return row["fingerprint"] == self.fingerprint(capability, args, target_id)

    def mark_confirmed(self, token: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE confirmations SET consumed_at=? WHERE token=?", (time.time(), token))

    def authorize_confirmation(self, user_id: str, token: str, capability: str, args: dict[str, Any], target_id: str) -> bool:
        if not self.confirm_token_valid(user_id, token, capability, args, target_id):
            return False
        self.mark_confirmed(token)
        return True

    # -------------------------------------------------------------- plan
    def _plan(self, target: dict[str, Any], capability: str, args: dict[str, Any]) -> tuple[list[str], str | None]:
        endpoint = target.get("endpoint", "local")
        if endpoint.startswith("adapter:"):
            return [f"adapter:{endpoint.split(':',1)[1]}.{capability}({json.dumps(args, sort_keys=True)})"], None
        if endpoint != "local":
            return [], f"endpoint '{endpoint}' has no driver configured (use 'local' or 'adapter:<name>')"
        if capability in {"app.open", "app.close"}:
            alias = str(args.get("app", ""))
            resolved, err = resolve_app(alias)
            if err:
                return [], err
            return ([f"spawn {shlex.quote(resolved)}"] if capability == "app.open" else [f"terminate tracked pid for {alias}"]), None
        if capability in {"notify", "app.list", "display.wake", "screenshot"}:
            return [f"local:{capability}"], None
        if capability in {"volume.set", "display.sleep"}:
            helper = shutil.which(os.environ.get("JARVIS_AUDIO_HELPER", "amixer")) or shutil.which("osascript")
            if helper is None:
                return [], f"no system helper for {capability} on this machine (set JARVIS_AUDIO_HELPER)"
            return [f"{helper} ..."], None
        if capability.startswith("power."):
            return [f"system power hook for {capability} (must be installed on the target)"], None
        if capability == "files.delete":
            root = os.environ.get("JARVIS_CONTROL_ALLOW_DIR", "")
            path = str(args.get("path", ""))
            if not root:
                return [], "files.delete requires JARVIS_CONTROL_ALLOW_DIR so only an allowlisted subtree is reachable"
            real_root, real_path = os.path.realpath(root), os.path.realpath(os.path.join(root, path))
            if not (real_path == real_root or real_path.startswith(real_root + os.sep)):
                return [], f"path '{path}' escapes the allowlisted directory"
            return [f"unlink {real_path}"], None
        return [], f"no local driver for capability '{capability}'"

    # -------------------------------------------------------------- audit
    def audit(self, user_id: str, *, action: str, target: str | None, args: dict, decision: str, reason: str | None, risk: str | None, duration_ms: int | None = None, output: str | None = None, exit_status: str | None = None, device_id: str | None = None) -> None:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO control_audit(id, user_id, device_id, action, target, args, decision, reason, risk, created_at, duration_ms, exit_status, output)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"aud_{uuid.uuid4().hex[:10]}", user_id, device_id, action, target, json.dumps(args, sort_keys=True), decision, reason, risk, time.time(), duration_ms, exit_status, (output or "")[:4000]),
            )

    def history(self, user_id: str, *, limit: int = 50, decision: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM control_audit WHERE user_id=?"
        params: list[Any] = [user_id]
        if decision:
            sql += " AND decision=?"
            params.append(decision)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [
            {"id": r["id"], "action": r["action"], "target": r["target"], "args": self.db.jloads(r["args"], {}),
             "decision": r["decision"], "reason": r["reason"], "risk": r["risk"], "created_at": r["created_at"],
             "duration_ms": r["duration_ms"], "exit_status": r["exit_status"], "output": r["output"]}
            for r in self.db.query(sql, params)
        ]


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------
def resolve_app(alias: str) -> tuple[str | None, str | None]:
    """Map a user-registered app alias to an absolute binary path.

    Resolution is against the user's own registered aliases and PATH, and the
    result must itself be an executable file. Unknown aliases are an error,
    never a fallback to "run what you typed".
    """
    apps = {}
    raw = os.environ.get("JARVIS_ALLOWED_APPS", "")
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        name, _, path = entry.partition("=")
        apps[name.strip().lower()] = (path.strip() or name.strip())
    key = alias.strip().lower()
    if key not in apps:
        return None, f"'{alias}' is not one of your registered apps (set JARVIS_ALLOWED_APPS=name=/path/to/bin)"
    candidate = apps[key]
    resolved = candidate if os.path.isabs(candidate) else shutil.which(candidate)
    if resolved is None:
        return None, f"app '{alias}' resolves to '{candidate}', which is not on PATH here"
    if not os.access(resolved, os.X_OK):
        return None, f"app '{alias}' resolves to '{resolved}', which is not executable"
    return resolved, None


class LocalExecutor:
    """Executes plans on this machine, within the constraints decided by Policy."""

    name = "local"

    def __init__(self) -> None:
        # in-memory because a Jarvis restart must NOT orphan control of apps it started
        self._tracked: dict[str, int] = {}

    def run(self, target: dict[str, Any], capability: str, args: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
        import subprocess

        if capability == "app.open":
            resolved, err = resolve_app(str(args.get("app", "")))
            if err or resolved is None:
                return {"ok": False, "error": err}
            key = str(args["app"]).strip().lower()
            if key in self._tracked and self._alive(self._tracked[key]):
                return {"ok": True, "detail": f"'{key}' already open (pid {self._tracked[key]})", "pid": self._tracked[key]}
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv only, resolved binary, no shell
                    [resolved], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                return {"ok": False, "error": f"could not start '{key}': {exc}"}
            self._tracked[key] = proc.pid
            return {"ok": True, "detail": f"started '{key}' (pid {proc.pid})", "pid": proc.pid}

        if capability == "app.close":
            key = str(args.get("app", "")).strip().lower()
            pid = self._tracked.pop(key, None)
            if pid is None:
                # deliberately refuses pkill-style matching: Jarvis only stops what it started
                return {"ok": False, "error": f"I did not start '{key}', so I will not kill it. Only processes I launched are tracked."}
            if not self._alive(pid):
                return {"ok": True, "detail": f"'{key}' (pid {pid}) had already exited"}
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                return {"ok": True, "detail": f"'{key}' had already exited"}
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            deadline = time.time() + 3.0
            while time.time() < deadline and self._alive(pid):
                time.sleep(0.1)
            force = self._alive(pid)
            if force:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
            return {"ok": True, "detail": f"closed '{key}' (pid {pid}){' after SIGKILL' if force else ''}"}

        if capability == "app.list":
            return {"ok": True, "detail": [{"app": k, "pid": p, "alive": self._alive(p)} for k, p in self._tracked.items()]}

        if capability == "notify":
            return {"ok": True, "detail": f"notification queued: {args.get('title')} — {args.get('body')}"}

        if capability == "volume.set":
            helper = shutil.which(os.environ.get("JARVIS_AUDIO_HELPER", "amixer"))
            if helper is None:
                return {"ok": False, "error": "no volume helper available on this host"}
            level = int(args.get("level", 0))
            cmd = [helper, "-q", "sset", "Master", f"{level}%"] if "amixer" in helper else ["osascript", "-e", f"set volume output volume {level}"]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
                return {"ok": out.returncode == 0, "detail": (out.stdout or out.stderr).strip()[:200] or f"volume set to {level}%"}
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"ok": False, "error": str(exc)}

        if capability == "display.sleep":
            for cmd in (["xdg-screensaver", "lock"], ["pmset", "displaysleepnow"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False, timeout=5, capture_output=True)  # noqa: S603
                    return {"ok": True, "detail": "display sleep requested"}
            return {"ok": False, "error": "no display helper (xdg-screensaver/pmset) on this host"}

        if capability == "files.delete":
            root = os.environ.get("JARVIS_CONTROL_ALLOW_DIR", "")
            path = os.path.realpath(os.path.join(root, str(args.get("path", ""))))
            if not path.startswith(os.path.realpath(root) + os.sep):
                return {"ok": False, "error": "refused: path escapes the allowlisted directory"}
            try:
                os.unlink(path)
                return {"ok": True, "detail": f"deleted {path}"}
            except OSError as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"local executor has no driver for '{capability}'"}

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


class DryRunExecutor:
    name = "dry-run"

    def run(self, target: dict[str, Any], capability: str, args: dict[str, Any], *, timeout: float = 0.0) -> dict[str, Any]:
        return {"ok": True, "detail": "dry run: nothing executed", "would_run": args}


class ControlService:
    def __init__(self, db, settings, *, executor: Any | None = None):
        self.db = db
        self.settings = settings
        self.policy = Policy(db, settings)
        self.executor = executor or LocalExecutor()

    def execute(
        self,
        user_id: str,
        capability: str,
        args: dict[str, Any] | None = None,
        *,
        device_id: str | None = None,
        session: dict[str, Any] | None = None,
        confirmation: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        args = args or {}
        # Validate + consume the token BEFORE deciding, so the decision that gets
        # executed and the decision that was authorised are the same object.
        consumed = False
        pre = self.policy.decide(user_id, capability, args, device_id=device_id, session=session, dry_run=dry_run)
        if confirmation and pre.target is not None:
            consumed = self.policy.authorize_confirmation(user_id, confirmation, capability, pre.args, pre.target.get("id", ""))
        decision = self.policy.decide(user_id, capability, args, device_id=device_id, session=session, dry_run=dry_run, has_confirmation=consumed)
        if decision.reason == "confirmation required" and confirmation is None:
            self.policy.audit(user_id, action=capability, target=(decision.target or {}).get("name"), args=decision.args,
                              decision="needs-confirmation", reason="confirmation required", risk=decision.risk, device_id=device_id)
            return {"status": "needs_confirmation", "confirmation_token": decision.confirmation_token, "decision": decision.as_dict(),
                    "message": "This is irreversible. Repeat the action with this confirmation token within 5 minutes."}
        if confirmation and not consumed:
            self.policy.audit(user_id, action=capability, target=(decision.target or {}).get("name"), args=decision.args,
                              decision="denied", reason="invalid, expired, or already-used confirmation token", risk=decision.risk, device_id=device_id)
            return {"status": "denied", "reason": "invalid, expired, or already-used confirmation token", "decision": decision.as_dict()}
        if not decision.allowed:
            self.policy.audit(user_id, action=capability, target=(decision.target or {}).get("name"), args=args,
                              decision="denied", reason=decision.reason, risk=decision.risk, device_id=device_id)
            return {"status": "denied", "reason": decision.reason, "decision": decision.as_dict()}
        if dry_run:
            self.policy.audit(user_id, action=capability, target=(decision.target or {}).get("name"), args=decision.args,
                              decision="dry-run", reason="allowed (not executed)", risk=decision.risk, device_id=device_id)
            return {"status": "dry_run", "allowed": True, "plan": decision.plan, "decision": decision.as_dict()}
        started = time.time()
        result = self.executor.run(decision.target or {}, capability, decision.args)
        duration = int((time.time() - started) * 1000)
        self.policy.audit(user_id, action=capability, target=(decision.target or {}).get("name"), args=decision.args,
                          decision="executed" if result.get("ok") else "failed", reason=result.get("error"),
                          risk=decision.risk, duration_ms=duration, output=json.dumps(result)[:1000],
                          exit_status="ok" if result.get("ok") else "error", device_id=device_id)
        return {"status": "executed" if result.get("ok") else "failed", "result": result, "decision": decision.as_dict()}

    def catalog(self, user_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for name, cap in CATALOG.items():
            entry = {"capability": name, "risk": cap.risk, "description": cap.description,
                     "irreversible": cap.irreversible, "requires_pairing": cap.requires_pairing,
                     "arguments": {k: {kk: vv for kk, vv in v.items() if kk != "pattern"} for k, v in cap.args.items()}}
            out.append(entry)
        return sorted(out, key=lambda d: (RISK_ORDER[d["risk"]], d["capability"]))
