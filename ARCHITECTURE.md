# Architecture

## The one idea everything else follows from

Jarvis has **one mutable data structure**: an append-only operation journal (`oplog`). Every table
that a feature reads is a projection of that journal, and every cross-device problem is a
replication problem.

```
                 write path
   feature  ──►  Database.append_op(entity, entity_key, field, kind, payload)
                        │
                        ├── INSERT INTO oplog            (the truth, forever)
                        ├── INSERT INTO outbox           (awaiting acknowledgement)
                        └── materialize(op)              (projection, same transaction)
                        │
                 read path
   feature  ◄──  traits / interests / memories / controlled_devices / control_rules / …
```

Three consequences, which is why the design is worth the indirection:

1. **Offline writes are not a special case.** The outbox *is* the offline queue. A write does not
   need the network to be valid; it needs the network only to be shared.
2. **Sync is one algorithm, not N.** New feature ⇒ one `entity` string and a `materialize` branch.
   It inherits conflict resolution, idempotency, replay, and dead-lettering.
3. **Undo/audit/rebuild are free.** `rebuild_projections(db)` empties the projections and replays
   the journal; a projection can be corrupted or deleted and the state comes back identical.

`append_op` does journal + projection in a single transaction, so a crash can never leave a change
in the journal that the local device can't see (or vice versa).

## Register model and conflict resolution

An op addresses a **register**: `(entity, entity_key, field)`. `field` is nullable — a trait
register is `(trait, "humor", NULL)`, a memory tag is `(memory, <id>, "tags")`.

Winners are chosen deterministically:

| Rule | Meaning |
| --- | --- |
| last-writer-wins | by `(wall_ts, lamport)` — wall time first, Lamport clock as the tiebreak for out-of-order arrival |
| tombstones beat older live writes | a delete with `deleted_at` wins over a set that is strictly older, so a device that was offline for a week cannot resurrect deleted memories |
| `op_id` tiebreak | identical `(wall_ts, lamport)` resolves by op id, never by arrival order |
| dedupe by `op_id` | `apply_ops` skips ops already in the journal, so push and pull are idempotent |

Two devices that each write to the same register and then sync through a shared replica converge to
byte-identical projections regardless of the order the replica receives them — that is
`test_order_independence_two_devices_same_register`, and it fails loudly if you ever make the winner
function depend on arrival order.

**Lamport clocks** live in `meta` and tick on both local writes and observed remote clocks
(`Database.tick_lamport`). Wall clocks lie and skew; a monotonic counter attached to each op is what
makes "happened after" meaningful across devices whose clocks disagree by minutes.

## Projection entities

`PROJECTED_ENTITIES` in `jarvis/sync.py`:

`user_profile`, `trait`, `interest`, `memory`, `controlled_device`, `control_rule`,
`device_trust`, `voice_enrollment`

Notably **not** journaled, by decision rather than omission:

- `conversations` / `messages` — raw chat transcripts stay on the device that wrote them. The
  *content* a user asked to remember is a `memory` op and does sync; the turn-by-turn log does not.
  Less surface, and the usual reason a transcript exists locally is audit, not portability.
- `control_audit` — a security log must be append-only on the device that took the action. A log
  that can be synced can be synced *away*.
- `sessions`, `confirmations` — device-local by definition.

Projections have **no foreign key on `users`**. A replica receiving a `traits` row for a user it has
never signed in as must still store the op; if the projection rejected it, the journal and the
projection would diverge forever, and the peer would refuse a legal write. Cross-user *journaling*
is still rejected — but in `SyncServer.ingest`, where the identity of the submitting device is
known, which is the only place that check is meaningful.

## Sync

```
SyncClient(db, device_id, transport, batch_limit)
    .sync(user_id) → SyncReport(pushed, pulled, applied, conflicts, rejected, pending, online, error, duration_ms)
```

