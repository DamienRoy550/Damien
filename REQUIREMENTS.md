# Requirements

The project brief was nine capabilities. This document traces each one from the requirement to the
code that satisfies it, the tests that hold it in place, and — where the honest answer is "partly"
— the deviation and why. Nothing here is aspirational: if a row says implemented, calling it works.

**Status legend** — *Implemented*: real code path, tested. *Implemented with fallback*: works
locally by default, upgrades when you configure a provider. *Deferred*: deliberately not built,
with the reason.

---

## 1. Adaptive learning

> Learns preferences, communication style and interests over time; responses and recommendations
> become more personal with use.

| | |
| --- | --- |
| Status | Implemented |
| Model | `jarvis/adaptive.py` — seven bounded traits (`formality`, `verbosity`, `warmth`, `caution`, `humor`, `technicality`, `curiosity`), an interest vector, and a half-life decay of 45 days by default |
| Write path | `observe_turn()` on every assistant reply, `record_feedback()` on 👍/👎 and on explicit directives ("be much shorter and less formal" → `STYLE_COMMANDS` maps to trait deltas), `reinforce()`/`set_target()` from `/api/preferences/trait` |
| Read path | `style_directive()` is injected into generation and `apply_style()` rewrites the surface of the answer (sentence length, hedges, contractions, openings); `recommend()` ranks memories/interests by the same vector |
| API | `GET /api/preferences`, `POST /api/preferences/trait`, `POST /api/assistant/feedback`, `GET /api/recommendations` |
| Persistence | the `traits` / `interests` projections, which are themselves journal entries — so a style change syncs to your other devices like any other memory |
| Tests | `tests/test_adaptive.py` (13) — movement, bounding, decay, directive vocabulary, that a matcher never claims a transformation it did not perform |
| Measured | in `jarvis demo`: verbosity and formality both shift by ≈ −0.79 after one directive, and the next reply is visibly shorter |

The traits are deliberately inspectable and reversible. A user who cannot see why the assistant got
blunter has no way to fix it.

## 2. Offline functionality

> Works without internet — voice recognition, basic commands, stored preferences; cache locally and sync when reconnected.

| | |
| --- | --- |
| Status | Implemented |
| Principle | SQLite is the source of truth; the network is an optional replication link, never a dependency for a read or a write |
| Offline-capable now | speaker verification (`voice.py`, numpy only), closed-vocabulary command spotting (`asr.py`), recall/memory, brainstorm/draft/plan/summarise via the local generator, device control policy and execution, image and video rendering (local providers), the whole web shell (service worker `jarvis-shell-v3`) |
| Sync when reconnected | every mutation is queued in `outbox`; `SyncClient.sync()` drains it and pulls, with a cursor that resumes exactly where it stopped |
| Honesty rule | open-vocabulary dictation is *not* claimed offline. With no ASR model installed, `Transcript.available=False, engine="unavailable"` and the UI says so instead of guessing your words |
| Detection | `net.py` connectivity probe; `JARVIS_FORCE_OFFLINE=1` for tests and demos |
| API | `GET /api/sync/status` (pending, dead letters, last error), `POST /api/sync/run`, `GET /api/sync/pull`, `POST /api/sync/push` |
| Tests | `tests/test_sync.py` (13) — queue-while-offline then drain only after confirmation, two devices on the same register converging, tombstone beats older live write, cursor exactness, order independence, dead-lettering; `tests/test_asr.py`, `tests/test_voice.py` run fully offline |
| Measured | `jarvis demo` stage 4: laptop pushes 38 ops, phone pushes 1 and pulls 38, both read the same value, 1 conflict resolved |

Two things were treated as non-negotiable here: a queued op is deleted only after the replica
acknowledges it, and an op the replica *rejects* is dead-lettered rather than dropped — a drained
queue that silently lost your change is worse than a stuck one.

## 3. Cross-device access

> Log in from anywhere with Gmail, Apple ID, or Microsoft; continuity of state across devices.

