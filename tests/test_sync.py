"""Replication: offline queueing, resumability, idempotency, convergent conflicts."""

from __future__ import annotations

import copy
import json
import time

import pytest
from jarvis.db import Database
from jarvis.sync import LocalReplicaTransport, SyncClient, SyncServer, rebuild_projections


def make_device(tmp_path, name: str, settings) -> tuple[Database, SyncClient]:
    from jarvis.config import Settings

    device_dir = tmp_path / name
    device_dir.mkdir(parents=True, exist_ok=True)
    device_settings = Settings(data_dir=device_dir, device_id=name, secret_key=settings.secret_key)
    db = Database(device_settings.db_path)
    with db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?, '{}')", (time.time(),))
    return db, SyncClient(db, name, None)


def journal(db, device: str, key: str, value: float, ts: float | None = None) -> str:
    op_id = db.append_op(
        device_id=device, user_id="u1", entity="trait", entity_key=key, field=None,
        kind="set", payload={"raw": value, "hits": 1},
    )
    if ts is not None:
        with db.write() as conn:
            conn.execute("UPDATE oplog SET wall_ts=?, lamport=? WHERE op_id=?", (ts, int(ts * 1000), op_id))
            row = conn.execute("SELECT payload FROM outbox WHERE op_id=?", (op_id,)).fetchone()
            if row:
                payload = json.loads(row["payload"])
                payload["wall_ts"] = ts
                payload["lamport"] = int(ts * 1000)
                conn.execute("UPDATE outbox SET payload=? WHERE op_id=?", (json.dumps(payload), op_id))
    return op_id


def trait(db, key: str = "humor"):
    return db.scalar(f"SELECT raw FROM traits WHERE user_id='u1' AND key='{key}'")


def test_local_write_applies_immediately_then_queues(tmp_path, settings):
    db, client = make_device(tmp_path, "laptop", settings)
    journal(db, "laptop", "humor", 0.4)
    assert trait(db) == pytest.approx(0.4), "must not wait for the network to take effect"
    assert client.pending_count() == 1


def test_push_is_idempotent_and_replays_are_harmless(tmp_path, settings):
    db, _ = make_device(tmp_path, "laptop", settings)
    journal(db, "laptop", "humor", 0.4)
    replica = Database(tmp_path / "replica.db")
    server = SyncServer(replica)
    ops = [json.loads(r["payload"]) for r in db.query("SELECT payload FROM outbox")]
    first = server.ingest("laptop", copy.deepcopy(ops))
    again = server.ingest("laptop", copy.deepcopy(ops))
    assert first["accepted"] == 1
    assert again["accepted"] == 1 and again["conflicts"] == 0
    assert replica.scalar("SELECT COUNT(*) FROM oplog") == 1, "duplicate op_id must not double-apply"


def test_outbox_drains_only_after_confirmation(tmp_path, settings):
    db, client = make_device(tmp_path, "laptop", settings)
    transport = FailingTransport()
    client.transport = transport
    journal(db, "laptop", "humor", 0.4)
    report = client.sync("u1")
    assert report.online is False and report.pending == 1, "an unreachable peer must keep the queue"
    client.transport = LocalReplicaTransport(Database(tmp_path / "replica.db"))
    report = client.sync("u1")
    assert report.pushed == 1 and report.pending == 0


def test_order_independence_two_devices_same_register(tmp_path, settings):
    """The core convergence claim: same final state whichever device syncs first."""
    a, _ = make_device(tmp_path, "alpha", settings)
    b, _ = make_device(tmp_path, "beta", settings)
    _shared = tmp_path / "replica.db"
    journal(a, "alpha", "humor", 0.9, ts=1000.0)
    journal(b, "beta", "humor", -0.7, ts=2000.0)

    for first, second in ((a, b), (b, a)):
        first_name = "alpha" if first is a else "beta"
        second_name = "beta" if first is a else "alpha"
        replica = Database(tmp_path / f"server-{first_name}.db")
        c1 = SyncClient(first, first_name, LocalReplicaTransport(replica))
        c2 = SyncClient(second, second_name, LocalReplicaTransport(replica))
        c1.sync("u1")
        c2.sync("u1")
        c1.sync("u1")
        assert trait(first) == pytest.approx(-0.7), "newer write wins"
        assert trait(second) == pytest.approx(-0.7), "the other device converges to the same value"


