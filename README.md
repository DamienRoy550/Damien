# Jarvis

A personal assistant that gets more useful the more you use it — on purpose, not by accident.
It runs a local server on your machine, keeps every piece of state in a local journal so it works
with the network off, recognises *your* voice specifically (not just "a voice"), syncs your
preferences and memory between your devices, and can drive real apps and files behind an explicit
permission policy.

This repository is the engine, the server, the web client, the CLI, and the test suite. It is a
working implementation, not a mockup: `jarvis demo` exercises all nine core capabilities end to
end in a few seconds and prints what actually happened, including where a capability fell back to
a local substitute because it needs a paid model.

```
pip install -e ".[dev]"
python -m jarvis.cli demo      # 7 stages, no network needed, nothing installed on your machine
python -m jarvis.cli serve     # then open http://localhost:8000/web/
```

---

## The nine core capabilities

| Capability | What it does here | Where | Honest limit |
| --- | --- | --- | --- |
| **Adaptive learning** | Per-user trait vector (formality, verbosity, warmth, caution, humor, technicality, curiosity) that moves when you give feedback and decays on a half-life; interest profile and response-style rewriting derived from it | `jarvis/adaptive.py` | Traits are a small interpretable vector, not a fine-tune. It steers wording and selection; it does not rewrite the model. |
| **Offline functionality** | SQLite is the source of truth. Voice verification, command spotting, recall, brainstorming, drafting, planning, control policy, and media generation all run with no network. Writes go to an outbox and sync later | `jarvis/db.py`, `jarvis/sync.py`, `jarvis/net.py` | Open-vocabulary dictation needs an ASR model; if none is installed the transcript comes back `unavailable` instead of a guess. |
| **Cross-device access** | Sign in with Google, Apple, or Microsoft (OIDC authorization-code + PKCE), or a local dev IdP. State continuity comes from the synced journal, not from the browser | `jarvis/auth.py`, `jarvis/accounts.py` | Real client IDs are yours to configure; without them the provider buttons show the configured flow rather than pretending. |
| **Device sync via Gmail** | Devices that sign in with the same Gmail subject are recognised and offered to each other; pairing needs a one-time code, then trust is explicit and revocable | `jarvis/auth.py` (`request_pairing`, `trust_device`), `/api/devices/*` | Never silent trust. Same-provider does not mean same-person. |
| **Comprehensive assistance** | Brainstorm, draft, rewrite, advise, plan, summarise, remember, recall, tasks — with intent routing, cards, suggested actions and follow-ups | `jarvis/assistant.py`, `jarvis/memory.py` | Uses your `llm_base_url` when set; otherwise a deterministic local generator. LLM failure degrades to `engine="local-fallback"` rather than erroring. |
| **Device control** | Only user-registered targets, argv-only execution, risk tiers, dry-run, budgets, quiet hours, per-decision audit including denials, a kill switch, and single-use confirmations for irreversible actions | `jarvis/devctl.py` | `power.restart` and `device.factory_reset` are forbidden by policy and cannot be enabled. Apps must be allowlisted via `JARVIS_ALLOWED_APPS`. |
| **Image & video generation** | `POST /api/media/image` and `/video`; local deterministic renderer plus OpenAI and Replicate providers | `jarvis/media.py` | The local renderer draws, it does not diffuse. Provider failures fall back to local with `fallback_used: true`. |
| **Web-based access** | Single-page client with no build step, served by the same process: strict CSP, `no-store`, no cookies anywhere, Bearer tokens in `localStorage`, offline shell cached by a service worker | `jarvis/api/app.py`, `web/` | `/api/*` is never cached. `?token=` exists only so `<img>` can fetch bytes. |
| **Voice recognition** | 59-dimensional speaker embedding (mel bands, MFCC, deltas, pitch, voicing) with enrolled voiceprints, per-user threshold, and a calibration report; gated step-up for privileged actions | `jarvis/voice.py` | A voiceprint is a strong convenience factor, not a per-impostor guarantee. Numbers in this repo come from synthetic probes. |

`python -c "import jarvis; print(jarvis.describe())"` prints that matrix with live capability
detection (which ASR engines are installed, whether media providers are configured, and so on).

## How state works

Every mutable change is an append-only operation in one journal:

```python
db.append_op(device_id=..., user_id=..., entity="trait", entity_key="humor",
             kind="set", payload={...})   # journal + projection, one transaction
```

The domain tables (`traits`, `interests`, `memories`, `controlled_devices`, `control_rules`) are
**projections** of that journal — they can be deleted and rebuilt with `rebuild_projections`. That
is what makes sync a solved problem: a device pushes its journal, applies a peer's, and resolves
conflicts last-writer-wins by `(wall_ts, lamport)` with tombstones beating older writes. Both
devices converge to byte-identical state.

An op that the replica *refuses* is dead-lettered into `sync_dead_letters` before its outbox row is
deleted, and requeueable with `jarvis sync --retry-dead-letters`. Refusals never vanish quietly.

