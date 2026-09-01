"""Adaptive learning: preferences, communication style, and interests.

How it learns
-------------
Each communication-style dimension is a bounded trait in ``[-1, 1]``. Every turn
produces *signals* — explicit feedback, self-corrections, phrasing the user
repeats, engagement — and each signal nudges the traits that were actually used
to produce that turn (credit assignment, so a "too long" complaint penalises the
verbosity that caused it rather than the whole profile).

Update rule is a normalised asymptotic step, so no signal can saturate the model
and 20 quick wins don't outweigh a persistent pattern::

    raw' = clamp(raw + lr * delta * (1 - |raw|))

Recency
-------
``raw`` is retained knowledge; *influence* decays with a configurable half-life,
so a habit you stopped having fades out of your directives but returns instantly
when you pick it up again (``hits`` restores confidence without re-learning).

Everything is stored through the op-log, so traits sync across devices and
survive being offline indefinitely.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

TRAIT_LABELS: dict[str, tuple[str, str, str]] = {
    # key: (low label, high label, what the directive does)
    "verbosity": ("terse", "thorough", "answer length"),
    "formality": ("casual", "formal", "register"),
    "warmth": ("neutral", "warm", "friendliness / encouragement"),
    "humor": ("dry", "playful", "jokes and lightness"),
    "technical_depth": ("plain language", "expert detail", "jargon and depth"),
    "directness": ("guided", "blunt", "how much to soften a no"),
    "emoji": ("no emoji", "emoji welcome", "emoji use"),
    "structure": ("prose", "structured lists", "bullets/headers vs paragraphs"),
    "questions": ("just answer", "ask clarifying questions", "proactivity in asking"),
    "risk_appetite": ("conservative", "bold", "recommendation aggressiveness"),
}

INTEREST_STOPWORDS = {
    "the", "and", "for", "you", "are", "with", "that", "this", "have", "has", "not",
    "but", "can", "will", "would", "should", "about", "there", "their", "them", "what",
    "when", "where", "which", "while", "your", "was", "were", "been", "being", "from",
    "into", "out", "some", "any", "all", "just", "like", "get", "got", "make", "made",
    "do", "does", "did", "please", "need", "want", "know", "think", "way", "lot",
}

#: explicit imperatives -> (trait, direction). Matched on user corrections so a
#: single "stop doing that" permanently shifts the profile.
STYLE_COMMANDS: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\b(shorter|concise|brief|too long|tl;?dr|keep it short)\b", re.I), "verbosity", -0.9),
    (re.compile(r"\b(longer|more detail|in depth|elaborate|expand|more thorough)\b", re.I), "verbosity", 0.9),
    (re.compile(r"\b(less formal|casual|chill|relaxed|informal)\b", re.I), "formality", -0.9),
    (re.compile(r"\b(more formal|professional|proper)\b", re.I), "formality", 0.9),
    (re.compile(r"\b(no emoji|without emoji|drop the emoji)\b", re.I), "emoji", -1.0),
    (re.compile(r"\b(use emoji|more emoji)\b", re.I), "emoji", 1.0),
    (re.compile(r"\b(simpler|plain english|explain like i.?m|lay(?:man)? terms)\b", re.I), "technical_depth", -0.9),
    (re.compile(r"\b(technical jargon|expert level|advanced detail)\b", re.I), "technical_depth", 0.9),
    (re.compile(r"\b(be funnier|more humor|joke around|playful)\b", re.I), "humor", 0.9),
    (re.compile(r"\b(no jokes|be serious|stop joking|dry)\b", re.I), "humor", -0.9),
    (re.compile(r"\b(warm(er)?|friendlier|kinder|nicer)\b", re.I), "warmth", 0.8),
    (re.compile(r"\b(be direct|blunt|straight up|don't sugarcoat|stop hedging)\b", re.I), "directness", 0.9),
    (re.compile(r"\b(bullets|lists|structured|headers)\b", re.I), "structure", 0.9),
    (re.compile(r"\b(prose|paragraphs|flowing)\b", re.I), "structure", -0.9),
    (re.compile(r"\b(ask me questions|check with me first|clarify)\b", re.I), "questions", 0.9),
    (re.compile(r"\b(don'?t ask just do it|no clarifying|just answer)\b", re.I), "questions", -0.9),
    # "I like long, formal explanations" states a preference without an explicit command
    # word, and the earlier patterns (which key on "shorter"/"more formal") miss it —
    # so the phrasing a user is most likely to try at the start learned nothing.
    (re.compile(r"\bi (?:like|prefer|would like|want)\b[^.?]{0,40}?\b(long|detailed|thorough|in[- ]depth|full)\b", re.I), "verbosity", 0.8),
    (re.compile(r"\bi (?:like|prefer|would like|want)\b[^.?]{0,40}?\b(short|brief|concise|snappy)\b", re.I), "verbosity", -0.8),
    (re.compile(r"\bi (?:like|prefer|would like|want)\b[^.?]{0,40}?\b(formal|professional|polished|proper)\b", re.I), "formality", 0.8),
    (re.compile(r"\b(?:i (?:like|prefer)\b[^.?]{0,40}?\bcasual|don'?t (?:be |be so |talk so |sound so )?(?:too )?formal|stop (?:being |being )?so formal|no need to be formal)\b", re.I), "formality", -0.8),
    (re.compile(r"\b(ask questions|check in with me|clarify first)\b", re.I), "questions", 0.8),
    (re.compile(r"\b(take risks|bold(er)?|ambitious)\b", re.I), "risk_appetite", 0.8),
    (re.compile(r"\b(play it safe|conservative|careful)\b", re.I), "risk_appetite", -0.8),
]

#: implicit signals harvested from natural language, weighted conservatively
IMPLICIT_SIGNALS: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"\bthanks?\b|\bgreat\b|\bperfect\b|\bexactly\b|\bnice\b|\bawesome\b", re.I), "_valence", 0.4),
    (re.compile(r"\bno,? i meant\b|\bi didn'?t ask\b|\bwrong\b|\bnot what i\b|\bthat'?s not it\b", re.I), "_valence", -0.7),
    (re.compile(r"\??\s*$", re.I), "questions", 0.12),  # user asks a lot -> mirror their engagement style
]


@dataclass
class TraitState:
    key: str
    raw: float
    hits: int
    updated_at: float
    half_life_days: float

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.updated_at) / 86400.0)

    @property
    def recency(self) -> float:
        return 0.5 ** (self.age_days / self.half_life_days)

    @property
    def confidence(self) -> float:
        """Saturating trust in the estimate: 5+ observations is fully trusted."""
        return min(1.0, 0.25 + 0.15 * self.hits) if self.hits else 0.0

    @property
    def effective(self) -> float:
        return self.raw * self.recency * self.confidence

    def as_dict(self) -> dict[str, Any]:
        low, high, _ = TRAIT_LABELS.get(self.key, (self.key, self.key, ""))
        return {
            "key": self.key,
            "raw": round(self.raw, 4),
            "effective": round(self.effective, 4),
            "hits": self.hits,
            "confidence": round(self.confidence, 3),
            "recency": round(self.recency, 3),
            "age_days": round(self.age_days, 2),
            "low_label": low,
            "high_label": high,
            "label": high if self.effective > 0.15 else low if self.effective < -0.15 else "balanced",
        }


class AdaptiveModel:
    """Learned profile for one user: traits, interests, and derived directives."""

    def __init__(self, db, settings, user_id: str):
        self.db = db
        self.settings = settings
        self.user_id = user_id

    # ------------------------------------------------------------------ write
    def _trait(self, key: str) -> TraitState:
        row = self.db.one("SELECT raw, hits, updated_at FROM traits WHERE user_id=? AND key=?", (self.user_id, key))
        if row is None:
            return TraitState(key, 0.0, 0, 0.0, self.settings.trait_half_life_days)
        return TraitState(key, float(row["raw"]), int(row["hits"]), float(row["updated_at"]), self.settings.trait_half_life_days)

    def all_traits(self) -> dict[str, TraitState]:
        out = {k: TraitState(k, 0.0, 0, 0.0, self.settings.trait_half_life_days) for k in TRAIT_LABELS}
        for row in self.db.query("SELECT key, raw, hits, updated_at FROM traits WHERE user_id=?", (self.user_id,)):
            out[row["key"]] = TraitState(row["key"], float(row["raw"]), int(row["hits"]), float(row["updated_at"]), self.settings.trait_half_life_days)
        return out

    def reinforce(self, key: str, delta: float, *, lr: float | None = None, persist: bool = True) -> TraitState:
        """Apply one normalised learning step to a trait."""
        lr = self.settings.trait_reinforcement if lr is None else lr
        state = self._trait(key)
        lo, hi = self.settings.trait_bounds
        raw = max(lo, min(hi, state.raw + lr * delta * (1.0 - abs(state.raw))))
        state.raw = raw
        state.hits += 1
        state.updated_at = time.time()
        if persist:
            self.db.append_op(
                device_id=self.settings.device_id,
                user_id=self.user_id,
                entity="trait",
                entity_key=key,
                field=None,
                kind="set",
                payload={"raw": state.raw, "hits": state.hits},
            )
        return state

    def set_target(self, key: str, target: float, *, lr: float = 0.85) -> TraitState:
        """Move a trait toward a commanded *value*, not by a delta.

        Explicit instructions use this because they are statements of intent, not
        evidence: "be shorter" must take effect on the next reply even if the last
        answer was long, and a delta step damped by (1 - |raw|) — which exists so a
        trait eases into its bound instead of slamming into it — makes a reversal
        nearly impossible once a value is large. A correction that does not correct
        anything reads as the assistant not listening.
        """
        state = self._trait(key)
        lo, hi = self.settings.trait_bounds
        target = max(lo, min(hi, target))
        state.raw = max(lo, min(hi, state.raw + lr * (target - state.raw)))
        state.hits += 1
        state.updated_at = time.time()
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="trait",
            entity_key=key,
            field=None,
            kind="set",
            payload={"raw": state.raw, "hits": state.hits},
        )
        return state

    def penalise(self, key: str, magnitude: float = 1.0) -> TraitState:
        """Move a trait toward neutral: 'that was wrong', not 'invert it'."""
        state = self._trait(key)
        lo, hi = self.settings.trait_bounds
        shrink = state.raw * (1.0 - self.settings.trait_penalty * max(0.0, min(1.0, magnitude)))
        state.raw = max(lo, min(hi, shrink))
        state.hits += 1
        state.updated_at = time.time()
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="trait",
            entity_key=key,
            field=None,
            kind="set",
            payload={"raw": state.raw, "hits": state.hits},
        )
        return state

    def set_trait(self, key: str, value: float) -> TraitState:
        """Explicit user-set preference (slider in the UI). Counts as high-confidence."""
        state = self._trait(key)
        state.raw = max(self.settings.trait_bounds[0], min(self.settings.trait_bounds[1], value))
        state.hits = max(state.hits, 6)
        state.updated_at = time.time()
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="trait",
            entity_key=key,
            field=None,
            kind="set",
            payload={"raw": state.raw, "hits": state.hits},
        )
        return state

    # ----------------------------------------------------------------- events
    def observe_turn(
        self,
        user_text: str,
        *,
        assistant_text: str = "",
        assistant_traits: dict[str, float] | None = None,
        channel: str = "web",
        engagement_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Learn from a natural turn: style commands, implicit sentiment, interests."""
        applied: dict[str, Any] = {"explicit": {}, "implicit": {}, "interests": [], "interaction_id": None}

        interaction_id = self._record_interaction("user", user_text, {"channel": channel})
        if assistant_text:
            self._record_interaction(
                "assistant",
                assistant_text,
                {"channel": channel, "attributed_traits": assistant_traits or {}, "reply_to": interaction_id},
            )
        applied["interaction_id"] = interaction_id

        for pattern, trait, direction in STYLE_COMMANDS:
            if pattern.search(user_text):
                # Slow accumulation is reserved for inference (below) and thumbs feedback,
                # where a single signal genuinely is weak evidence; here it is a command.
                state = self.set_target(trait, direction)
                applied["explicit"][trait] = round(state.raw, 3)

        for pattern, trait, weight in IMPLICIT_SIGNALS:
            if pattern.search(user_text):
                if trait == "_valence":
                    self._implicit_valence(user_text, weight)
                else:
                    self.reinforce(trait, weight, lr=0.06)
                    applied["implicit"][trait] = weight

        # reading time is a weak but honest preference signal for verbosity
        if engagement_seconds is not None:
            words = len(assistant_text.split())
            if words > 0:
                wpm_expected = 220.0
                expected = words / wpm_expected * 60.0
                if expected > 0:
                    ratio = engagement_seconds / expected
                    if ratio > 1.6:
                        self.reinforce("verbosity", 0.2, lr=0.05)
                    elif 0 < ratio < 0.45:
                        self.reinforce("verbosity", -0.2, lr=0.05)

        for topic in self.extract_topics(f"{user_text} {assistant_text}"):
            self._bump_interest(topic)
            applied["interests"].append(topic)

        return applied

    def _implicit_valence(self, user_text: str, weight: float) -> None:
        """Attribute sentiment to whatever the assistant just did."""
        row = self.db.one(
            """SELECT meta FROM interactions
               WHERE user_id=? AND kind='response' ORDER BY created_at DESC LIMIT 1""",
            (self.user_id,),
        )
        if row is None:
            return
        meta = self.db.jloads(row["meta"], {})
        traits = meta.get("attributed_traits") or {}
        for trait, value in traits.items():
            if abs(float(value)) < 0.1:
                continue
            direction = math.copysign(1.0, float(value)) * weight
            self.reinforce(trait, direction, lr=0.12)

    def record_feedback(self, valence: int, *, interaction_id: str | None = None, note: str = "") -> dict[str, Any]:
        """Thumbs up/down: credit-assign to the exact traits used for that answer."""
        valence = 1 if valence >= 0 else -1
        target = interaction_id
        if target is None:
            row = self.db.one(
                """SELECT id, meta FROM interactions WHERE user_id=? AND kind='response'
                   ORDER BY created_at DESC LIMIT 1""",
                (self.user_id,),
            )
            if row is None:
                return {"adjusted": {}, "error": "nothing to attribute feedback to yet"}
            target, meta = row["id"], self.db.jloads(row["meta"], {})
        else:
            meta = self.db.jloads(
                self.db.scalar("SELECT meta FROM interactions WHERE id=?", (target,), "{}"), {}
            )
        traits = (meta or {}).get("attributed_traits") or {}
        adjusted: dict[str, float] = {}
        if valence > 0:
            for trait, value in traits.items():
                if abs(float(value)) < 0.1:
                    continue
                adjusted[trait] = round(self.reinforce(trait, math.copysign(0.5, float(value)), lr=0.2).raw, 3)
        else:
            for trait, value in traits.items():
                if abs(float(value)) < 0.1:
                    continue
                adjusted[trait] = round(self.penalise(trait, 0.8).raw, 3)
        # a disliked answer that was structured/verbose might mean the *content* was
        # fine but the delivery was not: also record the note as an interest signal
        if note:
            for topic in self.extract_topics(note):
                self._bump_interest(topic)
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO feedback(id, user_id, interaction_id, valence, created_at, attributed_keys) VALUES(?,?,?,?,?,?)",
                (f"fb_{int(time.time()*1000)}", self.user_id, target, valence, time.time(), json.dumps(sorted(adjusted))),
            )
            conn.execute(
                "UPDATE interactions SET meta=? WHERE id=?",
                (json.dumps({**(meta or {}), "feedback": valence}), target),
            )
        return {"adjusted": adjusted, "valence": valence, "interaction_id": target}

    def _record_interaction(self, kind: str, text: str, meta: dict) -> str:
        iid = f"int_{time.time_ns()}"
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO interactions(id, user_id, created_at, kind, text, meta) VALUES(?,?,?,?,?,?)",
                (
                    iid,
                    self.user_id,
                    time.time(),
                    "response" if kind == "assistant" else kind,
                    text,
                    json.dumps(meta),
                ),
            )
        return iid

    # --------------------------------------------------------------- interests
    def extract_topics(self, text: str, limit: int = 8) -> list[str]:
        words = re.findall(r"[A-Za-z][A-Za-z'’\-]{2,}", text.lower())
        seen: dict[str, int] = {}
        for w in words:
            w = w.strip("'’-")
            if w in INTEREST_STOPWORDS or len(w) < 4:
                continue
            seen[w] = seen.get(w, 0) + 1
        bigrams = {}
        for a, b in itertools.pairwise(words):
            if a in INTEREST_STOPWORDS or b in INTEREST_STOPWORDS:
                continue
            key = f"{a} {b}"
            bigrams[key] = bigrams.get(key, 0) + 1
        ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        out = [t for t, c in ranked]
        strong_bigrams = [t for t, c in bigrams.items() if c >= 2]
        for t in strong_bigrams[: max(0, limit - len(out))]:
            out.append(t)
        return out

    def _bump_interest(self, topic: str) -> None:
        row = self.db.one("SELECT weight, hits FROM interests WHERE user_id=? AND topic=?", (self.user_id, topic))
        weight = (float(row["weight"]) if row else 0.0) + 1.0
        hits = (int(row["hits"]) if row else 0) + 1
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="interest",
            entity_key=topic,
            field=None,
            kind="set",
            payload={"weight": weight, "hits": hits},
        )

    def interests(self, limit: int = 15) -> list[dict[str, Any]]:
        now = time.time()
        hl = self.settings.trait_half_life_days * 86400.0
        out = []
        for row in self.db.query("SELECT topic, weight, hits, last_seen FROM interests WHERE user_id=?", (self.user_id,)):
            age = now - float(row["last_seen"])
            score = math.log1p(float(row["weight"])) * math.exp(-age / hl) * 4
            out.append({"topic": row["topic"], "score": round(score, 4), "weight": round(float(row["weight"]), 2), "hits": int(row["hits"])})
        out.sort(key=lambda d: -d["score"])
        return out[:limit]

    # ------------------------------------------------------------- read-out
    def trait_vector(self) -> dict[str, float]:
        return {k: round(s.effective, 4) for k, s in self.all_traits().items()}

    def style_directive(self) -> tuple[str, dict[str, Any]]:
        """Natural-language instructions derived from the profile, for response assembly."""
        traits = self.all_traits()
        lines: list[str] = []
        for key, state in traits.items():
            eff = state.effective
            if abs(eff) < 0.12:
                continue
            low, high, aspect = TRAIT_LABELS.get(key, (key, key, key))
            if key == "verbosity":
                lines.append("Keep answers short and skip preamble." if eff < 0 else "Give thorough answers with supporting detail.")
            elif key == "formality":
                lines.append("Use a casual, conversational register." if eff < 0 else "Use a formal, professional register.")
            elif key == "warmth":
                lines.append("Be matter-of-fact; skip encouragement." if eff < 0 else "Be warm and encouraging; acknowledge effort.")
            elif key == "humor":
                lines.append("No jokes. Dry tone only." if eff < 0 else "Allow light humour where it does not distract.")
            elif key == "technical_depth":
                lines.append("Explain in plain language; define any term you must use." if eff < 0 else "Assume expert knowledge; use precise terminology.")
            elif key == "directness":
                lines.append("Be blunt. Lead with the answer or the refusal, no cushioning." if eff > 0 else "Soften corrections; explain before concluding.")
            elif key == "emoji":
                lines.append("Do not use emoji." if eff < 0 else "Emoji are welcome in moderation.")
            elif key == "structure":
                lines.append("Use bullets, numbered steps, and short headers." if eff > 0 else "Write flowing prose, not bullet lists.")
            elif key == "questions":
                lines.append("Ask a clarifying question before a big or irreversible step." if eff > 0 else "Do not ask clarifying questions; state assumptions and proceed.")
            elif key == "risk_appetite":
                lines.append("Recommend the bold option and name the upside." if eff > 0 else "Recommend the safe option and name the downside.")
            else:  # unknown trait synced from another device
                lines.append(f"{aspect}: lean {high if eff > 0 else low}.")
        interests = self.interests(6)
        if interests:
            lines.append(
                "User is currently invested in: " + ", ".join(i["topic"] for i in interests) + ". Prefer examples and analogies from these areas."
            )
        if not lines:
            lines.append("No strong preferences learned yet. Use a friendly, clear, medium-length default and learn from feedback.")
        profile = {
            "traits": {k: s.as_dict() for k, s in traits.items()},
            "interests": self.interests(),
            "directives": lines,
            "observations": int(sum(s.hits for s in traits.values())),
            "feedback_count": int(self.db.scalar("SELECT COUNT(*) FROM feedback WHERE user_id=?", (self.user_id,), 0) or 0),
        }
        return "\n".join(f"- {line}" for line in lines), profile

    def recommend(self, candidates: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        """Rank arbitrary candidate items by fit to learned interests *and* style."""
        interests = {i["topic"]: i["score"] for i in self.interests(40)}
        traits = self.trait_vector()
        scored = []
        for c in candidates:
            tokens = self.extract_topics(c, limit=20)
            fit = sum(interests.get(t, 0.0) for t in tokens)
            if traits.get("structure", 0) > 0.3 and len(c.split()) > 14:
                fit -= 0.4  # a list-oriented user gets short suggestions
            scored.append({"item": c, "score": round(fit, 4), "matched": [t for t in tokens if t in interests]})
        scored.sort(key=lambda d: -d["score"])
        return scored[:limit]