`_push` sends up to `batch_limit` unacknowledged ops (oldest `device_seq` first), then, in **one**
transaction: dead-letters the refused ones, deletes the accepted ones from the outbox, and updates
`sync_state(peer, last_push_at, server_lamport)`. Nothing is ever removed from the outbox without
either an acknowledgement or a durable local record of the refusal.

`_pull` asks for `changes_since(cursor, user_id, exclude_device=self.device_id)` and pages until
`has_more` is false, storing the cursor only after a successful apply — so an interrupted pull
resumes exactly where it stopped rather than replaying a page or skipping one. `exclude_device` is
what stops a device from being handed its own ops back (and, with a shared replica, from an
echo-chamber loop).

**Transports** are the seam:

| Transport | Use |
| --- | --- |
| `LocalReplicaTransport(replica_db)` | a shared SQLite file — tests, `jarvis demo`, single-machine multi-device |
| HTTP against `/api/sync/push` + `/api/sync/pull` | device ↔ server |
| your own | anything with `push(device_id, payload) -> dict` and `pull(device_id, params) -> dict` |

`OfflineError` means "unreachable, keep the queue". Any other exception is *also* treated as
keep-the-queue — a transport bug must never lose data — but it is recorded in
`sync_state.last_error` and surfaced in `GET /api/sync/status` and `jarvis sync`, because otherwise
an `OfflineError` and a `NameError` look identical from outside, and "it's just offline" quietly
becomes the answer to every sync complaint.

**Dead letters.** `sync_dead_letters(op_id, device_id, user_id, entity, entity_key, error, payload,
rejected_at)` holds ops a replica refused. `SyncClient.dead_letter_count()` /
`dead_letters(limit)` / `retry_dead_letters()` (requeues at `MAX(device_seq)+1`) plus
`jarvis sync --dead-letters|--retry-dead-letters`, `jarvis doctor`, and the status endpoint. A
refused op is a change that will never arrive; dropping it would be data loss with a green checkmark.

## Server side

```
jarvis/api/app.py
  create_app(settings, db, auth, ...) -> FastAPI      # factory: tests and embedders get isolated instances
  build_default_app() -> FastAPI                      # reads env, opens var/jarvis.db
  class Jarvis                                        # dependency container: db, auth, accounts, voice, media, policy, assistant, sync client
```

`Jarvis.status()` is what `jarvis doctor` and `/api/health` report: provider configuration, which
optional engines are installed, pending ops, dead letters.

Request plumbing worth knowing:

- `session_or_401(request)` → resolves the bearer token, and `uid()` reads it. No cookies exist.
- `privileged_or_403(...)` additionally requires `scope='privileged'` *and* a trusted device.
- `guarded(...)` maps engine `ValueError`s to 400s with the engine's own wording. An engine error
  returning 500 tells a user "the server broke" when the truth is "you asked for something invalid".
- Responses get `SECURITY_HEADERS` (CSP `default-src 'self'`, no `unsafe-inline`, `no-store` on
  `/api/`), 401s get `WWW-Authenticate`, bodies over `max_upload_bytes` get 413 before parsing.

The web client is static files out of the same process — `REPO_ROOT/web`, overridable with
`JARVIS_WEB_DIR` — so there is no second origin, no CORS policy, and no proxy to misconfigure.
`/` and `/web` redirect to `/web/`; `sw.js` and `manifest.webmanifest` are served at the root because
a service worker's scope cannot be deeper than the script's own path.

## Voice

Two separate questions, two separate modules:

**Who is speaking?** `jarvis/voice.py`

```
pcm ─► decode_audio (WAV any rate/width, or raw s16le)
     ─► frames (25 ms / 10 ms hop), energy gate at 35th percentile, dB SNR floor
     ─► 40-band mel → 13 MFCC (+Δ, +ΔΔ) + pitch/voicing block
     ─► 59-dim unit vector
     ─► cosine similarity vs enrolled embeddings → accept / reject + margin
```

