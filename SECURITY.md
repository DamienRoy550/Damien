# Security

Written for the person who is about to run this on a machine that holds their notes, their files,
and a microphone. Read the **Threat model** and **What this does not protect against** sections even
if you skip the rest — a security property you misunderstand is worse than no property at all.

---

## Threat model

**In scope**

| Threat | Posture |
| --- | --- |
| Network attacker hitting the API | Everything except `/api/health` and the login routes requires a bearer session; oversized bodies are rejected with 413 before parsing; auth and voice-verification attempts are rate limited; responses carry `no-store` and a strict CSP |
| Browser-based attacks on the web client | No cookies at all → no CSRF surface; CSP `default-src 'self'` with no `unsafe-inline` and `frame-ancestors 'none'` → injected markup cannot execute or be framed; `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Cross-Origin-Opener/Resource-Policy: same-origin`, `Referrer-Policy: no-referrer`, `Permissions-Policy` narrowing the mic to `self` and disabling camera/geolocation; `base-uri 'none'`, `form-action 'self'` |
| Someone speaking into your device | Speaker verification must match your enrolled voiceprint, and a privileged grant additionally requires a *calibrated* model |
| Stolen device database | Access and refresh tokens are stored only as HMAC-SHA256 digests keyed by `secret.key`; a copied DB cannot be replayed against the API. The key is mode `0600` next to the database — losing the machine means losing the data, so full-disk encryption is assumed |
| Compromised/malicious model output, or text the assistant read, trying to drive your machine | The assistant can only *propose* a control action; policy is evaluated at `/api/control/execute`. Text cannot reach `Popen` without passing an allow rule, a risk tier, argument validation, and (for irreversible actions) a human confirmation |
| Sync peer replaying or forging writes | Ops are attributed to the authenticated device, `user_id`/`device_id` are overwritten server-side from the session, replay is idempotent by `op_id`, and a device may not journal into another user's account |
| Someone guessing at the biometric gate | Voice verification attempts have their own budget (cost 4.0 against a 30-token bucket ≈ 7 attempts, then ~1 per 4 s) with `Retry-After: 10`, and every refusal is written to `control_audit` |

**Out of scope / not claimed**

- A remote attacker who can already run code as your user. `LocalExecutor` is a convenience, not a
  sandbox; there is no seccomp/namespace isolation.
- Multi-tenant deployment: one server, one trust domain (see the last section).
- Coerced disclosure, evil-maid, network-level metadata exposure.

---

## Authentication

**Providers.** OIDC authorization-code flow with **PKCE S256** for Google, Apple and Microsoft
(`auth.PROVIDERS` + per-provider discovery). `state` is single-use and consumed by `take(state)`
before the IdP round trip completes, so a callback cannot be replayed; `nonce` is checked against
the ID token.

**ID token validation order is deliberate**: the `alg` header is checked against an allowlist
*before* any key resolution, and HMAC-vs-RSA confusion is refused. Signature verification happens
via PyJWT's JWKS client with the provider's own `jwks_uri`; `aud` must be the configured client id.
Everything an IdP can get wrong surfaces as `AuthError` → HTTP 400 with the reason, never a 500.

**Sessions.** 32 random bytes for access, 32 for refresh, both `secrets.token_bytes`. Only
`hmac_sha256(secret_key, token)` is stored, indexed in `meta` for O(1) lookup. `scope` is either
`basic` or `privileged`; refresh rotates; `logout` revokes one session, `logout-all` revokes all of
them, and a revoked session stops resolving immediately (revocation is checked on every request, not
only at issue time). Default TTLs: 8 h access, 30 d refresh.

**Transport of the token.** `Authorization: Bearer` from `localStorage`, and nothing else. Two
deliberate consequences:

1. `?token=` is accepted **only** because `<img>` cannot send a header, so artifact bytes are
   fetched with a query token. That token can land in a proxy access log or browser history. Keep
   the log you do not want it in off, or open artifacts from a session page instead.
2. `localStorage` is readable by any script that runs on the origin. That is why the CSP forbids
   inline script, why the HTML served by the API contains no inline handlers, and why there is no
   third-party anything on the page. If you inject an analytics tag into `web/index.html`, you have
   handed out the bearer token — that is your call to make, not a bug here.

**The dev IdP is a real door.** `allow_passwordless_dev_idp` (default `true`) exists so the full
flow is testable without registering three OAuth clients. It mints a locally signed token from an
email address with no password. **Set `JARVIS_ALLOW_PASSWORDLESS_DEV_IDP=false` on any machine
someone else can reach.** `jarvis doctor` prints a warning while it is on.

**Pairing and device trust.** `POST /api/devices/pair` issues a short code that must be entered on
the receiving device; `trust` only works after that. Revocation is instant and affects in-flight
sessions' ability to do risk-tiered work. Trust-on-first-use without confirmation is not offered,
even for two devices signed in with the same Gmail account — same provider is not the same person.

