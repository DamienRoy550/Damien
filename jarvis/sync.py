"""Two-way replication: offline queueing, resumable sync, convergent conflicts.

Model
-----
Replicated state is a **LWW-Element-Set over ops**, where the tuple
``(entity, entity_key, field)`` is a register and the winning op is the one with
the greatest ``(wall_ts, lamport)``.

The op-log *is* the state; the domain tables (``traits``, ``memories`` ...) are a
materialised projection of it. Convergence therefore does not depend on arrival
order: when a late op arrives for a register, we recompute the winner and only
re-project the tables if the winner actually changed. Two devices that both edit
the same preference while offline end up with the same projection after either
syncs first.

Design notes
------------
* Tombstones (``kind='delete'``) beat live values so a delete performed offline
  is not resurrected by a peer's stale write with an older timestamp.
* ``device_seq`` gives each device a gap-free local stream, which makes push
  resumable: the client only drops an outbox row once the server confirms it.
* Lamport clocks are merged on both push and pull, so causal order survives
  device clocks being wrong (a real problem for offline hardware).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# Projection targets: which op entities we fold onto tables, and how.
PROJECTED_ENTITIES = {
    "user_profile",
    "trait",
    "interest",
    "memory",
    "controlled_device",
    "control_rule",
    "device_trust",
    "voice_enrollment",
}


@dataclass
class SyncReport:
    pushed: int = 0
    pulled: int = 0
    applied: int = 0
    conflicts: int = 0
    rejected: int = 0
    pending: int = 0
    online: bool = True
    error: str | None = None
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.pushed,
            "pulled": self.pulled,
            "applied": self.applied,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "pending": self.pending,
            "online": self.online,
            "error": self.error,
            "duration_ms": self.duration_ms,
            **self.detail,
        }


# ---------------------------------------------------------------------------
# materialisation (server-side and client-side share this code path)
# ---------------------------------------------------------------------------
def is_winner(db, conn, op: dict) -> bool:
    """True when ``op`` is the current winning write for its register."""
    row = conn.execute(
        """SELECT op_id FROM oplog
           WHERE entity=? AND entity_key=? AND COALESCE(field,'')=COALESCE(?,'')
           ORDER BY wall_ts DESC, lamport DESC, op_id DESC LIMIT 1""",
        (op["entity"], op["entity_key"], op.get("field")),
    ).fetchone()
    return bool(row) and row[0] == op["op_id"]


def materialize(db, conn, op: dict) -> None:
    """Fold one op into the domain tables (only if it wins its register)."""
    entity = op["entity"]
    if entity not in PROJECTED_ENTITIES:
        return
    if not is_winner(db, conn, op):
        return

    key = op["entity_key"]
    payload = op.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    user_id = op.get("user_id")
    deleted = op.get("kind") == "delete"
    ts = float(op.get("wall_ts", time.time()))

    if entity == "trait":
        if deleted:
            conn.execute("DELETE FROM traits WHERE user_id=? AND key=?", (user_id, key))
        else:
            conn.execute(
                """INSERT INTO traits(user_id, key, raw, hits, updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, key) DO UPDATE SET
                     raw=excluded.raw, hits=excluded.hits, updated_at=excluded.updated_at""",
                (user_id, key, float(payload.get("raw", 0)), int(payload.get("hits", 0)), ts),
            )
    elif entity == "interest":
        if deleted:
            conn.execute("DELETE FROM interests WHERE user_id=? AND topic=?", (user_id, key))
        else:
            conn.execute(
                """INSERT INTO interests(user_id, topic, weight, hits, last_seen) VALUES(?,?,?,?,?)
                   ON CONFLICT(user_id, topic) DO UPDATE SET
                     weight=excluded.weight, hits=excluded.hits, last_seen=excluded.last_seen""",
                (user_id, key, float(payload.get("weight", 1.0)), int(payload.get("hits", 0)), ts),
            )
    elif entity == "user_profile":
        # field-level register: key = "<user_id>/<field>"
        uid, _, fname = key.partition("/")
        row = conn.execute("SELECT profile_json FROM users WHERE id=?", (uid,)).fetchone()
        profile = json.loads(row["profile_json"]) if row and row["profile_json"] else {}
        if deleted:
            profile.pop(fname, None)
        else:
            profile[fname] = payload.get("value")
        conn.execute(
            "UPDATE users SET profile_json=? WHERE id=?", (json.dumps(profile, sort_keys=True), uid)
        )
        if fname == "display_name":
            name = payload.get("value")
            if not deleted and name:
                conn.execute("UPDATE users SET display_name=? WHERE id=?", (str(name), uid))
    elif entity == "memory":
        if deleted:
            conn.execute("UPDATE memories SET deleted_at=? WHERE id=?", (ts, key))
        else:
            conn.execute(
                """INSERT INTO memories(id, user_id, body, tags, source, created_at, strength)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     body=excluded.body, tags=excluded.tags, source=excluded.source,
                     strength=excluded.strength""",
                (
                    key,
                    user_id,
                    str(payload.get("body", "")),
                    json.dumps(payload.get("tags", [])),
                    str(payload.get("source", "user")),
                    ts,
                    float(payload.get("strength", 1.0)),
                ),
            )
    elif entity == "controlled_device":
        if deleted:
            conn.execute("DELETE FROM controlled_devices WHERE id=?", (key,))
        else:
            conn.execute(
                """INSERT INTO controlled_devices(id, user_id, name, kind, endpoint, capabilities,
                       enabled, created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, kind=excluded.kind, endpoint=excluded.endpoint,
                     capabilities=excluded.capabilities, enabled=excluded.enabled""",
                (
                    key,
                    user_id,
                    str(payload.get("name", key)),
                    str(payload.get("kind", "laptop")),
                    str(payload.get("endpoint", "local")),
                    json.dumps(payload.get("capabilities", [])),
                    1 if payload.get("enabled", True) else 0,
                    ts,
                ),
            )
    elif entity == "control_rule":
        if deleted:
            conn.execute("DELETE FROM control_rules WHERE id=?", (key,))
        else:
            conn.execute(
                """INSERT INTO control_rules(id, user_id, capability, allow, max_risk, config, created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     capability=excluded.capability, allow=excluded.allow,
                     max_risk=excluded.max_risk, config=excluded.config""",
                (
                    key,
                    user_id,
                    str(payload.get("capability", "*")),
                    1 if payload.get("allow", True) else 0,
                    str(payload.get("max_risk", "low")),
                    json.dumps(payload.get("config", {})),
                    ts,
                ),
            )
    elif entity == "device_trust":
        conn.execute(
            "UPDATE devices SET trust_level=?, trusted_by=?, last_seen=? WHERE id=?",
            (
                "revoked" if deleted else str(payload.get("trust_level", "trusted")),
                str(payload.get("trusted_by", "sync")),
                ts,
                key,
            ),
        )
    elif entity == "voice_enrollment":
        if deleted:
            conn.execute("UPDATE users SET voiceprint_model=NULL WHERE id=?", (key,))
        else:
            conn.execute(
                "UPDATE users SET voiceprint_model=?, voiceprint_enrolled_at=? WHERE id=?",
                (json.dumps(payload.get("model", {})), ts, key),
            )


def rebuild_projections(db) -> int:
    """Replay winning ops onto the projection tables.

    Needed after a schema change, or when a projection was written before the
    table existed. Deterministic: exactly one winner per register, chosen by
    ``(wall_ts, lamport)``.
    """
    with db.write() as conn:
        for table in ("traits", "interests", "memories", "controlled_devices", "control_rules"):
            # table names come from the literal tuple above, never from input
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        # Every winning op for a projected register: one row per (entity, entity_key, field)
        # chosen by (wall_ts, lamport). Entities are filtered in Python rather than with a
        # dynamic IN(...) list — the set is a fixed internal tuple, and this keeps the SQL literal.
        rows = conn.execute(
            """SELECT o.op_id, o.device_id, o.user_id, o.entity, o.entity_key, o.field, o.kind,
                      o.payload, o.wall_ts, o.lamport
               FROM oplog o
               JOIN (
                 SELECT entity, entity_key, COALESCE(field,'') AS f,
                        MAX(wall_ts * 100000000000 + lamport) AS rank
                 FROM oplog GROUP BY entity, entity_key, f
               ) w ON w.entity = o.entity AND w.entity_key = o.entity_key
                  AND COALESCE(o.field,'') = w.f
                  AND (o.wall_ts * 100000000000 + o.lamport) = w.rank
               ORDER BY o.seq"""
        ).fetchall()
        applied = 0
        for row in rows:
            op = {
                "op_id": row["op_id"], "device_id": row["device_id"], "user_id": row["user_id"],
                "entity": row["entity"], "entity_key": row["entity_key"], "field": row["field"],
                "kind": row["kind"], "payload": json.loads(row["payload"]), "wall_ts": row["wall_ts"],
                "lamport": row["lamport"],
            }
            if op["entity"] not in PROJECTED_ENTITIES:
                continue
            materialize(db, conn, op)
            applied += 1
    return applied


# ---------------------------------------------------------------------------
# server side (the replica a device pushes to / pulls from)
# ---------------------------------------------------------------------------
class SyncServer:
    """Replica endpoint. Stateless per call, so any HTTP transport can front it."""

    def __init__(self, db):
        self.db = db

    def ingest(self, device_id: str, ops: list[dict], *, observed_lamport: int = 0) -> dict:
        """Accept a client batch. Idempotent; returns the client's next cursor."""
        accepted, rejected, conflicts = 0, [], 0
        with self.db.write() as conn:
            for op in ops:
                try:
                    existing = conn.execute(
                        "SELECT op_id, payload FROM oplog WHERE op_id=?", (op["op_id"],)
                    ).fetchone()
                    if existing:
                        accepted += 1  # already have it: client can drop from outbox
                        continue
                    prev = conn.execute(
                        """SELECT op_id, wall_ts, lamport FROM oplog
                           WHERE entity=? AND entity_key=? AND COALESCE(field,'')=COALESCE(?,'')
                           ORDER BY wall_ts DESC, lamport DESC LIMIT 1""",
                        (op["entity"], op["entity_key"], op.get("field")),
                    ).fetchone()
                    if prev and (float(prev["wall_ts"]), int(prev["lamport"])) > (
                        float(op["wall_ts"]),
                        int(op["lamport"]),
                    ):
                        conflicts += 1
                    self.db.tick_lamport(max(int(op.get("lamport", 0)), observed_lamport))
                    conn.execute(
                        """INSERT INTO oplog(op_id, device_id, device_seq, user_id, entity, entity_key,
                               field, kind, payload, wall_ts, lamport, base_lamport, applied_at, origin)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'push')""",
                        (
                            op["op_id"],
                            op.get("device_id", device_id),
                            int(op.get("device_seq", 0)),
                            op.get("user_id"),
                            op["entity"],
                            op["entity_key"],
                            op.get("field"),
                            op.get("kind", "set"),
                            json.dumps(op.get("payload", {}), sort_keys=True),
                            float(op["wall_ts"]),
                            int(op.get("lamport", self.db.lamport)),
                            int(op.get("base_lamport", 0)),
                            time.time(),
                        ),
                    )
                    materialize(self.db, conn, op)
                    accepted += 1
                except Exception as exc:  # malformed op: tell the client so it stops retrying
                    rejected.append({"op_id": op.get("op_id"), "error": f"{type(exc).__name__}: {exc}"})
        last_seq = self.db.scalar("SELECT COALESCE(MAX(seq),0) FROM oplog", (), 0)
        return {
            "accepted": accepted,
            "conflicts": conflicts,
            "rejected": rejected,
            "cursor": int(last_seq or 0),
            "server_lamport": self.db.lamport,
        }

    def changes_since(self, after_seq: int, user_id: str | None = None, limit: int = 500, *, exclude_device: str | None = None) -> dict:
        params: list[Any] = [after_seq]
        where = "seq > ?"
        if exclude_device:
            where += " AND device_id <> ?"
            params.append(exclude_device)
        if user_id:
            where += " AND (user_id IS NULL OR user_id = ?)"
            params.append(user_id)
        # `where` is assembled from fixed literals with ? placeholders, never from input
        rows = self.db.query(
            f"""SELECT seq, op_id, device_id, device_seq, user_id, entity, entity_key, field,
                       kind, payload, wall_ts, lamport, base_lamport
                FROM oplog WHERE {where} ORDER BY seq ASC LIMIT ?""",  # noqa: S608
            (*params, limit),
        )
        ops = []
        for r in rows:
            ops.append(
                {
                    "seq": r["seq"],
                    "op_id": r["op_id"],
                    "device_id": r["device_id"],
                    "device_seq": r["device_seq"],
                    "user_id": r["user_id"],
                    "entity": r["entity"],
                    "entity_key": r["entity_key"],
                    "field": r["field"],
                    "kind": r["kind"],
                    "payload": json.loads(r["payload"]),
                    "wall_ts": r["wall_ts"],
                    "lamport": r["lamport"],
                    "base_lamport": r["base_lamport"],
                }
            )
        max_seq = self.db.scalar("SELECT COALESCE(MAX(seq),0) FROM oplog", (), 0)
        return {"ops": ops, "cursor": int(rows[-1]["seq"]) if rows else int(after_seq), "has_more": len(ops) == limit, "server_max_seq": int(max_seq or 0), "server_lamport": self.db.lamport}


# ---------------------------------------------------------------------------
# client side
# ---------------------------------------------------------------------------
class SyncClient:
    """Queues ops while offline and reconciles when a transport is reachable.

    ``transport`` is anything exposing ``push(device_id, payload)`` and
    ``pull(device_id, cursor) -> dict``. The HTTP client and the in-process
    replica both satisfy it, so offline behaviour is testable without a network.
    """

    def __init__(self, db, device_id: str, transport, *, batch_limit: int = 500):
        self.db = db
        self.device_id = device_id
        self.transport = transport
        self.batch_limit = batch_limit

    def _note_error(self, message: str) -> None:
        """Persist why the last sync failed, on the same row the status endpoint reads."""
        try:
            with self.db.write() as conn:
                conn.execute(
                    """INSERT INTO sync_state(peer, last_error) VALUES(?,?)
                       ON CONFLICT(peer) DO UPDATE SET last_error=excluded.last_error""",
                    (self.peer_key(), message[:500]),
                )
        except Exception:  # never let the error report itself be the thing that fails
            pass

    def last_error(self) -> str | None:
        row = self.db.one("SELECT last_error FROM sync_state WHERE peer=?", (self.peer_key(),))
        return row["last_error"] if row else None

    def clear_error(self) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE sync_state SET last_error=NULL WHERE peer=?", (self.peer_key(),))

    def dead_letter_count(self) -> int:
        """Ops the replica refused. Non-zero means a local change never reached the peer,
        and the reason is stored next to it — see :meth:`dead_letters`."""
        return int(self.db.scalar("SELECT COUNT(*) FROM sync_dead_letters WHERE device_id=?", (self.device_id,), 0) or 0)

    def dead_letters(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM sync_dead_letters WHERE device_id=? ORDER BY rejected_at DESC LIMIT ?",
            (self.device_id, limit),
        )
        return [dict(r) for r in rows]

    def retry_dead_letters(self) -> int:
        """Put refused ops back in the queue (after fixing the cause, e.g. a schema or
        payload bug). Returns how many were requeued; the rows are cleared either way."""
        rows = self.db.query("SELECT op_id, payload FROM sync_dead_letters WHERE device_id=?", (self.device_id,))
        if not rows:
            return 0
        with self.db.write() as conn:
            seq = int(conn.execute("SELECT COALESCE(MAX(device_seq),0) FROM outbox WHERE device_id=?", (self.device_id,)).fetchone()[0] or 0)
            for r in rows:
                seq += 1  # device_seq is the per-device ordering key; reusing 0 would collide
                conn.execute(
                    """INSERT OR IGNORE INTO outbox(op_id, device_id, device_seq, payload, queued_at, attempts, last_error)
                       VALUES(?,?,?,?,?,0,NULL)""",
                    (r["op_id"], self.device_id, seq, r["payload"], time.time()),
                )
            conn.execute("DELETE FROM sync_dead_letters WHERE device_id=?", (self.device_id,))
        return len(rows)

    def pending_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM outbox WHERE device_id=?", (self.device_id,), 0) or 0)

    def cursor(self) -> int:
        return int(
            self.db.scalar(
                "SELECT COALESCE(last_applied_seq,0) FROM sync_state WHERE peer=?", (self.peer_key(),), 0
            )
            or 0
        )

    def peer_key(self) -> str:
        return "server"

    def sync(self, user_id: str | None = None) -> SyncReport:
        started = time.time()
        report = SyncReport(pending=self.pending_count())
        try:
            report.pushed, report.rejected = self._push(user_id)
            report.pulled, report.applied, report.conflicts = self._pull(user_id)
        except OfflineError as exc:
            report.online = False
            report.error = str(exc)
            self._note_error(str(exc))
        except Exception as exc:
            # A transport that raises anything unexpected is still treated as unreachable,
            # so a bad network can never lose a change — but the exception is recorded, not
            # swallowed. Without this a programming bug and an airplane mode look identical
            # from the outside, and "pending ops" stays mysterious forever.
            report.online = False
            report.error = f"{type(exc).__name__}: {exc}"
            self._note_error(report.error)
        else:
            # a clean run makes the previous failure note stale, and a stale note next to
            # "in sync" is worse than no note at all
            if self.last_error():
                self.clear_error()
        report.pending = self.pending_count()
        report.duration_ms = int((time.time() - started) * 1000)
        report.detail = {"cursor": self.cursor(), "device_id": self.device_id}
        return report

    def _push(self, user_id: str | None) -> tuple[int, int]:
        """Send the queue. Returns (accepted, rejected).

        The rejected count is part of the return value rather than a log line because a
        refused op is a change that will never arrive: the caller has to show it.
        """
        rows = self.db.query(
            "SELECT op_id, device_seq, payload FROM outbox WHERE device_id=? ORDER BY device_seq ASC LIMIT ?",
            (self.device_id, self.batch_limit),
        )
        if not rows:
            return 0, 0
        batch = []
        for r in rows:
            op = json.loads(r["payload"])
            op["op_id"] = r["op_id"]
            batch.append(op)
        result = self.transport.push(
            self.device_id,
            {"ops": batch, "observed_lamport": self.db.lamport, "user_id": user_id},
        )
        accepted_ids = {op["op_id"] for op in batch}
        by_id = {op["op_id"]: op for op in batch}
        rejections = {
            str(r["op_id"]): str(r.get("error") or "rejected by the replica")
            for r in result.get("rejected", [])
            if isinstance(r, dict) and r.get("op_id")
        }
        rejected = set(rejections)
        with self.db.write() as conn:
            for op_id, error in rejections.items():
                op = by_id.get(op_id, {})
                # Dead-letter first, then drop: the queue must drain, but the change and
                # the reason must survive locally so this is diagnosable after the fact.
                conn.execute(
                    """INSERT OR REPLACE INTO sync_dead_letters
                       (op_id, device_id, user_id, entity, entity_key, error, payload, rejected_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        op_id,
                        self.device_id,
                        op.get("user_id"),
                        op.get("entity"),
                        op.get("entity_key"),
                        error[:500],
                        json.dumps(op.get("payload", {}), sort_keys=True)[:4000],
                        time.time(),
                    ),
                )
            for op_id in accepted_ids:
                conn.execute("DELETE FROM outbox WHERE op_id=?", (op_id,))
            conn.execute(
                """INSERT INTO sync_state(peer, last_push_at, server_lamport) VALUES(?,?,?)
                   ON CONFLICT(peer) DO UPDATE SET last_push_at=excluded.last_push_at,
                     server_lamport=MAX(sync_state.server_lamport, excluded.server_lamport)""",
                (self.peer_key(), time.time(), int(result.get("server_lamport", 0))),
            )
        self.db.tick_lamport(int(result.get("server_lamport", 0)))
        return len(accepted_ids) - len(rejected), len(rejected)

    def _pull(self, user_id: str | None) -> tuple[int, int, int]:
        pulled = applied = conflicts = 0
        for _ in range(50):  # hard page cap
            page = self.transport.pull(
                self.device_id,
                {"cursor": self.cursor(), "user_id": user_id, "limit": self.batch_limit, "exclude_device": self.device_id},
            )
            ops = page.get("ops", [])
            self.db.tick_lamport(int(page.get("server_lamport", 0)))
            if not ops:
                break
            pulled += len(ops)
            for op in ops:
                if self._is_stale(op):
                    conflicts += 1
            applied += self.db.apply_ops(ops, origin="pull", peer=self.peer_key())
            if not page.get("has_more"):
                break
        return pulled, applied, conflicts

    def _is_stale(self, op: dict) -> bool:
        """Did we already hold a newer write for this register? (surfaced as a conflict)"""
        row = self.db.one(
            """SELECT wall_ts, lamport FROM oplog
               WHERE entity=? AND entity_key=? AND COALESCE(field,'')=COALESCE(?,'')
               ORDER BY wall_ts DESC, lamport DESC LIMIT 1""",
            (op["entity"], op["entity_key"], op.get("field")),
        )
        if not row:
            return False
        return (float(row["wall_ts"]), int(row["lamport"])) > (float(op["wall_ts"]), int(op["lamport"]))

class OfflineError(RuntimeError):
    """Raised by transports that cannot reach the replica. Keeps the queue intact."""


class LocalReplicaTransport:
    """Sync against a database in this process.

    Used for tests, for a LAN peer, and as the fallback when no remote replica is
    configured — the sync protocol is exercised identically either way.
    """

    def __init__(self, server_db: Any):
        self.server = SyncServer(server_db)

    def push(self, device_id: str, payload: dict) -> dict:
        return self.server.ingest(device_id, payload["ops"], observed_lamport=payload.get("observed_lamport", 0))

    def pull(self, device_id: str, params: dict) -> dict:
        return self.server.changes_since(
            int(params.get("cursor", 0)), params.get("user_id"), int(params.get("limit", 500)),
            exclude_device=params.get("exclude_device"),
        )
