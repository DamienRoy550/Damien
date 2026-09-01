"""``jarvis`` command line — the offline path.

Every subcommand here runs against the local database with no server and no
network, because that is the requirement being satisfied: core features must work
without internet. ``demo`` runs a scripted end-to-end proof of adaptive learning,
voice step-up, offline queueing, and cross-device conflict convergence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from jarvis import voice as voice_mod
from jarvis.accounts import AccountStore
from jarvis.adaptive import TRAIT_LABELS, AdaptiveModel
from jarvis.api.app import Jarvis
from jarvis.assistant import Assistant
from jarvis.auth import AuthService
from jarvis.config import get_settings
from jarvis.db import Database
from jarvis.devctl import CATALOG, ControlService
from jarvis.media import MediaService
from jarvis.memory import MemoryStore
from jarvis.sync import LocalReplicaTransport, SyncClient


def _paint(text: str, colour: str) -> str:
    codes = {"dim": "2", "cyan": "36", "green": "32", "yellow": "33", "red": "31", "bold": "1"}
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{codes[colour]}m{text}\033[0m"


def _wrap(text: str, width: int = 92, indent: str = "") -> str:
    out, line = [], indent
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line.rstrip())
            line = indent
        line += word + " "
    out.append(line.rstrip())
    return "\n".join(out)


class Cli:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        if args.data_dir:
            os.environ["JARVIS_DATA_DIR"] = args.data_dir
        self.settings = get_settings()
        self.settings.ensure_secret_key()
        self.core = Jarvis(self.settings)
        self.db: Database = self.core.db
        self.user_id = args.as_user or self._default_user()

    def _default_user(self) -> str | None:
        users = AccountStore(self.db, self.settings).list_users()
        return users[-1]["id"] if users else None

    def require_user(self) -> str:
        if not self.user_id:
            raise SystemExit("no account yet — run:  jarvis init --email you@example.com")
        return self.user_id

    # ------------------------------------------------------------ commands
    def init(self) -> None:
        accounts = AccountStore(self.db, self.settings)
        email = self.args.email
        existing = accounts.find_user_by_email(email) if email else None
        if existing:
            self.user_id = existing["id"]
            print(_paint(f"using existing account {existing['display_name']} ({existing['id']})", "green"))
        else:
            user = accounts.create_user(self.args.name or email.split("@")[0], email=email)
            self.user_id = user["id"]
            if email:
                accounts.link_identity(user["id"], "cli", "local-" + user["id"], email=email, email_verified=False)
            print(_paint(f"created {user['display_name']} ({user['id']})", "cyan"))
        accounts.register_device(
            self.user_id, self.settings.device_id, name=self.settings.device_name, platform="cli", capabilities=["mic"]
        )
        print(f"data dir   {self.settings.data_dir}")
        print(f"database   {self.settings.db_path}")
        print(f"device id  {self.settings.device_id}")
        print(_paint("next: jarvis say \"brainstorm names for my side project\"", "dim"))

    def doctor(self) -> None:
        state = self.core.connectivity.state(self.settings, force=True)
        creds = self.settings.provider_credentials()
        print(_paint("Jarvis core — capability report", "bold"))
        rows = [
            ("network", f"{'online' if state.online else 'offline'} ({state.detail})"),
            ("local ASR engines", json.dumps(state.local_engines, default=str)),
            ("sign-in providers", "configured" if creds["identity"] else _paint("none → dev IdP only", "yellow")),
            ("LLM", "configured" if creds["llm"] else _paint("not set → local generators", "yellow")),
            ("image", self.settings.image_provider),
            ("video", self.settings.video_provider),
            ("database", str(self.settings.db_path)),
            ("account", self.user_id or _paint("none — run jarvis init", "yellow")),
        ]
        model = AccountStore(self.db, self.settings).get_voiceprint(self.user_id) if self.user_id else None
        rows.append((
            "voiceprint",
            _paint(f"enrolled, calibrated · {model.sample_count} samples · threshold {model.threshold:.3f}", "green")
            if model and model.calibrated
            else _paint(f"enrolled ({model.sample_count}) but NOT calibrated → privileged step-up stays locked", "yellow") if model
            else _paint("not enrolled → privileged step-up impossible", "yellow"),
        ))
        rows.append((
            "dev IdP",
            _paint("enabled (passwordless) — fine locally, turn off if reachable", "yellow")
            if self.settings.allow_passwordless_dev_idp
            else "disabled (OIDC only)",
        ))
        refused = int(self.db.scalar("SELECT COUNT(*) FROM sync_dead_letters", (), 0) or 0)
        if refused:
            rows.append(("refused ops", _paint(f"{refused} — run `jarvis sync --dead-letters`", "red")))
        for key, value in rows:
            print(f"  {key:20s} {value}")
        if os.environ.get("JARVIS_ENABLE_DEMO_RESET") == "1":
            print(_paint("\n⚠ POST /api/demo/reset is live: it deletes the database.", "red"))
        print(_paint("\noffline behaviour", "bold"))
        for feature in ("voiceprint", "adaptive learning", "memory retrieval", "device control (local)", "image/video render", "sync queue"):
            print(f"  {_paint('✓', 'green')} {feature}")

    def say(self) -> None:
        user_id = self.require_user()
        assistant = Assistant(self.db, self.settings, user_id)
        from jarvis.assistant import LLMClient

        client = LLMClient(self.settings)
        engine = client if (client and self.core.connectivity.state(self.settings).capabilities.get("llm")) else None
        started = time.time()
        reply = assistant.respond(self.args.text, llm_client=engine)
        elapsed = int((time.time() - started) * 1000)
        print(_wrap(reply.text))
        print()
        print(_paint(f"intent={reply.intent} engine={reply.engine} {elapsed}ms", "dim"))
        if reply.meta.get("style_edits"):
            print(_paint("style applied: " + ", ".join(reply.meta["style_edits"]), "dim"))
        if reply.follow_ups:
            print(_paint("follow up: " + " | ".join(reply.follow_ups), "dim"))

    def learn(self) -> None:
        user_id = self.require_user()
        adaptive = AdaptiveModel(self.db, self.settings, user_id)
        result = adaptive.record_feedback(1 if self.args.positive else -1, note=self.args.note or "")
        print(_paint(f"feedback recorded · adjusted: {json.dumps(result.get('adjusted', {}))}", "green" if self.args.positive else "yellow"))
        directive, _ = adaptive.style_directive()
        print(_wrap(directive, indent="  "))

    def prefs(self) -> None:
        user_id = self.require_user()
        adaptive = AdaptiveModel(self.db, self.settings, user_id)
        if self.args.set:
            key, _, value = self.args.set.partition("=")
            if key not in TRAIT_LABELS:
                raise SystemExit(f"unknown trait '{key}' — one of: {', '.join(TRAIT_LABELS)}")
            try:
                adaptive.set_trait(key, float(value))
            except ValueError:
                raise SystemExit("value must be a number between -1 and 1") from None
            print(_paint(f"{key} = {value}", "green"))
            return
        _, profile = adaptive.style_directive()
        print(_paint("learned traits (raw → effective, after decay)", "bold"))
        for key, data in profile["traits"].items():
            if not data["hits"] and abs(data["raw"]) < 0.001:
                continue
            bar = self._bar(data["effective"])
            print(f"  {key:16s} {bar}  {data['raw']:+.2f} → {data['effective']:+.2f}  ({data['hits']} obs, {data['age_days']}d)")
        if profile["interests"]:
            print(_paint("\ninterests", "bold"))
            print("  " + ", ".join(f"{i['topic']}({i['score']:.2f})" for i in profile["interests"][:12]))
        print(_paint("\ndirectives", "bold"))
        for line in profile["directives"]:
            print("  " + _wrap(line, indent="   ").strip())
        print(_paint(f"\n{profile['observations']} observations · {profile['feedback_count']} explicit ratings", "dim"))

    def remember(self) -> None:
        user_id = self.require_user()
        memory = MemoryStore(self.db, self.settings, user_id)
        mid = memory.remember(self.args.text)
        print(_paint(f"remembered ({mid})", "green"))

    def recall(self) -> None:
        user_id = self.require_user()
        memory = MemoryStore(self.db, self.settings, user_id)
        hits = memory.search(self.args.query, limit=self.args.limit)
        if not hits:
            print(_paint("nothing stored that matches", "yellow"))
            return
        for hit in hits:
            print(f"  {hit['score']:.3f}  {hit['body']}  {_paint('[' + str(hit['age_days']) + 'd]', 'dim')}")

    def sync(self) -> None:
        device_id = self.args.device or self.settings.device_id
        client = SyncClient(self.db, device_id, self.core.sync_transport, batch_limit=self.settings.sync_batch_limit)
        if getattr(self.args, "dead_letters", False):
            rows = client.dead_letters(50)
            if not rows:
                print(_paint("no refused ops — every local change reached the replica", "green"))
            for r in rows:
                print(f"  {r['entity']}/{r['entity_key']}: {r['error']}")
            return
        if getattr(self.args, "retry_dead", False):
            n = client.retry_dead_letters()
            print(_paint(f"requeued {n} refused op(s); run 'jarvis sync' to push them", "yellow" if n else "green"))
            return
        if self.args.status_only:
            pending = client.pending_count()
            dead = client.dead_letter_count()
            note = _paint(f", {dead} refused", "red") if dead else ""
            print(f"device {device_id}: {pending} queued op(s), cursor {client.cursor()}{note}")
            return
        report = client.sync(self.user_id)
        print(_paint(
            f"{'push' if report.online else 'OFFLINE — still queued'}: pushed {report.pushed}, pulled {report.pulled}, applied {report.applied}, conflicts {report.conflicts}, pending {report.pending}",
            "green" if report.online else "yellow",
        ))

    def devices(self) -> None:
        user_id = self.require_user()
        accounts = AccountStore(self.db, self.settings)
        if self.args.trust:
            updated = accounts.set_device_trust(user_id, self.args.trust, "trusted", by="cli")
            print(_paint(f"trusted: {updated['name']}" if updated else "unknown device", "green" if updated else "red"))
            return
        for device in accounts.list_devices(user_id):
            mark = _paint("trusted", "green") if device["trust_level"] == "trusted" else device["trust_level"]
            print(f"  {device['name']:22s} {device['id']:16s} {device['platform']:10s} {mark}")

    def control(self) -> None:
        user_id = self.require_user()
        service = ControlService(self.db, self.settings)
        if self.args.list:
            for name, cap in CATALOG.items():
                colour = "red" if cap.risk in {"high", "forbidden"} else "yellow" if cap.risk == "medium" else "green"
                print(f"  {name:22s} {_paint(cap.risk, colour):>28s}  {cap.description[:60]}")
            return
        if self.args.allow:
            print(json.dumps(service.policy.allow(user_id, self.args.allow, max_risk=self.args.max_risk), indent=2))
            return
        if self.args.pause is not None:
            service.policy.kill_switch(user_id, enabled=self.args.pause)
            print(_paint(f"device control {'paused' if self.args.pause else 'resumed'} (syncs to every device)", "yellow" if self.args.pause else "green"))
            return
        if self.args.audit:
            for entry in service.policy.history(user_id, limit=self.args.audit):
                colour = "red" if entry["decision"] == "denied" else "green" if entry["decision"] == "executed" else "yellow"
                print(f"  {_paint(entry['decision'], colour):>30s}  {entry['action']:18s} {entry['reason'] or ''}")
            return
        if not self.args.exec:
            raise SystemExit("nothing to do — try --list, --allow CAPABILITY, --exec CAPABILITY, --audit 20")
        capability = self.args.exec
        args = json.loads(self.args.args) if self.args.args else {}
        session = {"scope": "privileged" if self.args.privileged else "basic", "device_trusted": True}
        result = service.execute(user_id, capability, args, session=session, dry_run=self.args.dry_run, confirmation=self.args.confirm)
        colour = "green" if result["status"] in {"executed", "dry_run"} else "red" if result["status"] in {"denied", "failed"} else "yellow"
        print(_paint(f"{result['status']}", colour), end="")
        print(f" — {result.get('reason') or (result.get('result') or {}).get('detail') or ''}")
        if result.get("confirmation_token"):
            print(_paint(f"confirmation token (5 min, single use): {result['confirmation_token']}", "yellow"))
        for check in (result.get("decision") or {}).get("checks", []):
            mark = _paint("✓", "green") if check["passed"] else _paint("✗", "red")
            print(f"  {mark} {check['check']:22s} {check['detail']}")

    def image(self) -> None:
        user_id = self.require_user()
        media = MediaService(self.db, self.settings)
        out = Path(self.args.out or "jarvis.png")
        result = media.generate(user_id, self.args.kind, self.args.prompt, params={"width": self.args.width, "height": self.args.height, "style": self.args.style}, force_offline=self.args.offline)
        if result["status"] != "ready":
            raise SystemExit(_paint(f"generation failed: {result['error']}", "red"))
        blob = media.read_blob(result["id"])
        if blob is None:
            raise SystemExit("blob missing on this device")
        out.write_bytes(blob[0])
        print(_paint(f"{result['kind']} via {result['provider']} → {out} ({len(blob[0]) // 1024} KB, {result['duration_ms']} ms)", "green"))

    def voice(self) -> None:
        user_id = self.require_user()
        accounts = AccountStore(self.db, self.settings)
        auth = AuthService(self.db, self.settings)
        f0 = self.args.f0
        if self.args.enrol:
            count = max(1, self.args.enrol)
            for i in range(count):
                pcm = voice_mod.synthesize_probe_pcm(f"enrolment phrase number {i + 1} please", 2.6, f0)
                embedding, quality = voice_mod.embedding_from(voice_mod.decode_audio(pcm))
                existing = accounts.get_voiceprint(user_id)
                samples = [list(e) for e in (existing.embeddings if existing else [])] + [embedding]
                model = voice_mod.enroll(samples, provider=voice_mod.default_provider().name, quality=[*(existing.quality if existing else []), quality])
                accounts.put_voiceprint(user_id, model)
                print(f"  sample {i + 1}: snr {quality['snr_db']} dB · threshold {model.threshold:.3f}")
            model = accounts.get_voiceprint(user_id)
            print(_paint(f"enrolled {model.sample_count} samples. Not calibrated → privileged step-up stays locked until real-speech trials are run.", "yellow"))
            return
        if self.args.verify:
            pcm = voice_mod.synthesize_probe_pcm(self.args.verify, 2.6, f0)
            result = auth.verify_voice_step_up(user_id, self.args.session or "cli", pcm)
            colour = "green" if result.get("accepted") else "red"
            print(_paint(json.dumps(result, indent=2), colour))
            return
        model = accounts.get_voiceprint(user_id)
        if model is None:
            print(_paint("no voiceprint enrolled — try: jarvis voice --enrol 3", "yellow"))
            return
        print(f"  samples     {model.sample_count}")
        print(f"  threshold   {model.threshold:.4f}")
        print(f"  provider    {model.provider}")
        print(f"  calibrated  {_paint('yes', 'green') if model.calibrated else _paint('no — step-up refused', 'yellow')}")
        print(f"  intra sims  {[round(s, 4) for s in model.intra_sims]}")
        print(f"  fingerprint {model.fingerprint}")

    def demo(self) -> None:
        """Scripted proof of the interesting parts, in throwaway databases."""
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="jarvis-demo-"))
        from jarvis.config import Settings

        laptop_dir, phone_dir = tmp / "laptop", tmp / "phone"
        laptop_dir.mkdir()
        phone_dir.mkdir()
        settings = Settings(data_dir=laptop_dir, device_id="laptop", device_name="MacBook")
        settings.ensure_secret_key()
        db = Database(settings.db_path)
        accounts = AccountStore(db, settings)

        print(_paint("\n── 1. sign in and register this device ─────────────────────────", "bold"))
        user = accounts.create_user("Demo User", email="demo@example.com")
        accounts.register_device(user["id"], "laptop", name="MacBook", platform="macos", capabilities=["mic"])
        print(f"  account {user['id']} on device 'laptop', database {settings.db_path.name}")

        print(_paint("\n── 2. it learns from what you say ─────────────────────────────", "bold"))
        assistant = Assistant(db, settings, user["id"])
        adaptive = AdaptiveModel(db, settings, user["id"])
        before, _ = adaptive.style_directive()
        reply = assistant.respond("brainstorm ways to speed up my morning routine")
        print("  first 2 lines of the answer:")
        for line in reply.text.splitlines()[:2]:
            print("    " + line[:98])
        assistant.respond("be much shorter and less formal from now on")
        after, profile = adaptive.style_directive()
        print(f"  directives changed after being told: {before != after}")
        print(f"  verbosity={profile['traits']['verbosity']['raw']:+.3f}  formality={profile['traits']['formality']['raw']:+.3f}")
        reply2 = assistant.respond("how should I handle a flaky test suite at 6pm?")
        print("  a later answer, reshaped by what it learned:")
        print("    " + reply2.text.splitlines()[0][:98])
        print(f"    style edits applied: {reply2.meta.get('style_edits')}")

        print(_paint("\n── 3. memory that survives a restart ──────────────────────────", "bold"))
        memory = MemoryStore(db, settings, user["id"])
        memory.remember("I fly business class only on flights over 8 hours")
        hits = memory.search("how long until business class is worth it")
        print(f"  retrieved: {hits[0]['body'] if hits else 'nothing'}")

        print(_paint("\n── 4. offline queue + two-device conflict convergence ─────────", "bold"))
        replica = Database(tmp / "replica.db")
        phone_settings = Settings(data_dir=phone_dir, device_id="phone", device_name="iPhone")
        phone_settings.secret_key = settings.secret_key
        phone_db = Database(phone_settings.db_path)
        with phone_db.write() as conn:
            conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES(?,?,?,?)", (user["id"], "Demo User", time.time(), "{}"))
        client_a = SyncClient(db, "laptop", LocalReplicaTransport(replica))
        client_b = SyncClient(phone_db, "phone", LocalReplicaTransport(replica))
        AdaptiveModel(db, settings, user["id"]).set_trait("humor", 0.9)
        AdaptiveModel(phone_db, phone_settings, user["id"]).set_trait("humor", -0.7)
        print(f"  laptop queued {client_a.pending_count()} op(s), phone queued {client_b.pending_count()} op(s), neither has synced")
        ra, rb = client_a.sync(user["id"]), client_b.sync(user["id"])
        client_a.sync(user["id"])
        client_b.sync(user["id"])
        va = float(db.scalar("SELECT raw FROM traits WHERE user_id=? AND key='humor'", (user["id"],), 0.0) or 0.0)
        vb = float(phone_db.scalar("SELECT raw FROM traits WHERE user_id=? AND key='humor'", (user["id"],), 0.0) or 0.0)
        print(f"  laptop: pushed {ra.pushed} pulled {ra.pulled} · phone: pushed {rb.pushed} pulled {rb.pulled}")
        print(f"  trait 'humor' → laptop {va:+.3f}, phone {vb:+.3f}, converged: {abs(va - vb) < 1e-9}")
        print(f"  conflicts observed and resolved by timestamp: {ra.conflicts + rb.conflicts}")

        print(_paint("\n── 5. voice step-up: owner passes, stranger does not ──────────", "bold"))
        model = None
        for i in range(3):
            emb, _quality = voice_mod.embedding_from(voice_mod.decode_audio(voice_mod.synthesize_probe_pcm(f"sample {i} open the garage door", 2.4, 118.0)))
            samples = [list(e) for e in (model.embeddings if model else [])] + [emb]
            model = voice_mod.enroll(samples)
        accounts.put_voiceprint(user["id"], model)
        owner_emb, _ = voice_mod.embedding_from(voice_mod.decode_audio(voice_mod.synthesize_probe_pcm("sample 3 open the garage door again", 2.4, 118.0)))
        owner = voice_mod.cosine(owner_emb, model.centroid)
        stranger_emb, _ = voice_mod.embedding_from(voice_mod.decode_audio(voice_mod.synthesize_probe_pcm("sample 3 open the garage door again", 2.4, 205.0)))
        stranger = voice_mod.cosine(stranger_emb, model.centroid)
        print(f"  owner {owner:.4f} · stranger {stranger:.4f} · threshold {model.threshold:.4f}")
        print(_paint(f"  → {'stranger rejected' if stranger < model.threshold else 'SECURITY FAILURE'}", "green" if stranger < model.threshold else "red"))
        print(_paint(f"  uncalibrated model: privileged grant refused even for the owner → {voice_mod.verify(model, owner_emb)['blocked_reason']}", "dim"))

        print(_paint("\n── 6. device control: policy first, then a real action ────────", "bold"))
        # only binaries the user registered as apps are ever spawnable, and the
        # demo registers /bin/sleep so it can show a real start/stop cycle
        os.environ["JARVIS_ALLOWED_APPS"] = "sleeper=/bin/sleep"
        control = ControlService(db, settings)
        _target = control.policy.register(user["id"], name="Living room TV", kind="tv",
                                         capabilities=["app.open", "app.close", "app.list", "power.shutdown"], pairing_verified=True)
        shown = control.execute(user["id"], "app.open", {"app": "vlc"}, session={"scope": "basic", "device_trusted": True})
        print(f"  1. no rule yet            → {shown['status']}: {shown.get('reason')}")
        control.policy.allow(user["id"], "app.open", config={"max_per_hour": 5})
        for extra in ("app.close", "app.list"):
            control.policy.allow(user["id"], extra)
        control.policy.allow(user["id"], "power.shutdown", max_risk="high")
        shown = control.execute(user["id"], "power.shutdown", {}, session={"scope": "basic", "device_trusted": True})
        print(f"  2. shutdown, basic scope  → {shown['status']}: {shown.get('reason')}")
        shown = control.execute(user["id"], "power.shutdown", {}, session={"scope": "privileged", "device_trusted": True})
        print(f"  3. shutdown, privileged   → {shown['status']} (single-use token issued, nothing ran)")
        dry = control.execute(user["id"], "app.open", {"app": "sleeper"}, session={"scope": "privileged", "device_trusted": True}, dry_run=True)
        print(f"  4. dry run                → {dry['status']} plan={dry.get('plan')}")
        hostile = control.execute(user["id"], "app.open", {"app": "sleeper; rm -rf /"}, session={"scope": "privileged", "device_trusted": True})
        print(f"  5. shell injection        → {hostile['status']}: {str(hostile.get('reason'))[:70]}")
        unlisted = control.execute(user["id"], "app.open", {"app": "terminal"}, session={"scope": "privileged", "device_trusted": True})
        print(f"  6. app not allowlisted    → {unlisted['status']}: {str(unlisted.get('reason'))[:70]}")
        ran = control.execute(user["id"], "app.open", {"app": "sleeper"}, session={"scope": "privileged", "device_trusted": True})
        print(_paint(f"  7. executed                 → {ran['status']}: {str((ran.get('result') or {}).get('detail'))[:60]}", "green"))
        listed = control.execute(user["id"], "app.list", {}, session={"scope": "basic", "device_trusted": True})
        procs = (listed.get("result") or {}).get("detail") or []
        alive = [p for p in procs if p["alive"]]
        print(f"  8. app.list               → {len(alive)} live process(es) Jarvis owns")
        closed = control.execute(user["id"], "app.close", {"app": "sleeper"}, session={"scope": "privileged", "device_trusted": True})
        print(_paint(f"  9. app.close              → {closed['status']}: {str((closed.get('result') or {}).get('detail'))[:60]}", "green"))
        stray = control.execute(user["id"], "app.close", {"app": "sleeper"}, session={"scope": "privileged", "device_trusted": True})
        print(f" 10. close what it did not start → {stray['status']}: {str((stray.get('result') or {}).get('error') or stray.get('reason'))[:60]}")
        entries = control.policy.history(user["id"], limit=50)
        print(f" 11. audit                    → {len(entries)} entries, {sum(1 for e in entries if e['decision'] == 'denied')} of them denials")
        control.policy.kill_switch(user["id"], enabled=True)
        paused = control.execute(user["id"], "app.open", {"app": "sleeper"}, session={"scope": "privileged", "device_trusted": True})
        print(_paint(f" 12. kill switch on          → {paused['status']}: {paused.get('reason')}", "yellow"))
        control.policy.kill_switch(user["id"], enabled=False)

        print(_paint("\n── 7. generate media with no network ──────────────────────────", "bold"))
        media = MediaService(db, settings)
        img = media.generate(user["id"], "image", "sunset over mountains for the office wall", params={"width": 640, "height": 400})
        vid = media.generate(user["id"], "video", "calm ocean waves loop", params={"width": 240, "height": 150, "seconds": 2.0, "fps": 6})
        print(f"  image: {img['status']} via {img['provider']} · {img['bytes'] // 1024} KB · {img['duration_ms']} ms")
        print(f"  video: {vid['status']} via {vid['provider']} · {vid['bytes'] // 1024} KB · {vid['duration_ms']} ms")
        print(f"  blobs: {media.blob_dir}")
        print(_paint(f"\nopen them:  {sorted(p.name for p in media.blob_dir.glob('*'))[:4]}", "dim"))

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _bar(value: float) -> str:
        width = 21
        filled = round((max(-1.0, min(1.0, value)) + 1) / 2 * width)
        return "▕" + "█" * filled + "·" * (width - filled) + "▏"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis core — offline-first assistant engine")
    parser.add_argument("--data-dir", help="override the local data directory")
    parser.add_argument("--as-user", dest="as_user", help="act as this user id")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the local account and device")
    p.add_argument("--email")
    p.add_argument("--name")
    sub.add_parser("doctor", help="report what works offline and what is configured")

    p = sub.add_parser("say", help="talk to the assistant")
    p.add_argument("text")

    p = sub.add_parser("learn", help="thumbs up/down the last answer")
    p.add_argument("positive", nargs="?", default="+1")
    p.add_argument("--note")

    p = sub.add_parser("prefs", help="inspect or set learned traits")
    p.add_argument("--set", metavar="KEY=VALUE", help="e.g. --set verbosity=-0.6")

    sub.add_parser("remember", help="store a fact").add_argument("text")
    p = sub.add_parser("recall", help="search memory")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("sync", help="push/pull queued changes")
    p.add_argument("--device")
    p.add_argument("--status", dest="status_only", action="store_true")
    p.add_argument("--dead-letters", dest="dead_letters", action="store_true", help="show ops the replica refused")
    p.add_argument("--retry-dead-letters", dest="retry_dead", action="store_true", help="requeue refused ops")

    p = sub.add_parser("devices", help="list or trust devices")
    p.add_argument("--trust", metavar="DEVICE_ID")

    p = sub.add_parser("control", help="device control with policy checks")
    p.add_argument("--list", action="store_true")
    p.add_argument("--allow", metavar="CAPABILITY")
    p.add_argument("--max-risk", choices=["low", "medium", "high"])
    p.add_argument("--exec", metavar="CAPABILITY")
    p.add_argument("--args", help="JSON object")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--confirm", metavar="TOKEN")
    p.add_argument("--privileged", action="store_true", help="pretend the session is step-up verified")
    p.add_argument("--pause", type=lambda v: v.lower() in {"1", "true", "yes", "on"}, default=None)
    p.add_argument("--audit", type=int, default=0, metavar="N")

    p = sub.add_parser("image", help="generate an image (or --kind video)")
    p.add_argument("prompt")
    p.add_argument("--kind", choices=["image", "video"], default="image")
    p.add_argument("--width", type=int, default=800)
    p.add_argument("--height", type=int, default=500)
    p.add_argument("--style", default="poster")
    p.add_argument("--out")
    p.add_argument("--offline", action="store_true")

    p = sub.add_parser("voice", help="voiceprint status / enrol / verify")
    p.add_argument("--enrol", type=int, metavar="N")
    p.add_argument("--verify", metavar="PHRASE")
    p.add_argument("--f0", type=float, default=118.0)
    p.add_argument("--session")

    sub.add_parser("demo", help="run a scripted end-to-end demonstration")

    p = sub.add_parser("serve", help="run the local web app")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "learn":
        args.positive = str(args.positive).startswith("+") or args.positive in {"1", "up", "yes"}
    if args.command == "serve":
        import uvicorn

        get_settings().ensure_secret_key()
        uvicorn.run("jarvis.api.app:build_default_app", host=args.host, port=args.port, reload=args.reload, factory=True)
        return 0
    cli = Cli(args)
    handler = getattr(cli, args.command, None)
    if handler is None:
        parser.error(f"no handler for {args.command}")
    handler()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