The embedding is a *composite* on purpose: mean+std MFCC alone saturates around 0.999 between
different speakers, cohort whitening destroys the separation, spectral-tilt projection breaks
genuine samples, and LPC formant tracking is fragile on harmonic-only audio. Those are measured
dead ends, recorded so the next person does not re-tread them. What survives is bands + cepstrum +
dynamics + pitch.

Enrolment stores *embeddings*, never audio. `verify(..., strict=True)` refuses to grant privileges
on an uncalibrated model: `calibrated` requires ≥10 genuine **and** ≥10 impostor trials, and
`usable_for_privileged` is the gate on `/api/voice/verify` → privileged session. Thresholds are
derived from the owner's own spread rather than a constant, because a fixed threshold's false accept
rate on synthetic probes was measured at 0.15–0.28 — no constant beats a spread-derived bar.

**What was the command?** `jarvis/asr.py`

Open-vocabulary dictation delegates to whatever is installed, in order: `whisper-cli`,
`faster-whisper`, `vosk`, then an HTTP endpoint; if none exist, `Transcript.available=False` with
`engine="unavailable"`. Closed-vocabulary spotting (`CommandSpotter`) is fully local: per-frame
cepstrally-mean-normalised MFCCs compared with a banded DTW, so channel and gain differences cancel
while *content* differences do not.

That shape was forced by measurement. A time-averaged embedding for keyword spotting scored a wrong
phrase at 0.98 against a 0.90 threshold — i.e. useless. Two traps worth not repeating: a DP row that
depends on its own left neighbour cannot be vectorised (numpy silently dropped horizontal steps,
unequal-length takes became `inf`, and `max(6.0, inf)` meant *any* audio matched), and a pure
Sakoe-Chiba band cannot reach the far corner when lengths differ — hence
`band = max(12, 0.3*max(n,m), |n-m|+12)`.

Match requires both distance under threshold **and** a margin over the runner-up. When neither can
be decided, the answer is "no match, here is the reason" — never a guess.

## Media and generation

`MediaService` is a provider interface with three implementations (`local`, `openai`,
`replicate`). The local provider is a real encoder, not a stub: PNG chunks and LZW-compressed GIF
written by hand and verified by *decoding them back* in tests, composition seeded from
`sha256(prompt)` so the same prompt renders the same image on every device (hashing via `hashlib`,
never `hash()` — string hashing is salted per process, which is how a "deterministic" test became a
flake).

Bytes stay on the generating device. A `memory` op tagged `artifact:{kind}` carries metadata, and
other devices fetch the file via `/api/artifacts/{id}/file`. That keeps journal size bounded and
means a 4-million-pixel image never rides through op replication.

`assistant.py` follows the same rule: `LLMClient` is falsy when `llm_base_url` is empty, and any
provider failure degrades to `engine="local-fallback"` with the error in `meta`. A capability that
needs a paid model is always an interface plus a working local implementation, never a fake.

## Device control

Nothing about the assistant's authority is implicit:

```
intent ──► proposes action ──► /api/control/execute ──► Policy ──► LocalExecutor
                                (the only enforcement point)
```

`Policy.evaluate` runs named checks in a fixed order and stops at the first failure, returning the
list of checks it ran, so a denial says *which* gate blocked it (`/api/control/execute` passes that
through to the caller):

1. `capability-known` — present in the catalog at all
2. `arguments-valid` — per-capability schemas, consulted before anything else
3. `risk-not-forbidden` — `power.restart` and `device.factory_reset` cannot be enabled by any rule
4. `kill-switch-off` — the pause flag, read from the synced user flag
5. `has-registered-devices`, `target-specified`, `target-enabled`, `target-capability`,
   `pairing-verified` — the target must exist, be opted in, and expose that capability