| | |
| --- | --- |
| Status | Implemented with fallback |
| Protocol | OIDC authorization-code + PKCE (S256, single-use `state`, `nonce` check), per-provider discovery documents in `auth.PROVIDERS`; tokens are validated with an algorithm allowlist *before* signature checks |
| Zero-config path | a local development IdP (`/api/auth/dev-login`) gated on `JARVIS_ALLOW_PASSWORDLESS_DEV_IDP`, so the full session/device/sync flow is exercisable without registering three OAuth apps |
| Continuity | comes from the journal, not the browser: sign in on a new device, sync, and you have the same preferences, memories, tasks, voiceprint enrolment and control policy |
| Sessions | `basic` vs `privileged` scope; random 32-byte tokens, only their HMAC keyed hash is stored; refresh rotates, `logout_all` revokes; rate-limited per minute |
| API | `POST /api/auth/login/begin`, `GET /api/auth/callback`, `POST /api/auth/logout`, `POST /api/auth/logout-all`, `GET /api/me`, `POST /api/me/name` |
| Tests | `tests/test_auth.py` (19) — PKCE verifier/state handling, replay of a used code, `alg: none` and RS256-substitution rejection, expiry, refresh, revocation, rate limiting; `tests/test_api.py` — the same paths over HTTP, including that an unauthenticated request to a user endpoint is a 401 with `WWW-Authenticate` |
| Deferred | automatic account *linking* across providers (same email on Google and Microsoft stays two users until you link deliberately). Deliberate: auto-linking on email alone is an account-takeover vector, not a convenience. |

## 4. Device sync via Gmail

> Jarvis recognises and syncs devices signed in with Gmail, for seamless transitions.