def test_different_registers_both_survive(tmp_path, settings):
    a, _ = make_device(tmp_path, "alpha", settings)
    b, _ = make_device(tmp_path, "beta", settings)
    journal(a, "alpha", "humor", 0.9, ts=1000.0)
    journal(b, "beta", "verbosity", -0.3, ts=1100.0)
    replica = Database(tmp_path / "replica2.db")
    SyncClient(a, "alpha", LocalReplicaTransport(replica)).sync("u1")
    SyncClient(b, "beta", LocalReplicaTransport(replica)).sync("u1")
    SyncClient(a, "alpha", LocalReplicaTransport(replica)).sync("u1")
    assert trait(a, "humor") == pytest.approx(0.9)
    assert trait(a, "verbosity") == pytest.approx(-0.3), "unrelated registers must not clobber each other"
    assert trait(b, "humor") == pytest.approx(0.9)


def test_tombstone_beats_older_live_write(tmp_path, settings):
    a, _ = make_device(tmp_path, "alpha", settings)
    b, _ = make_device(tmp_path, "beta", settings)
    journal(a, "alpha", "humor", 0.5, ts=1000.0)
    # a delete at t=2000 on the other device, which had been offline the whole time
    delete_id = b.append_op(device_id="beta", user_id="u1", entity="trait", entity_key="humor", field=None, kind="delete", payload={})
    with b.write() as conn:
        conn.execute("UPDATE oplog SET wall_ts=2000, lamport=2000000 WHERE op_id=?", (delete_id,))
        conn.execute("UPDATE outbox SET payload=json_set(payload,'$.wall_ts',2000,'$.lamport',2000000) WHERE op_id=?", (delete_id,))
    replica = Database(tmp_path / "replica3.db")
    SyncClient(a, "alpha", LocalReplicaTransport(replica)).sync("u1")
    SyncClient(b, "beta", LocalReplicaTransport(replica)).sync("u1")
    SyncClient(a, "alpha", LocalReplicaTransport(replica)).sync("u1")
    assert trait(a) is None, "the delete must win and not be resurrected by the stale value"
    assert trait(b) is None


def test_cursor_resumes_exactly_where_it_stopped(tmp_path, settings):
    a, _ = make_device(tmp_path, "alpha", settings)
    b, _ = make_device(tmp_path, "beta", settings)
    replica = Database(tmp_path / "replica4.db")
    ca = SyncClient(a, "alpha", LocalReplicaTransport(replica))
    cb = SyncClient(b, "beta", LocalReplicaTransport(replica))
    for i in range(5):
        journal(a, "alpha", f"t{i}", 0.1 * i)
    first = ca.sync("u1")
    assert first.pushed == 5 and first.pulled == 0, "own ops are not echoed back as new work"
    second = ca.sync("u1")
    assert second.pushed == 0 and second.applied == 0, "an idle sync must be a no-op"
    assert ca.cursor() == first.detail["cursor"]
    journal(b, "beta", "from_peer", 0.5)
    cb.sync("u1")
    third = ca.sync("u1")
    assert third.pulled == 1 and third.applied == 1, "exactly the one op the peer added since the cursor"
    assert trait(a, "from_peer") == pytest.approx(0.5)
    assert ca.sync("u1").pulled == 0, "and the cursor advanced past it"


def test_lamport_clock_never_goes_backwards_across_restart(tmp_path, settings):

    db = Database(tmp_path / "restart.db")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    SyncClient(db, "alpha", LocalReplicaTransport(Database(tmp_path / "r2.db")))
    journal(db, "alpha", "humor", 0.2)
    before = db.lamport
    reopened = Database(tmp_path / "restart.db")
    assert reopened.lamport == before, "clock must be recovered from the log, not reset"
    reopened.tick_lamport(observed=before + 50)
    assert reopened.lamport == before + 51