---

## Biometric access control

This is the part most projects oversell, so it is stated narrowly.

**What is implemented.** A 59-dimensional embedding (40 mel bands, 13 MFCC plus delta and
delta-delta, pitch/voicing), unit-normalised; cosine similarity against enrolled embeddings; a
threshold derived from the *owner's* own intra-speaker spread, adaptive to sample count:

```
n == 1                 → 0.90
otherwise              → clamp( min(pairwise) − (0.06 + 0.09/n), 0.55, 0.93 | 0.95 for n ≥ 5 )
```

Enrolment stores embeddings, never audio. An enrolment carries the `model_version` that produced it;
after the extractor changes, the stored vectors no longer match the live embedding dimensions and
`verify` refuses with `embedding-dim-mismatch` rather than scoring garbage — re-enrolment is
required, by design.

**The gate.** `POST /api/voice/verify` grants a privileged session only if
`verify(strict=True)` accepts **and** the model is `calibrated` with `usable_for_privileged`, which
requires at least 10 genuine **and** 10 impostor trials (`/api/voice/calibrate`). An uncalibrated
profile refuses the privilege *for the rightful owner* — refusal is the default, and
`blocked_reason` tells you which of `uncalibrated`, `thin-enrolment`, `embedding-dim-mismatch`
applied. Privileged scope is what unlocks medium/high-risk device control.

**Closed-vocabulary commands (`/api/commands/*`) are convenience only.** A DTW match on
cepstrally-normalised MFCCs says "this sounds like the recording you gave me for *that phrase*". It
is not speaker verification and it is not an authorisation signal: it is how you can say "arm the
system" without a model download, and nothing more may be granted on it. Enrolled phrases are also
not a secret — anyone who can see the command list knows them; only the audio shape is private, and
audio shape is the weakest possible secret.

Enrolled phrases and their take counts are listed by `GET /api/commands`, can be re-recorded
(`{"replace": true}` restarts one phrase without touching the others), and can be deleted with
`DELETE /api/commands/{phrase}` — from the web client too. If you cannot delete a recording, you did
not consent to it, you merely misplaced it.

**What a voiceprint here does not do.**

- It does not reliably distinguish you from a determined impostor, and it certainly does not survive
  replay of a recording you have already spoken aloud (there is no liveness challenge).
- It is not tied to a live human. Playback over a speaker has been measured to pass similar
  features; assume it can work.
- It does not support "any sentence in any accent" from a short enrolment. With 3 samples the
  measured threshold was 0.881 and one of seven synthetic near-clone impostors scored above it.
  The near-clone differed by 8 Hz of fundamental frequency; the failure is a property of the
  *synthetic generator* as much as of the model, and it is reported rather than hidden.
- It is not FIDO/WebAuthn. Where a hardware key or biometric platform API is available, prefer it.

Every number in this repository was measured with **synthesised probe speech**, not a corpus of real
people, so do not read any of them as a FAR/FRR estimate for your voice.

**Recommendation.** Treat privileged scope as "you proved it twice": IdP session + voiceprint. For
anything you would cry about (delete-heavy file operations, anything irreversible), Jarvis already
requires the single-use confirmation regardless — the biometric never authorises it alone.

---

## Device control

The largest attack surface in the project, because it runs real programs.

1. **No shell, ever.** `LocalExecutor` builds an argv list and `Popen`s it. Argument quoting bugs,
   `;`, `&&`, backticks, and newlines are not special characters here; they are just characters that
   fail format validation with a reason naming the argument.
2. **Allowlisted apps only.** `app.open` can launch anything in `JARVIS_ALLOWED_APPS`
   (`name=/absolute/path`) and nothing else — not by name matching, by dictionary lookup. There is no
   path-resolution fallback, so `../` games have nothing to win.
3. **Per-capability allow rules.** Each capability needs its own rule. Being allowed to start a
   process is not permission to kill one, and `app.close` refuses any pid it did not spawn.
4. **Risk tiers with hard floors.** `power.restart` and `device.factory_reset` are marked
   `forbidden` in the catalog and cannot be enabled by any rule, confirmation or session scope.
   `files.delete` and `power.shutdown` are high-risk **and** irreversible, hence always
   confirmation-gated.
5. **Confirmation is single-use, argument-bound and 300 s.** A token is minted for the exact
   arguments; changing the target or the path invalidates it, so a token approved for one thing
   cannot be spent on another.
6. **Budgets and quiet hours** bound how much can happen in a window and when; a burst of otherwise
   legal actions is itself a signal.
7. **Kill switch.** `POST /api/control/pause` with an explicit boolean (omitting it is a 422 — a
   route that pauses by accident is a route that does not stay paused). State is journalled, so the
   pause propagates to your other devices.
