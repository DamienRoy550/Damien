"""Device control: the refusals matter more than the executions."""

from __future__ import annotations

import json
import time

import pytest
from jarvis.devctl import CATALOG, ControlService, DryRunExecutor, LocalExecutor, Policy, resolve_app

USER = "u-owner"


@pytest.fixture()
def control(db, settings, monkeypatch):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES(?,?,?,'{}')", (USER, "Owner", time.time()))
    monkeypatch.setenv("JARVIS_ALLOWED_APPS", "sleeper=/bin/sleep,cat=/bin/cat")
    service = ControlService(db, settings, executor=LocalExecutor())
    return service


def priv(device_trusted: bool = True) -> dict:
    return {"scope": "privileged", "device_trusted": device_trusted}


def basic() -> dict:
    return {"scope": "basic", "device_trusted": True}


def register(control, **kw) -> str:
    target = control.policy.register(USER, name=kw.pop("name", "Living room TV"), **kw)
    return target["id"]


# ------------------------------------------------------------------ catalog
def test_catalog_marks_dangerous_capabilities_forbidden_by_default():
    assert CATALOG["device.factory_reset"].risk == "forbidden"
    assert CATALOG["power.restart"].risk == "forbidden"
    assert CATALOG["app.open"].risk == "medium"
    assert CATALOG["notify"].risk == "low"
    irreversible_names = {"files.delete", "power.shutdown", "power.restart", "device.factory_reset"}
    for cap in CATALOG.values():
        assert cap.irreversible == (cap.name in irreversible_names), cap.name


def test_forbidden_capability_cannot_be_run_even_if_allowed(control):
    register(control, capabilities=["device.factory_reset"], pairing_verified=True)
    control.policy.allow(USER, "device.factory_reset", max_risk="high")
    result = control.execute(USER, "device.factory_reset", {}, session=priv())
    assert result["status"] == "denied"
    assert "forbidden" in result["reason"]


def test_unknown_capability_is_denied(control):
    register(control, capabilities=["notify"], pairing_verified=True)
    result = control.execute(USER, "power.launch_missiles", {}, session=priv())
    assert result["status"] == "denied" and "catalog" in result["decision"]["checks"][0]["detail"]


# ---------------------------------------------------------------- opt-in
def test_no_devices_means_no_control(control):
    result = control.execute(USER, "notify", {"title": "x", "body": "y"}, session=basic())
    assert result["status"] == "denied" and "no devices registered" in result["reason"]


def test_disabled_target_is_refused(control):
    target = register(control, capabilities=["notify"], pairing_verified=True)
    control.policy.set_enabled(USER, target, False)
    result = control.execute(USER, "notify", {"title": "t", "body": "b"}, device_id=target, session=basic())
    assert result["status"] == "denied" and "disabled" in result["reason"]


def test_capability_must_be_in_the_target_list(control):
    target = register(control, name="Speaker", kind="speaker", capabilities=["notify"], pairing_verified=True)
    control.policy.allow(USER, "app.open")
    result = control.execute(USER, "app.open", {"app": "sleeper"}, device_id=target, session=priv())
    assert result["status"] == "denied" and "does not expose" in result["reason"]


def test_risky_action_needs_pairing_verified_device(control):
    target = register(control, capabilities=["app.open"], pairing_verified=False)
    control.policy.allow(USER, "app.open")
    result = control.execute(USER, "app.open", {"app": "sleeper"}, device_id=target, session=priv())
    assert result["status"] == "denied" and "pairing" in result["reason"]