def test_malformed_op_is_rejected_without_blocking_the_queue(tmp_path, settings):
    _, _ = make_device(tmp_path, "alpha", settings)
    replica = Database(tmp_path / "replica5.db")
    server = SyncServer(replica)
    result = server.ingest("alpha", [{"op_id": "bad1", "device_id": "alpha", "wall_ts": 1.0, "lamport": 1}])
    assert result["rejected"], "an op without entity/entity_key must be reported, not crash the sync"
    assert replica.scalar("SELECT COUNT(*) FROM oplog") == 0


def test_rebuild_projections_replays_the_log(tmp_path, settings):
    db, _client = make_device(tmp_path, "alpha", settings)
    journal(db, "alpha", "humor", 0.42)
    journal(db, "alpha", "humor", 0.77, ts=time.time() + 10)
    with db.write() as conn:
        conn.execute("DELETE FROM traits")
    assert trait(db) is None
    applied = rebuild_projections(db)
    assert applied >= 1
    assert trait(db) == pytest.approx(0.77), "rebuild must replay the winning op only"


def test_pull_excludes_the_requesting_device(tmp_path, settings):
    db, _ = make_device(tmp_path, "alpha", settings)
    replica_db = Database(tmp_path / "replica6.db")
    journal(db, "alpha", "humor", 0.4)
    SyncClient(db, "alpha", LocalReplicaTransport(replica_db)).sync("u1")
    server = SyncServer(replica_db)
    echoed = server.changes_since(0, "u1")
    filtered = server.changes_since(0, "u1", exclude_device="alpha")
    assert len(echoed["ops"]) == 1 and len(filtered["ops"]) == 0


class FailingTransport:
    def push(self, device_id, payload):
        raise ConnectionError("no route to replica")

    def pull(self, device_id, params):
        raise ConnectionError("no route to replica")


class _RefusingTransport:
    """Pretends the replica rejected everything, to exercise the dead-letter path."""

    def __init__(self):
        self.pushes = 0

    def push(self, device_id, payload):
        self.pushes += 1
        return {
            "accepted": 0,
            "conflicts": 0,
            "cursor": 0,
            "server_lamport": 0,
            "rejected": [{"op_id": op["op_id"], "error": "entity not recognised"} for op in payload["ops"]],
        }

    def pull(self, device_id, cursor, limit=500):
        return {"ops": [], "cursor": cursor, "has_more": False, "server_max_seq": cursor, "server_lamport": 0}


def test_refused_ops_are_dead_lettered_not_vanished(tmp_path, settings):
    """A rejected op must stop being retried *and* stay visible.

    The first version of this path deleted the outbox row and then tried to UPDATE it
    with the error — a no-op that lost every trace of a change the peer refused. So the
    assertion is about the record surviving, not just about the queue draining.
    """
    db, _ = make_device(tmp_path, "alpha", settings)
    op_id = journal(db, "alpha", "humor", 0.5)
    transport = _RefusingTransport()
    client = SyncClient(db, "alpha", transport)

    report = client.sync("u1")
    assert report.rejected == 1, report
    assert client.pending_count() == 0, "the queue must drain; a poisoned op cannot block sync forever"

    letters = client.dead_letters()
    assert len(letters) == 1 and letters[0]["op_id"] == op_id
    assert "entity not recognised" in letters[0]["error"]
    assert letters[0]["entity"] == "trait" and letters[0]["entity_key"] == "humor"
    assert json.loads(letters[0]["payload"])["raw"] == 0.5, "the change itself must be recoverable"

    # a second sync must not re-push what was already refused
    pushes_before = transport.pushes
    client.sync("u1")
    assert transport.pushes == pushes_before

    # ...and an explicit retry puts it back in the queue with a fresh ordering key
    assert client.retry_dead_letters() == 1
    assert client.dead_letter_count() == 0
    assert client.pending_count() == 1
    client.sync("u1")
    assert transport.pushes == pushes_before + 1


def test_dead_letter_count_is_per_device(tmp_path, settings):
    """A refusal on the phone is not a refusal on the laptop: counts must not bleed."""
    db, _ = make_device(tmp_path, "alpha", settings)
    journal(db, "alpha", "humor", 0.5)
    journal(db, "beta", "warmth", 0.5)
    SyncClient(db, "alpha", _RefusingTransport()).sync("u1")
    assert SyncClient(db, "alpha", None).dead_letter_count() == 1
    assert SyncClient(db, "beta", None).dead_letter_count() == 0