6. `user-rule` / `user-risk-ceiling`, else `default-opt-in` — anything above low risk needs an
   explicit allow rule and must sit under that rule's `max_risk`
7. `privileged-session` / `trusted-device` — for risk above low
8. budget (`max_per_hour`) and quiet hours
9. `confirmation` — irreversible actions need a single-use token bound to these exact arguments

`LocalExecutor` then spawns the argv list with `Popen` — no shell exists, so quoting is not a
concern and `; rm -rf /` is just a string that fails argument validation. It tracks the pids it
started, so `app.close` refuses to signal a process Jarvis did not launch. Every outcome, denial
included, lands in `control_audit`.

A medium-or-higher risk action requires a privileged session on a trusted device, and an
irreversible one additionally requires a single-use confirmation token bound to the exact arguments
and valid 300 seconds. That combination is the "proper safety checks" requirement, and it is why
`POST /api/control/pause` insists on an explicit boolean — a UI that can flip `paused` by omission
is a kill switch you cannot trust.

## Data model

21 tables; the load-bearing ones:

| Group | Tables |
| --- | --- |
| identity | `users`, `identities` (provider + subject), `devices`, `sessions` |
| journal | `oplog`, `outbox`, `sync_state`, `sync_dead_letters`, `meta` |
| projections | `traits`, `interests`, `memories`, `interactions`, `feedback`, `controlled_devices`, `control_rules`, `artifacts` |
| control safety | `control_audit`, `confirmations` |
| conversation | `conversations`, `messages` |

`Database.write()` opens `BEGIN IMMEDIATE` and is deliberately **non-reentrant**: calling a writer
inside a `with db.write()` argument list raises rather than deadlocking or silently nesting. It
caught a real bug in `add_task`, and it is the reason helpers resolve what they need *before* opening
a transaction.

## Testing strategy

222 tests, no network, no build step, no fixtures that need a machine-specific install.

The rule used when choosing what to cover: **test the invariants that would otherwise be invisible**.
Not "does `enroll` return 200" but "does an uncalibrated model refuse a privileged grant even for
the owner" (yes, and that is the security property). Not "does sync move ops" but "does a replayed
`op_id` count as accepted without double-applying".

Two guards earn their keep:

- a **route drift guard** that extracts every `/api/...` literal from `web/app.js` and fails if it
  is absent from the OpenAPI document — the classic way a client silently rots;
- a **cross-user journaling guard** at the HTTP layer, since a sync endpoint that trusts a device id
  is a data-leak primitive.

The voiceprint and command-spotter tests assert the *measured* behaviour, including the failures
(impostor near-clones from the synthetic generator). A test that promises "3 takes accept anything"
would just be a lie that passes.

## Extension points

| To add… | Do this |
| --- | --- |
| a new synced feature | new `entity` in `PROJECTED_ENTITIES` + a `materialize` branch + projection table; write via `append_op` |
| an LLM/media provider | subclass alongside `Local*Provider` in `media.py`/`assistant.py`; keep the local fallback and the `fallback_used` flag |
| a new control capability | one `CATALOG` entry (name, risk tier, argument schema, `forbidden` flag); `Policy` and the audit trail come free |
| a new transport | `push(device_id, payload) -> dict`, `pull(device_id, params) -> dict`; raise `OfflineError` when unreachable |
| a heavy ML dependency | put it in a `pyproject.toml` extra and detect it in `jarvis.capabilities()`, never import it at module scope |

## What this design is not

Not multi-tenant-safe as shipped: one server, one trust domain, one user (sessions and journals are
per-user, but the ASR probe cache, temp-file model cache, and `LocalExecutor` are process-wide). Not
a zero-knowledge sync system: the replica sees plaintext ops, so a hosted replica is a party you are
trusting. Not post-quantum, not FIPS-validated, and not an authentication system that should gate
money or medical decisions — which is why privileged voiceprint grants are step-up signals inside a
session that an OIDC provider already vouched for.