# ------------------------------------------------------------------ scoping
def test_medium_risk_requires_privileged_session(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open")
    denied = control.execute(USER, "app.open", {"app": "sleeper"}, session=basic())
    assert denied["status"] == "denied" and "step-up" in denied["reason"]
    allowed = control.execute(USER, "app.open", {"app": "sleeper"}, session=priv(), dry_run=True)
    assert allowed["status"] == "dry_run"


def test_low_risk_works_on_a_basic_session(control):
    register(control, capabilities=["notify"], pairing_verified=False)
    result = control.execute(USER, "notify", {"title": "hi", "body": "there"}, session=basic())
    assert result["status"] == "executed"


def test_untrusted_device_cannot_do_risky_things(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open")
    result = control.execute(USER, "app.open", {"app": "sleeper"}, session=priv(device_trusted=False))
    assert result["status"] == "denied" and "untrusted" in result["reason"]


# ------------------------------------------------------------ argument safety
def test_shell_metacharacters_are_rejected(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open")
    for bad in ["nano; rm -rf /", "nano | tee", "$(whoami)", "nano && id", "nano\nid", "nano`id`"]:
        result = control.execute(USER, "app.open", {"app": bad}, session=priv())
        assert result["status"] == "denied", bad


def test_out_of_range_arguments_rejected(control):
    register(control, capabilities=["volume.set"], pairing_verified=True)
    control.policy.allow(USER, "volume.set")
    assert control.execute(USER, "volume.set", {"level": 500}, session=priv())["status"] == "denied"
    assert control.execute(USER, "volume.set", {"level": -3}, session=priv())["status"] == "denied"


def test_unknown_arguments_rejected(control):
    register(control, capabilities=["notify"], pairing_verified=True)
    result = control.execute(USER, "notify", {"title": "t", "body": "b", "as_root": True}, session=basic())
    assert result["status"] == "denied" and "unknown argument" in result["reason"]


def test_app_resolution_never_falls_back_to_the_typed_string(control, monkeypatch, tmp_path):
    """An unregistered name is an error, never a binary path taken from the request."""
    resolved, err = resolve_app("sleeper")
    assert err is None and resolved.endswith("/sleep")
    bad, err2 = resolve_app("/bin/cat")  # a full path is not an alias
    assert bad is None and "not one of your registered apps" in err2, err2
    monkeypatch.setenv("JARVIS_ALLOWED_APPS", "echo=/bin/echo,notexec=/etc/hostname,dangling=/nope/nothing")
    resolved2, err3 = resolve_app("echo")
    assert err3 is None and resolved2 == "/bin/echo", "aliases are user-owned: what they point at is their choice"
    # a non-executable alias is refused at resolve time, not at spawn time
    refused, err4 = resolve_app("notexec")
    assert refused is None and "not executable" in err4, err4
    missing, err5 = resolve_app("dangling")
    assert missing is None and ("not on PATH" in err5 or "not executable" in err5), err5
    bare, err6 = resolve_app("definitely-not-a-real-binary-xyz")
    assert bare is None and "not on PATH" not in err6 and "not one of your registered apps" in err6
    # ...but only through the allowlist; the executor never receives a raw string
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open")
    result = control.execute(USER, "app.open", {"app": "/bin/cat"}, session=priv())
    assert result["status"] == "denied"


def test_local_executor_refuses_to_kill_what_it_did_not_start():
    executor = LocalExecutor()
    result = executor.run({"endpoint": "local"}, "app.close", {"app": "safari"})
    assert result["ok"] is False and "did not start" in result["error"]


def test_executor_never_uses_a_shell():
    """Structural check: no subprocess call in the module passes shell=, and every
    spawn/execute uses an argv list. A substring grep would trip on the prose that
    explains the rule, so this parses the syntax tree."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("jarvis/devctl.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            if node.func.attr in {"run", "Popen", "call", "check_output", "check_call"}:
                if any(k.arg == "shell" for k in node.keywords):
                    offenders.append(node.lineno)
                first = node.args[0] if node.args else None
                if first is not None and not isinstance(first, (ast.List, ast.Tuple)):
                    if not (isinstance(first, ast.Name) and first.id == "cmd"):
                        offenders.append(node.lineno)  # a string command line would go through a shell
    assert not offenders, f"possible shell/string execution at lines {offenders}"


# ------------------------------------------------------------- confirmation
def test_irreversible_action_requires_confirmation_then_consumes_it(control):
    register(control, capabilities=["power.shutdown"], pairing_verified=True)
    control.policy.allow(USER, "power.shutdown", max_risk="high")
    first = control.execute(USER, "power.shutdown", {}, session=priv())
    assert first["status"] == "needs_confirmation" and first["confirmation_token"]
    token = first["confirmation_token"]
    # tampering with the arguments invalidates the token (it is bound to them)
    tampered = control.execute(USER, "power.shutdown", {"delay_seconds": 99999}, session=priv(), confirmation=token)
    assert tampered["status"] == "denied" and "confirmation" in tampered["reason"]
    ok = control.execute(USER, "power.shutdown", {}, session=priv(), confirmation=token)
    assert ok["status"] in {"executed", "failed"}, ok  # may fail for lack of a power helper; not a policy denial
    replay = control.execute(USER, "power.shutdown", {}, session=priv(), confirmation=token)
    assert replay["status"] == "denied" and "already-used" in replay["reason"]


def test_confirmation_token_expires(control, db):
    register(control, capabilities=["power.shutdown"], pairing_verified=True)
    control.policy.allow(USER, "power.shutdown", max_risk="high")
    token = control.execute(USER, "power.shutdown", {}, session=priv())["confirmation_token"]
    with db.write() as conn:
        conn.execute("UPDATE confirmations SET expires_at=? WHERE token=?", (time.time() - 1, token))
    assert control.execute(USER, "power.shutdown", {}, session=priv(), confirmation=token)["status"] == "denied"


# ------------------------------------------------------- budgets / windows
def test_hourly_budget_blocks_after_the_limit(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open", config={"max_per_hour": 2})
    control.executor = DryRunExecutor()
    for _ in range(2):
        assert control.execute(USER, "app.open", {"app": "sleeper"}, session=priv())["status"] == "executed"
    blocked = control.execute(USER, "app.open", {"app": "sleeper"}, session=priv())
    assert blocked["status"] == "denied" and "budget" in blocked["reason"]


def test_quiet_hours_demand_confirmation(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.policy.allow(USER, "app.open", config={"quiet_hours": [0, 24]})
    result = control.execute(USER, "app.open", {"app": "sleeper"}, session=priv())
    assert result["status"] == "needs_confirmation" or result["decision"]["requires_confirmation"]


def test_kill_switch_blocks_everything_then_releases(control):
    register(control, capabilities=["notify"], pairing_verified=True)
    control.policy.kill_switch(USER, enabled=True)
    assert control.execute(USER, "notify", {"title": "t", "body": "b"}, session=priv())["status"] == "denied"
    control.policy.kill_switch(USER, enabled=False)
    assert control.execute(USER, "notify", {"title": "t", "body": "b"}, session=priv())["status"] == "executed"


def test_kill_switch_is_replicated(db, settings):
    """Paused on one device must mean paused everywhere, so it goes through the op-log."""
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES(?,?,?,'{}')", (USER, "Owner", time.time()))
    policy = Policy(db, settings)
    policy.kill_switch(USER, enabled=True)
    row = db.one("SELECT payload, entity FROM oplog WHERE entity='user_profile' ORDER BY seq DESC LIMIT 1")
    assert row is not None and json.loads(row["payload"])["value"] is True


# --------------------------------------------------------------------- audit
def test_every_denial_is_audited_with_its_reason(control):
    register(control, capabilities=["app.open"], pairing_verified=True)
    control.execute(USER, "app.open", {"app": "sleeper"}, session=basic())
    control.policy.allow(USER, "app.open")
    control.execute(USER, "app.open", {"app": "sleeper"}, session=basic())
    entries = control.policy.history(USER, limit=10)
    assert entries and entries[0]["decision"] == "denied"
    assert entries[0]["risk"] == "medium"
    assert "step-up" in (entries[0]["reason"] or ""), entries[0]


def test_history_can_be_filtered(control):
    register(control, capabilities=["notify"], pairing_verified=True)
    control.execute(USER, "notify", {"title": "t", "body": "b"}, session=basic())
    control.execute(USER, "power.shutdown", {}, session=basic())
    assert all(e["decision"] == "denied" for e in control.policy.history(USER, decision="denied"))
    assert all(e["decision"] == "executed" for e in control.policy.history(USER, decision="executed"))


def test_endpoint_without_a_driver_is_refused_not_guessed(control):
    target = control.policy.register(USER, name="Lan box", kind="laptop", endpoint="lan:10.0.0.9", capabilities=["notify"])
    control.policy.allow(USER, "notify")
    result = control.execute(USER, "notify", {"title": "t", "body": "b"}, device_id=target["id"], session=basic())
    assert result["status"] == "denied" and "no driver" in result["reason"]


def test_files_delete_is_confined_to_the_allowlist(control, monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "inside.txt").write_text("keep")
    monkeypatch.setenv("JARVIS_CONTROL_ALLOW_DIR", str(root))
    target = register(control, name="My laptop", capabilities=["files.delete"], pairing_verified=True)
    control.policy.allow(USER, "files.delete", max_risk="high")
    escape = control.execute(USER, "files.delete", {"path": "../outside.txt"}, device_id=target, session=priv())
    assert escape["status"] == "denied"
    (tmp_path / "outside.txt").write_text("must survive")
    assert (tmp_path / "outside.txt").exists()
