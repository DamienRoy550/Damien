"""SQLite access layer.

The database is the *source of truth even when offline*: every mutable write goes
through it as an operation-log entry, so a device that has been unreachable for a
week still behaves normally and converges later. Server-side and device-side state
share this schema, which is what makes two-way sync tractable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    locale TEXT NOT NULL DEFAULT 'en',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    voiceprint_model TEXT,          -- JSON, device-cached; enables offline speaker verify
    voiceprint_enrolled_at REAL,
    profile_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS identities (
    provider TEXT NOT NULL,         -- google | apple | microsoft
    subject TEXT NOT NULL,          -- stable id from the id_token `sub`
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    linked_at REAL NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);
CREATE INDEX IF NOT EXISTS idx_identities_email ON identities(provider, email);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    last_seen REAL NOT NULL,
    last_ip TEXT,
    trust_level TEXT NOT NULL DEFAULT 'untrusted',   -- untrusted | trusted | revoked
    trusted_by TEXT,
    app_version TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]'          -- JSON list: mic, camera, notifications...
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    refresh_hash TEXT,
    refresh_expires_at REAL,
    scope TEXT NOT NULL DEFAULT 'basic',      -- basic | privileged (voiceprint-verified)
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

/* ---- adaptive learning ------------------------------------------------ */
/* The tables below are projections of the op-log, not the authority. They
   deliberately carry no foreign key onto users: during replication an op can
   legitimately arrive before the user row it belongs to, and a projection must
   never reject data the log already holds. Rebuild them with
   ``jarvis.sync.rebuild_projections`` after a schema change. */
CREATE TABLE IF NOT EXISTS traits (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    raw REAL NOT NULL DEFAULT 0,            -- pre-decay learned value, bounded
    hits INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS interests (
    user_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1,
    hits INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL,
    PRIMARY KEY (user_id, topic)
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,                     -- request | response | correction
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_interactions_user_time ON interactions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    interaction_id TEXT,
    valence INTEGER NOT NULL,               -- +1 / -1
    created_at REAL NOT NULL,
    attributed_keys TEXT NOT NULL DEFAULT '[]'
);

/* ---- long-term memory (offline retrieval corpus) --------------------- */
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    strength REAL NOT NULL DEFAULT 1.0,
    last_recall REAL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, deleted_at);

/* ---- replication / sync ---------------------------------------------- */
CREATE TABLE IF NOT EXISTS oplog (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL,
    user_id TEXT,
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    field TEXT,
    kind TEXT NOT NULL DEFAULT 'set',       -- set | delete
    payload TEXT NOT NULL,
    wall_ts REAL NOT NULL,
    lamport INTEGER NOT NULL,
    base_lamport INTEGER NOT NULL DEFAULT 0,
    applied_at REAL NOT NULL,
    origin TEXT NOT NULL DEFAULT 'local'    -- local | pull
);
CREATE INDEX IF NOT EXISTS idx_oplog_user ON oplog(user_id, seq);
CREATE INDEX IF NOT EXISTS idx_oplog_dedup ON oplog(device_id, device_seq);

CREATE TABLE IF NOT EXISTS outbox (
    op_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    queued_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

/* An op the replica refused is dropped from the outbox so the queue can drain, but it is
   never simply discarded: it lands here, where the user can actually see it. Without this
   table a rejected change silently stops existing on every other device. */
CREATE TABLE IF NOT EXISTS sync_dead_letters (
    op_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    user_id TEXT,
    entity TEXT,
    entity_key TEXT,
    error TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    rejected_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dead_letters_time ON sync_dead_letters(rejected_at DESC);

CREATE TABLE IF NOT EXISTS sync_state (
    peer TEXT PRIMARY KEY,
    last_applied_seq INTEGER NOT NULL DEFAULT 0,
    last_pull_at REAL,
    last_push_at REAL,
    server_lamport INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

/* ---- device control --------------------------------------------------- */
CREATE TABLE IF NOT EXISTS controlled_devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- laptop | phone | tv | light | speaker...
    endpoint TEXT NOT NULL DEFAULT 'local', -- local | lan:<host> | adapter:<name>
    capabilities TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    pairing_verified_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ctrl_user ON controlled_devices(user_id);

CREATE TABLE IF NOT EXISTS control_rules (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    allow INTEGER NOT NULL DEFAULT 1,
    max_risk TEXT NOT NULL DEFAULT 'low',
    config TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS control_audit (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_id TEXT,
    action TEXT NOT NULL,
    target TEXT,
    args TEXT NOT NULL DEFAULT '{}',
    decision TEXT NOT NULL,                 -- executed | denied | dry-run | needs-confirmation
    reason TEXT,
    risk TEXT,
    created_at REAL NOT NULL,
    duration_ms INTEGER,
    exit_status TEXT,
    output TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user_time ON control_audit(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS confirmations (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

/* ---- generated media -------------------------------------------------- */
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- image | video
    prompt TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,                   -- ready | failed
    path TEXT,
    mime TEXT,
    width INTEGER,
    height INTEGER,
    duration_ms INTEGER,
    params TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user_time ON artifacts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'Assistant',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    device_id TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
"""