| | |
| --- | --- |
| Status | Implemented |
| Recognition | a device row carries its provider and provider subject; two devices with the same Gmail subject under the same user are recognised as yours and surfaced in `GET /api/devices` with trust level and last-seen |
| Trust | trust-on-first-use *with confirmation*: `POST /api/devices/pair` issues a short code that must be entered on the receiving device, then `POST /api/devices/{id}/trust`. Nothing is trusted silently, and `revoke` is instant |
| Sync payload | the journal — preferences, memories, tasks, voiceprint enrolment (embeddings, not audio), enrolled voice commands, control targets and rules |
| Conflict rule | last-writer-wins by `(wall_ts, lamport)`, tombstones beat older live writes, `op_id` breaks ties; replay is idempotent by `op_id` |
| API | `GET /api/sync/status`, `POST /api/sync/run`, `GET /api/devices`, `POST /api/devices/*` |
| CLI | `jarvis sync`, `jarvis devices` |
| Tests | `tests/test_sync.py` — different registers both survive, same-register order independence, replayed op ids counted as accepted without double-applying; `tests/test_api.py` — cross-user journaling is refused (a device cannot write into another user's journal), push/pull idempotency through the public endpoints |
| Deferred | a public relay server. Two devices must share a replica URL (or the same account's server); a hosted sync relay is a deployment decision with its own trust story. |

## 5. Comprehensive assistance

> Brainstorming, content creation, practical advice, everyday tasks, friendly and dynamic.

| | |
| --- | --- |
| Status | Implemented |
| Routing | `INTENTS` + `classify()` — brainstorm, write, advise, plan, summarise, remember, recall, task, control, media, smalltalk — with stemming so inflected verbs still match |
| Generation | `gen_brainstorm` (divergent, deduped), `gen_write` (draft + alternates), `gen_advice` (steps + cautions), `gen_plan` (sequenced with time budget), `gen_summary`; every reply carries `intent`, `engine`, `cards`, `actions`, `follow_ups`, `meta` |
| Everyday tasks | tasks are `todo`-tagged memories: `add_task`, `tasks`, `complete_task`, and recall is TF-IDF-ish over the same store (`memory.py`), proven to survive a restart |
| Friendliness | the reply is rewritten by `apply_style()` using the adaptive traits — so "dynamic" is the same mechanism as requirement 1, not a separate tone string |
| Providers | any OpenAI-compatible `llm_base_url`; when unset or on failure, `engine="local-fallback"` and the answer is still useful |
| API | `POST /api/assistant`, `GET/POST /api/memory`, `GET /api/memory/search`, `DELETE /api/memory/{id}`, `GET/POST /api/tasks`, `POST /api/tasks/{id}/complete`, `GET /api/commands` |
| Tests | `tests/test_assistant.py` (33) — intent classification, each generator, style edits reported truthfully, task lifecycle, memory recall, that a control intent only ever *proposes* an action; `tests/test_api.py` for the HTTP shapes |

## 6. Device control

> Open/close/manage user-specified devices and apps, with proper safety checks.

| | |
| --- | --- |
| Status | Implemented |
| Surface | 12 capabilities in `CATALOG`, each with a risk tier; `files.delete`, `power.shutdown` high and irreversible; `power.restart` and `device.factory_reset` **forbidden** and not enableable by any rule |
| Policy | `Policy.register` records whether a target actually supports a capability (`unsupported` + `note` rather than a silent no-op); per-capability allow/deny rules; `max_risk` validation; per-device enable flags; budgets and quiet hours; a synced kill switch; single-use, argument-bound confirmations valid 300 s for irreversible actions |
| Execution | `LocalExecutor` — argv only, never a shell; `Popen` for lifecycle, tracked so `app.close` can only signal a process Jarvis started; `execute` returns one of `executed`, `failed`, `denied`, `needs_confirmation`, `dry_run` |
| Injection | arguments are validated against per-capability formats; `; rm -rf /`-shaped input is denied with the reason |
| Audit | one `control_audit` row per decision, denial included |
| API | `GET /api/control/catalog`, `POST /api/control/targets`, `DELETE /api/control/targets/{id}`, `POST /api/control/rules`, `POST /api/control/execute`, `POST /api/control/pause`, `GET /api/control/audit` |
| Tests | `tests/test_devctl.py` (26) — allowlists, risk tiers, confirmation single-use and argument binding, budget/quiet-hours, kill switch, argv-only execution, denial audit rows |
| Boundary | the assistant may *propose* an action; the only enforcement point is `/api/control/execute`. Prompt-injected text cannot bypass a policy it never reaches. |

## 7. Image & video generation

> Generate images and videos on demand for creative and multimedia work.

| | |
| --- | --- |
| Status | Implemented with fallback |
| Interface | `MediaService(db, settings)` with `generate/list/read_blob`; providers `local`, `openai`, `replicate` selected by config |
| Local provider | deterministic encoder written for this repo: `encode_png`/`decode_png` (real PNG chunks, round-trip identical) and `encode_gif` (LZW, verified by decoding it back); composition seeded from `sha256(prompt)`, so the same prompt renders the same picture on every device |
| Guardrails | `MAX_PIXELS = 4e6`, bounded frame count and dimensions, remote failure → local render with `fallback_used: true` |
| Data residency | bytes stay on the generating device; only metadata syncs, as a `memory` row tagged `artifact:{kind}`; other devices fetch `GET /api/artifacts/{id}/file` |
| API | `POST /api/media/image`, `POST /api/media/video`, `GET /api/artifacts`, `GET /api/artifacts/{id}/file` |
| Tests | `tests/test_media.py` (14) — PNG/GIF structural validity, prompt determinism, size caps, fallback path, artifact listing and cross-device fetch |
| Measured | `jarvis demo` stage 7 with no network: image 64 KB in ~50 ms, 9-frame GIF 147 KB in ~2.6 s |
| Deferred | photorealistic output. That is a diffusion model, which is a paid API or a multi-gigabyte download — so the provider interface is real and the local implementation is honest about what it is. |

## 8. Web-based access

> Secure access through a web browser, in addition to dedicated apps.

| | |
| --- | --- |
| Status | Implemented |
| Client | `web/` — seven views (assistant, memory, preferences, control, media, devices, settings), vanilla JS, no build step, installable (`manifest.webmanifest`) and offline-shelled (`sw.js`) |
| Transport | `Authorization: Bearer` from `localStorage`; no cookies exist, so there is no CSRF surface; `/api/*` is `no-store` and never cached by the service worker |
| Hardening | CSP `default-src 'self'` with no `unsafe-inline` anywhere (the HTML has no inline `style=` or `<script>`), `X-Content-Type-Options: nosniff`, `frame-ancestors 'none'`, `base-uri 'none'`, `form-action 'self'`, oversized bodies rejected with 413, 401s carry `WWW-Authenticate` |
| Voice from the browser | `getUserMedia` → downsampled to 16 kHz mono s16le WAV → base64 in the JSON body; no audio is ever persisted unless you enrol |
| Server | `create_app(...)` is a factory, so tests and embedders build isolated instances; `jarvis serve` binds 127.0.0.1:8000 by default |
| API | 52 paths / 54 operations; the OpenAPI document is authoritative and is used as a test oracle |
| Tests | `tests/test_api.py` (61) — headers, 413, auth, every capability route, and a drift guard that fails the suite if `web/app.js` calls an `/api/...` path missing from OpenAPI |
| Deferred | native iOS/Android apps. The PWA shell plus the same HTTP API is the substitute; a real app is a packaging job on top of an engine that is already device-agnostic. |

## 9. Voice recognition (biometric, only your voice)

> Recognises the user's specific voice for secure, personalised access.

| | |
| --- | --- |
| Status | Implemented, with the limits stated in the open |
| Front end | WAV decode at any rate/width/channels (or raw s16le) → 40-band mel spectrum → 13 MFCC + deltas + a pitch/voicing block → **59-dimensional unit embedding**; energy gating in dB with an SNR floor |
| Model | `LocalSpeakerModel` (numpy, always available) or `PretrainedSpeakerModel` (an embedded speaker-embedding model, cached under the system temp dir) — same interface |
| Verification | `VoiceprintModel.verify(pcm, strict=...)` returns similarity, decision, margin and `blocked_reason` ∈ {`uncalibrated`, `thin-enrolment`, `embedding-dim-mismatch`}; threshold is derived from your own intra-speaker spread, sample-count adaptive (n=1 → 0.90; otherwise `min(pairwise) − (0.06 + 0.09/n)` bounded to [0.55, 0.93/0.95]) |
| Privileges | `verify_voice_step_up` grants a `privileged` session only when the model is `calibrated` and `usable_for_privileged` (≥10 genuine **and** ≥10 impostor trials). Otherwise the privileged grant is refused even for the rightful owner. |
| Command spotting | `CommandSpotter` — per-frame, cepstrally-mean-normalised MFCCs with banded DTW, multi-take enrolment, per-phrase calibrated threshold (`suggest_threshold`, `apply_calibrated_threshold`, `/api/commands/auto-calibrate`). Match requires both distance and a margin over the runner-up. |
| API | `POST /api/voice/enroll`, `POST /api/voice/verify`, `/verify-simulated`, `/calibrate`, `/status`, `/reset`, `/synth`, `GET/POST /api/commands*`, `DELETE /api/commands/{phrase}` |
| Consent | every enrolled phrase lists its take count in `GET /api/commands` and can be re-recorded (`replace: true` restarts one phrase without disturbing the others) or deleted outright, from the API and from the web UI. Enrolments you cannot undo are a liability |
| Tests | `tests/test_voice.py` (19) and `tests/test_asr.py` (24) — enrolment quality, threshold derivation, calibration gating, dimension mismatch, that noise and wrong phrases are rejected, that export/load preserves every take so calibration survives a restart |
| Measured | n=3 enrolment: threshold 0.881, unseen sentence 0.888 accepted, 6 of 7 synthetic impostors refused. Command battery: exact and reworded forms match (6.7 / 8.1), unrelated phrases and four impostor voices are refused (11.6–26.5). |

Two claims this repository will not make: that a 3-take enrolment accepts arbitrary new content
(it does not, and the tests say so), and that a voiceprint is a strong per-impostor guarantee.
It is a convenience factor and a step-up signal — which is exactly how the code uses it.

---

## Non-functional requirements

| Requirement | How it is met | Evidence |
| --- | --- | --- |
| No build step for the client | static files served by the same process | `jarvis/api/app.py::_web_dir` |
| Core dependencies stay small | stdlib + numpy + FastAPI; heavy optional ML in extras (`local-asr`, `voice-model`, `providers`) | `pyproject.toml` |
| Determinism in tests | no `hash()` seeds anywhere (all seeding via `sha256`), suite re-run green under varied `PYTHONHASHSEED` | 3 runs at different seeds |
| Every capability demonstrable in one command | `python -m jarvis.cli demo` | prints 7 stages |
| Lint as a correctness tool | `ruff` with `E,F,W,I,B,UP,S,RUF` at line-length 130, repo-wide clean | `ruff check .` |
| Tests that fail when docs drift | route drift guard against OpenAPI | `tests/test_api.py` |

## Deliberately out of scope

- Real paid diffusion models, cloud ASR, and push-notification transport are wired as provider
  interfaces with working local implementations. Wiring a vendor key changes config, not code.
- A hosted multi-tenant deployment (this is single-user-per-server by design; see SECURITY.md for
  what would have to change).
- Auto-linking identities across providers, and any control capability marked forbidden.