## Configuration

All settings are environment variables prefixed `JARVIS_` (or a `.env` file), defined in
`jarvis/config.py`. The ones you will actually set:

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_DATA_DIR` | `./var` | SQLite file, blobs, and `secret.key` live here |
| `JARVIS_SECRET_KEY` | generated into `data_dir/secret.key` (0600) | HMAC key for session token hashes |
| `JARVIS_DEVICE_ID` / `JARVIS_DEVICE_NAME` | `local-device` / `This machine` | identity used by the journal |
| `JARVIS_GOOGLE_CLIENT_ID` / `_SECRET`, `JARVIS_APPLE_*`, `JARVIS_MICROSOFT_*` | empty | real OIDC providers |
| `JARVIS_REDIRECT_BASE_URL` | `http://localhost:8000` | OIDC callback base |
| `JARVIS_ALLOW_PASSWORDLESS_DEV_IDP` | `true` | local dev IdP; **set to `false` anywhere real** |
| `JARVIS_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | empty | any OpenAI-compatible endpoint; empty means local-only |
| `JARVIS_IMAGE_PROVIDER` / `JARVIS_VIDEO_PROVIDER` | `local` | `local` \| `openai` \| `replicate` |
| `JARVIS_ASR_PROVIDER` / `JARVIS_ASR_ENDPOINT` | `offline` | whisper-cli → faster-whisper → vosk → HTTP, in that order |
| `JARVIS_ALLOWED_APPS` | empty | `name=/absolute/path`, the only apps `app.open` may launch |
| `JARVIS_ENFORCE_VOICEPRINT_FOR_PRIVILEGED` | `true` | turn off to allow privileged sessions without step-up |
| `JARVIS_FORCE_OFFLINE` | unset | force the connectivity probe to report offline |
| `JARVIS_ENABLE_DEMO_RESET` | unset | required for `POST /api/demo/reset` |

## CLI

```
jarvis init                                            create the local account and device
jarvis doctor                                          what works offline, what is configured, what is refused
jarvis say "be much shorter and less formal"            ask the assistant (intent, engine, cards, actions)
jarvis learn up|down [--note "..."]                     rate the last answer, watch the traits move
jarvis prefs [--set verbosity=-0.6]                     inspect or override a learned trait
jarvis remember <text>      jarvis recall <query> [--limit N]
jarvis sync [--status | --dead-letters | --retry-dead-letters] [--device ID]
jarvis devices [--trust DEVICE_ID]                      trust levels and last-seen, per device
jarvis control --list
jarvis control --allow app.open --max-risk medium
jarvis control --exec app.open --args '{"app":"editor"}' [--dry-run] [--confirm TOKEN]
jarvis control --pause true | --audit 20
jarvis image "prompt" [--kind video] [--out file] [--offline]
jarvis voice                     status;  --enrol N  |  --verify PHRASE [--f0 118]
jarvis demo                                            7 stages, end to end, in a temp dir
jarvis serve [--host H] [--port P]                     uvicorn, defaults to 127.0.0.1:8000
```

## Layout

```
jarvis/
  config.py db.py sync.py            journal, projections, offline queue, conflict resolution
  voice.py asr.py                    speaker verification, closed-vocabulary command spotting
  adaptive.py memory.py              traits, interests, recall
  assistant.py                       intent routing, content generation, replies
  auth.py accounts.py net.py         OIDC + PKCE, sessions, devices, pairing, connectivity
  devctl.py media.py                 device policy + executor, image/video providers
  api/app.py                         FastAPI: 52 paths / 54 operations, static web client, hardening headers
  cli.py                             the command line
web/                                 index.html, app.js, styles.css, sw.js (no build step)
tests/                               222 tests, 9 files, no network required
```

## Tests

```
python -m pytest            # 222 tests
ruff check .                # clean, including bandit and bugbear rules
```

`jarvis doctor` also states, in plain words, what the biometric gate will and will not unlock today.

The suite covers the parts that are easy to get subtly wrong: journal replay and idempotency,
conflict ordering across two devices, voiceprint calibration gating, command-spotter thresholds
against impostor speech, policy denials (including injection-shaped arguments), provider fallback,
every auth path, and the HTTP layer — including a guard that every `/api/...` string in the web
client still exists in the OpenAPI document.

## Security

Read [SECURITY.md](SECURITY.md). The short version: Bearer-only auth with tokens stored as HMAC
hashes, privileged actions require a verified identity session *and* a calibrated voiceprint
step-up on a trusted device, the kill switch and per-capability allow rules are the only way to run
a destructive command, and every decision — allowed or denied — is audited. [ARCHITECTURE.md](ARCHITECTURE.md)
explains why the design is shaped this way; [REQUIREMENTS.md](REQUIREMENTS.md) traces each of the
nine capabilities to code and tests.