8. **Everything is audited**, including denials, including rate-limit refusals. The audit log is
   device-local on purpose: an attacker who wants the record of what they did should not be able to
   sync it away, and a projection that replicates can also be rewritten.

`GET /api/control/audit` is the first thing to read after any surprise.

---

## Data, storage, and the sync replica

- **SQL.** Every query in the codebase is parameterised. The two places where SQL is built as a
  string interpolate only literal table names from a fixed internal tuple or `?` placeholders, and
  both carry an inline justification. If you add string-built SQL, that is the standard.
- **Paths.** `GET /api/artifacts/{id}/file` rejects an id that contains `.` or does not start with
  `art_`, then reads bytes for that id and refuses unless the row's `user_id` matches the session — so
  there is no caller-controlled path to escape from in the first place. `var/` is git-ignored, and
  blobs are never committed.
- **Journal contents.** Ops are plaintext on the replica. Sync metadata (memory ids, trait values,
  enrolment embeddings) is not end-to-end encrypted. **The sync replica is a party you trust** — a
  third-party-hosted replica can read your preferences and memory, even if it cannot forge your
  identity. Self-host it, or run your own `LocalReplicaTransport` peer.
- **Media bytes do not replicate.** Only an `artifact:{kind}` metadata op is journalled; files are
  fetched over the authenticated file route. That keeps a 4-megapixel image out of every device's
  journal, and it means a device that never generated an artifact cannot hoard all of them.
- **Chat transcripts (`conversations`, `messages`) are device-local** and deliberately not synced;
  memories, tasks, preferences, enrolments and policy are synced.
- **Refused writes are dead-lettered, not dropped.** `sync_dead_letters` keeps the op payload plus
  the replica's error; `jarvis sync --dead-letters` to look, `--retry-dead-letters` to requeue.
  A silently drained queue would be the data-loss bug people never find.
- **Uploads** are capped at `max_upload_bytes` (8 MiB) at the header, before body parsing, and audio
  decoders reject malformed/truncated WAV rather than guessing.
- **Secrets**: never logged, never echoed by an endpoint. `secret.key` is created `0600`; if you
  rotate it, existing sessions stop resolving (that is the intended behaviour, not a bug).

---

## HTTP hardening summary

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data: blob:; media-src 'self' blob: data:; connect-src 'self';
  font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: geolocation=(), microphone=(self), camera=(), storage-access=()
Cache-Control: no-store
```

`SECURITY_HEADERS` in `jarvis/api/app.py` is the single source of truth for that list, and
`Cache-Control: no-store` is what the API demands (the offline shell is cached by the service worker,
never by the browser cache). Plus: 413 before parse, 401 with `WWW-Authenticate`, 429 on both the
auth budget and the biometric budget — the latter with `Retry-After: 10` — and the service worker
never caching `/api/`. `POST /api/demo/reset` — which deletes
the database — returns 404 unless `JARVIS_ENABLE_DEMO_RESET=1`, and requires a session when enabled.

If you put this behind a domain, terminate TLS in front of it and set
`JARVIS_REDIRECT_BASE_URL=https://your.host`; do not rely on the app to redirect you into HTTPS,
because a bearer token sent over plain HTTP is a bearer token logged everywhere.

---

## Known limitations, plainly

| Limitation | Why it is like this |
| --- | --- |
| Single user per server; no per-user quota or isolation between users beyond row scoping | The engine is one trust domain. Multi-tenancy would need per-tenant DBs, per-tenant executors in a sandbox, and per-tenant temp dirs (the speaker-model cache is `tempfile.gettempdir()/jarvis-speaker-model`, shared on a multi-user machine) |
| No liveness detection on the voiceprint | Playback attacks are plausible; mitigated only by making it a step-up factor rather than an authoriser |
| No TOTP/hardware-key second factor yet, though session scope already distinguishes "verified" | A WebAuthn assertion is the right second factor; the `privileged` scope exists so it can be added without reworking every route |
| `RateLimiter` is in-process | Restarts reset budgets. Deliberate: "we must run Redis to be safe" is how local-first projects die. Behind a public interface, front it with a real limiter |
| The local media renderer is not a model | Deterministic, prompt-seeded composition. It is honest about being that, and about `fallback_used` when a provider call fails |
| ASR quality offline is a closed vocabulary | Better requires installing a model (`.[local-asr]`); we would rather return `available: false` than transcribe your sentence wrong and act on it |
| Trust in the replica | Plaintext ops; see above |

## Reporting

Open an issue for anything that looks wrong in a policy check, an auth path, or a conflict rule. For
something you would rather not post publicly, send it privately; the code paths that matter most are
`jarvis/devctl.py::Policy`, `jarvis/auth.py`, `jarvis/sync.py::SyncServer.ingest`, and
`jarvis/api/app.py::privileged_or_403`.