class Database:
    """Small, thread-safe wrapper. One connection per thread, WAL for concurrency."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._lamport = 0
        self._init()

    # --- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._write_lock:
            self.conn.executescript(_SCHEMA)
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
            # recover the Lamport clock so it never goes backwards across restarts
            r = self.conn.execute("SELECT MAX(lamport) FROM oplog").fetchone()
            self._lamport = int(r[0] or 0)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # --- helpers ----------------------------------------------------------
    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence = (), default=None):
        row = self.one(sql, params)
        if row is None:
            return default
        value = row[0]
        return value if value is not None else default

    # --- op log -----------------------------------------------------------
    def tick_lamport(self, observed: int | None = None) -> int:
        with self._write_lock:
            if observed is not None and observed > self._lamport:
                self._lamport = observed
            self._lamport += 1
            return self._lamport

    @property
    def lamport(self) -> int:
        return self._lamport

    def append_op(
        self,
        *,
        device_id: str,
        user_id: str | None,
        entity: str,
        entity_key: str,
        field: str | None,
        kind: str,
        payload: dict,
        op_id: str | None = None,
        dedupe: bool = True,
        project: bool = True,
    ) -> str | None:
        """Journal a local mutation, project it, and queue it for push.

        Idempotent per ``op_id``; returns ``None`` when the op was already present.
        ``project=False`` writes only the log (used by replay paths).
        """
        op_id = op_id or uuid.uuid4().hex
        now = time.time()
        with self._write_lock:
            device_seq = int(
                self.scalar(
                    "SELECT COALESCE(MAX(device_seq), 0) FROM oplog WHERE device_id=?",
                    (device_id,),
                    0,
                )
            ) + 1
            lamport = self.tick_lamport()
            row = {
                "op_id": op_id,
                "device_id": device_id,
                "device_seq": device_seq,
                "user_id": user_id,
                "entity": entity,
                "entity_key": entity_key,
                "field": field,
                "kind": kind,
                "payload": payload,
                "wall_ts": now,
                "lamport": lamport,
                "base_lamport": lamport - 1,
            }
            with self.write() as conn:
                if dedupe and conn.execute(
                    "SELECT 1 FROM oplog WHERE op_id=?", (op_id,)
                ).fetchone():
                    return None
                conn.execute(
                    """INSERT INTO oplog(op_id, device_id, device_seq, user_id, entity, entity_key,
                        field, kind, payload, wall_ts, lamport, base_lamport, applied_at, origin)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'local')""",
                    (
                        op_id,
                        device_id,
                        device_seq,
                        user_id,
                        entity,
                        entity_key,
                        field,
                        kind,
                        json.dumps(payload, sort_keys=True),
                        now,
                        lamport,
                        lamport - 1,
                        now,
                    ),
                )
                if device_id != "server":
                    conn.execute(
                        """INSERT OR IGNORE INTO outbox(op_id, device_id, device_seq, payload, queued_at)
                           VALUES(?,?,?,?,?)""",
                        (op_id, device_id, device_seq, json.dumps(row, sort_keys=True), now),
                    )
                if project:
                    # A local mutation is journalled and materialised in the same
                    # transaction, so no caller can forget the projection half and
                    # leave the UI reading a stale table.
                    from jarvis.sync import materialize

                    materialize(self, conn, row)
            return op_id

    def apply_ops(self, ops: Iterable[dict], *, origin: str = "pull", peer: str = "server") -> int:
        """Idempotently fold remote ops in and materialise them onto the tables."""
        applied = 0
        with self.write() as conn:
            for op in ops:
                if conn.execute("SELECT 1 FROM oplog WHERE op_id=?", (op["op_id"],)).fetchone():
                    continue
                conn.execute(
                    """INSERT INTO oplog(op_id, device_id, device_seq, user_id, entity, entity_key,
                       field, kind, payload, wall_ts, lamport, base_lamport, applied_at, origin)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, ?)""",
                    (
                        op["op_id"],
                        op["device_id"],
                        op.get("device_seq", 0),
                        op.get("user_id"),
                        op["entity"],
                        op["entity_key"],
                        op.get("field"),
                        op.get("kind", "set"),
                        json.dumps(op.get("payload", {}), sort_keys=True),
                        op["wall_ts"],
                        op["lamport"],
                        op.get("base_lamport", 0),
                        time.time(),
                        origin,
                    ),
                )
                self._materialize(conn, op)
                applied += 1
            if applied:
                row = conn.execute("SELECT MAX(seq) FROM oplog").fetchone()
                conn.execute(
                    """INSERT INTO sync_state(peer, last_applied_seq, last_pull_at, server_lamport)
                       VALUES(?,?,?,?)
                       ON CONFLICT(peer) DO UPDATE SET
                         last_applied_seq=excluded.last_applied_seq,
                         last_pull_at=excluded.last_pull_at,
                         server_lamport=MAX(sync_state.server_lamport, excluded.server_lamport)""",
                    (peer, row[0] or 0, time.time(), self.lamport),
                )
        return applied

    def _materialize(self, conn: sqlite3.Connection, op: dict) -> None:
        """Write an op onto its table using last-writer-wins by (wall_ts, lamport)."""
        from jarvis import sync as sync_module

        sync_module.materialize(self, conn, op)

    # --- user flags (kill switch etc.) ------------------------------------
    def user_flag(self, user_id: str, key: str, default=None):
        profile = self.jloads(self.scalar("SELECT profile_json FROM users WHERE id=?", (user_id,)), {})
        return profile.get(f"flag:{key}", default)

    def set_user_flag(self, user_id: str, key: str, value, *, device_id: str = "server") -> None:
        """Flags are ordinary profile fields, so e.g. the device-control kill switch set
        on a phone reaches the laptop through the same replication as any other change.
        Written as an op and projected by :func:`jarvis.sync.materialize`, never by hand."""
        self.append_op(
            device_id=device_id,
            user_id=user_id,
            entity="user_profile",
            entity_key=f"{user_id}/flag:{key}",
            field=f"flag:{key}",
            kind="set" if value is not None else "delete",
            payload={"value": value},
        )

    # --- convenience for JSON columns ------------------------------------
    @staticmethod
    def jloads(value, default=None):
        if value in (None, ""):
            return {} if default is None else default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {} if default is None else default
