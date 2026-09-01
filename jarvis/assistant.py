"""The assistant: intent routing, offline generators, LLM adapter, and reply shaping.

Two execution paths, one contract
---------------------------------
``respond()`` always returns the same shape. With credentials + connectivity it
calls an OpenAI-compatible chat model, injecting the *learned* style directive and
retrieved memory as context. Without them it uses the local generators below, so
"brainstorm with me", "draft a post", "what should I do about X" still produce
structured, useful output on a plane instead of an error.

Local generators are deliberate, not decorative:

* ``brainstorm`` — divergent ideas across named angles, then a convergent
  impact/effort triage and one recommended next step.
* ``write`` — outline then draft, with the shape chosen per surface (email, post,
  announcement, list) and the user's verbosity/structure traits applied after.
* ``advice`` — restates the goal, lists real options with their tradeoffs, picks
  per the learned ``risk_appetite``, and gives the smallest next step.
* ``plan`` — ordered steps with durations and a first-10-minutes entry point.
* ``summarise`` — extractive, frequency-scored sentence selection.

Reply shaping (:func:`apply_style`) is where adaptive learning becomes visible to
the user: sentence budget, list-vs-prose reflow, emoji stripping, preamble
removal. It is applied to local *and* LLM output, so the profile changes behaviour
even when a frontier model is doing the writing.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from jarvis.adaptive import AdaptiveModel
from jarvis.memory import MemoryStore

BRACKET = re.compile(r"\[[^\]]*\]|\([^)]*\)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF\U0001F1E6-\U0001F1FF\uFE0F\u2B00-\u2BFF\u2190-\u21FF\u2700-\u27BF]"
)


@dataclass
class Reply:
    text: str
    intent: str
    engine: str = "local"
    cards: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    follow_ups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "intent": self.intent, "engine": self.engine, "cards": self.cards,
                "actions": self.actions, "follow_ups": self.follow_ups, "meta": self.meta}


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------
INTENTS: list[tuple[str, re.Pattern[str]]] = [
    ("remember", re.compile(r"^\s*(remember|note (?:that|this)|keep in mind|don'?t forget)\b", re.I)),
    ("recall", re.compile(r"\b(what do you (know|remember)|do you remember|recall|my (prefs|preferences)|what did i (say|tell))\b", re.I)),
    ("preferences", re.compile(r"\b(set my preference|prefer\b|use (shorter|longer|less|more)|i like\b|i hate\b|stop (using|doing)|from now on\b|don'?t (use|be)\b)", re.I)),
    ("brainstorm", re.compile(r"\b(brainstorm|ideas?\b|give me options|ways to|how (can|might) i)\b", re.I)),
    ("write", re.compile(r"\b(writ(?:e|es|ing|ten)|draft(s|ed|ing)?|compose|rewrite|polish|turn this into|a post|an? email|a message to|caption)\b", re.I)),
    ("advice", re.compile(r"\b(should i|what (should|would) i|advice|is it (worth|better|ok to)|help me decide|weigh\b|pros and cons)\b", re.I)),
    ("plan", re.compile(r"\b(plan(s|ned|ning)?|schedule|roadmap|steps to|how do i start|organise|organize|breakdown)\b", re.I)),
    # stems get explicit suffix classes: \b(summar)\b never matches "summarise", and a
    # missed intent is a silently unhelpful assistant
    ("summarise", re.compile(r"\b(summar(?:ise|ize|ing|y)|tl;?dr|shorten this|key points|condense)\b", re.I)),
    ("control", re.compile(r"\b(open|close|launch|quit|shut down|turn (on|off)|volume|screenshot|set volume|put .* to sleep)\b", re.I)),
    ("image", re.compile(r"\b(image|picture|poster|wallpaper|logo|illustration|render|draw)\b", re.I)),
    ("video", re.compile(r"\b(video|animation|clip|gif|motion|loop)\b", re.I)),
    ("sync", re.compile(r"\b(sync|devices|offline|back ?up|push (my )?changes)\b", re.I)),
    ("feedback", re.compile(r"\b(thanks|great|perfect|exactly|nice|awesome|wrong|not what i|i didn'?t ask|that'?s not it|useless|bad)\b", re.I)),
    ("greeting", re.compile(r"^\s*(hi|hey|hello|yo|good (morning|afternoon|evening)|morning)\b[\s!.]*$", re.I)),
    ("identity", re.compile(r"\b(who are you|what are you|what can you do|help me with|capabilities)\b", re.I)),
]


def classify(text: str) -> str:
    for name, pattern in INTENTS:
        if pattern.search(text):
            return name
    return "chat"


# ---------------------------------------------------------------------------
# local generators
# ---------------------------------------------------------------------------
ANGLES = [
    ("obvious", "the version most people would try first"),
    ("inverted", "the opposite of the obvious approach"),
    ("cheap", "under an hour and near-zero cost"),
    ("ambitious", "what you would attempt with six weeks and a small team"),
    ("constraint-led", "start from the thing you least want to compromise on"),
    ("borrowed", "steal the mechanism from an unrelated field"),
]


def gen_brainstorm(topic: str, *, count: int = 6, interests: list[str] | None = None) -> Reply:
    topic = BRACKET.sub("", topic).strip(" ?.,!") or "your topic"
    lean = (interests or [])[:4]
    cards = []
    for i, (angle, framing) in enumerate(ANGLES[:count]):
        hook = f" (touches {'/'.join(lean[:2])})" if lean and i < 2 else ""
        cards.append({
            "kind": "idea", "angle": angle, "title": f"{angle.title()}: {topic}",
            "body": f"{framing.capitalize()}{hook}. For “{topic}”, this means committing to one concrete move this week, "
                    f"measuring whether it moved anything, and stopping if it did not.",
            "impact": ["high", "medium", "high", "high", "medium", "low"][i % 6],
            "effort": ["medium", "low", "low", "high", "medium", "high"][i % 6],
        })
    top = next((c for c in cards if c["impact"] == "high" and c["effort"] in {"low", "medium"}), cards[0])
    text = (
        f"Six angles on “{topic}”:\n\n"
        + "\n".join(f"{i + 1}. [{c['angle']}] {c['body']}" for i, c in enumerate(cards))
        + f"\n\nIf I had to pick one: start with **{top['angle']}** — high impact for {top['effort']} effort, and it is testable this week."
    )
    return Reply(text=text, intent="brainstorm", cards=cards,
                 follow_ups=["Expand idea 1 into a plan", f"What would break the {top['angle']} option?", "Give me a cheaper version"])


def gen_write(text: str, *, traits: dict[str, float] | None = None) -> Reply:
    traits = traits or {}
    target = "email" if re.search(r"e-?mail", text, re.I) else "post" if re.search(r"\bpost\b|\barticle\b|blog", text, re.I) else "announcement" if re.search(r"announce|launch", text, re.I) else "message"
    subject = BRACKET.sub("", text)
    subject = re.sub(r"^\s*(write|draft|compose|me|a|an|up)\s+", "", subject, flags=re.I).strip(" ?.,!") or "the thing you are announcing"
    outline = [
        "Open with the outcome the reader cares about, in one sentence.",
        "Give the single most useful detail — the thing that makes it real.",
        "State what you want from them (a decision, a reply, a click).",
        "Close with the deadline or next step, explicitly.",
    ]
    body = (
        f"**Draft — {target}**\n\n"
        f"Subject: {subject[:70]}\n\n"
        f"Hi [name],\n\n"
        f"Here is the short version: {subject} is ready, and the part that matters to you is what changes on your side.\n\n"
        f"What you need to know: it works now, it took one focused pass, and nothing about your current routine changes.\n\n"
        f"What I need from you: a yes or a no by Friday — silence will be read as a no, and that is fine.\n\n"
        f"Thanks,\n[you]"
    )
    structured = traits.get("structure", 0) > 0.2
    if structured:
        body += "\n\n**Outline used**\n" + "\n".join(f"- {line}" for line in outline)
    else:
        body += "\n\n(Outline behind it: " + " then ".join(item.rstrip(".").lower() for item in outline) + ".)"
    return Reply(text=body, intent="write", cards=[{"kind": "outline", "items": outline}],
                 follow_ups=["Make it warmer", "Half the length", "Add a subject line option"])


def gen_advice(dilemma: str, *, traits: dict[str, float] | None = None) -> Reply:
    traits = traits or {}
    subject = BRACKET.sub("", dilemma).strip(" ?.,!") or "this decision"
    bold = traits.get("risk_appetite", 0) > 0.15
    options = [
        {"label": "Do the small reversible version now", "upside": "learning this week, nothing to unwind", "downside": "you may end up doing it twice"},
        {"label": "Wait for more information", "upside": "fewer surprises if the picture genuinely changes", "downside": f"on {subject}, waiting usually costs the option itself"},
        {"label": "Commit fully in one move", "upside": "fast, and it forces the surrounding decisions", "downside": "expensive to reverse if the premise is wrong"},
    ]
    pick = options[2] if bold else options[0]
    text = (
        f"Goal, as I read it: {subject}.\n\n"
        + "\n".join(f"• {o['label']} — upside: {o['upside']}; downside: {o['downside']}." for o in options)
        + f"\n\nI would take **{pick['label'].lower()}**"
        + (", and I would take it now: you said you want the bold option, and the recovery cost here is lower than the delay cost."
           if bold else
           " — you have not told me you like big bets, and this one is cheap to try first.")
        + "\n\nSmallest next step: 15 minutes to write the constraint you refuse to move on, then decide against it."
    )
    return Reply(text=text, intent="advice", cards=[{"kind": "options", "options": options, "chosen": pick["label"]}],
                 follow_ups=["What would change your mind?", "Give me the failure cases", "Now plan the first week"])


def gen_plan(goal: str, *, traits: dict[str, float] | None = None) -> Reply:
    goal = BRACKET.sub("", goal).strip(" ?.,!") or "the thing you want done"
    steps = [
        ("Write down what 'done' means, in one sentence", 10, "definition"),
        ("List what you already have: access, data, people, prior attempts", 15, "inventory"),
        ("Find the one thing that blocks everything else", 20, "constraint"),
        ("Do the smallest version that a real person could use", 90, "prototype"),
        ("Test it on yourself for two days, notes on friction", 30, "validation"),
        ("Ship it, then decide whether to invest or stop", 45, "decision"),
    ]
    lines = [f"{i + 1}. {name} — ~{mins} min" for i, (name, mins, _) in enumerate(steps)]
    total = sum(m for _, m, _ in steps)
    text = (
        f"Plan for {goal} — about {total} minutes of work, front-loaded so you learn something early.\n\n"
        + "\n".join(lines)
        + "\n\nStart now with step 1; if it takes longer than 10 minutes, the goal is still too vague to plan against."
    )
    return Reply(text=text, intent="plan", cards=[{"kind": "steps", "items": [{"title": n, "minutes": m, "phase": p} for n, m, p in steps]}],
                 follow_ups=["Compress this into a weekend", "Which step could I skip?", "Turn step 4 into a checklist"])


def gen_summary(text: str, *, max_sentences: int = 3) -> Reply:
    body = BRACKET.sub("", text)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", body) if len(p.strip()) > 24]
    if len(parts) <= max_sentences:
        return Reply(text="\n".join(f"• {p}" for p in parts) or text, intent="summarise",
                     meta={"note": "already short — nothing to compress"})
    words = re.findall(r"[a-z']+", body.lower())
    stop = {"the", "and", "for", "you", "are", "with", "that", "this", "have", "from", "was", "were", "not", "but", "can", "will", "your", "they", "their", "there", "what", "when", "who", "how", "about", "into", "out", "all", "any", "just", "like", "some", "more", "most", "than", "then", "them", "here", "been", "being", "does", "did", "would", "should", "could", "because", "while", "after", "before", "over", "under", "very", "much", "many", "such", "only", "own", "same", "other", "each", "every"}
    freq = {w: words.count(w) for w in set(words) if w not in stop and len(w) > 2}
    scored = []
    for i, p in enumerate(parts):
        tokens = re.findall(r"[a-z']+", p.lower())
        score = sum(freq.get(t, 0) for t in tokens) / (len(tokens) or 1)
        scored.append((score, i, p))
    scored.sort(key=lambda t: (-t[0], t[1]))
    chosen = sorted(scored[:max_sentences], key=lambda t: t[1])
    return Reply(text="\n".join(f"• {p}" for _, _, p in chosen), intent="summarise",
                 cards=[{"kind": "note", "body": f"extractive: {len(parts)} sentences considered, {max_sentences} kept"}])


CAPABILITIES_TEXT = (
    "Here is what I can do right now, on this device, with no network:\n\n"
    "• Think with you: brainstorm, weigh options, plan, summarise.\n"
    "• Write: emails, posts, announcements — in the tone you have taught me.\n"
    "• Remember: tell me to remember something and I will retrieve it later.\n"
    "• Learn: say 'shorter', 'no emoji', 'be blunt' and it changes how I answer from then on.\n"
    "• Control devices you have registered, inside the safety rules you set.\n"
    "• Generate images and short animated clips offline; real models when configured.\n"
    "• Verify it is you by voice before anything risky.\n"
    "• Sync: everything above works offline and reconciles when you reconnect."
)


# ---------------------------------------------------------------------------
# style application
# ---------------------------------------------------------------------------
def apply_style(text: str, traits: dict[str, float]) -> tuple[str, list[str]]:
    """Transform completed text so it matches the learned profile. Returns (text, edits)."""
    edits: list[str] = []
    out = text
    if traits.get("emoji", 0) < -0.25 and EMOJI_RE.search(out):
        out = EMOJI_RE.sub("", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        edits.append("stripped emoji")
    verbosity = traits.get("verbosity", 0)
    sentences = SENTENCE_END.split(out.strip())
    if verbosity < -0.45 and len(sentences) > 4:
        keep = max(2, int(len(sentences) * 0.35))
        tail = sentences[-1] if len(sentences[-1]) < 160 else ""
        out = " ".join([s for s in sentences[:keep] if s.strip()] + ([tail] if tail else []))
        edits.append(f"compressed {len(sentences)} sentences to {keep}")
    if verbosity > 0.55 and len(out) < 900:
        edits.append("kept full detail (thorough preference)")
    preamble = re.match(r"^\s*(sure[,!]?\s+|of course[,!]?\s+|happy to help[,!]?\s+|great question[.!]?\s+|certainly[,!]?\s+)", out, re.I)
    if preamble:
        out = out[preamble.end() :]
        out = out[0].upper() + out[1:] if out else out
        edits.append("removed filler preamble")
    if traits.get("formality", 0) < -0.4:
        before = out
        out = re.sub(r"\b(The aforementioned\b)", "The", out)
        out = out.replace("Therefore", "So").replace("However,", "But").replace("Additionally,", "Also").replace("Utilise", "Use").replace("It is worth noting that", "Note that").replace("I would recommend", "I would")
        if out != before:
            edits.append("relaxed register")
    elif traits.get("formality", 0) > 0.4:
        before = out
        out = re.sub(r"\b(gonna|gonna\b)", "going to", out).replace("wanna", "want to").replace(" kinda", " somewhat").replace("gotta", " have to")
        if out != before:
            edits.append("tightened register")
    if traits.get("warmth", 0) < -0.3:
        before = out
        out = re.sub(r"\b(Thanks!|thanks so much|no worries|You'?ve got this|Absolutely!)\s*", "", out)
        if out != before:
            edits.append("removed encouragement padding")
    if traits.get("emoji", 0) > 0.45 and len(out) < 400 and "•" not in out and not out.endswith("✦"):
        out = out.rstrip(".") + " ✦"
        edits.append("added light flourish")
    return out.strip(), edits


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
class Assistant:
    def __init__(self, db, settings, user_id: str):
        self.db = db
        self.settings = settings
        self.user_id = user_id
        self.adaptive = AdaptiveModel(db, settings, user_id)
        self.memory = MemoryStore(db, settings, user_id)

    # ------------------------------------------------------------------ main
    def respond(self, text: str, *, channel: str = "web", engagement_seconds: float | None = None, llm_client: Any | None = None) -> Reply:
        intent = classify(text)
        traits = self.adaptive.trait_vector()
        memory_hits: list[dict[str, Any]] = []

        if intent == "remember":
            body = re.sub(r"^\s*(remember|note that|note this|keep in mind|don'?t forget)\b[:,]?\s*(that\s+)?", "", text, flags=re.I).strip()
            mid = self.memory.remember(body) if body else ""
            reply = Reply(text=f"Noted — {body}" if body else "What should I remember?", intent="remember",
                          meta={"memory_id": mid}, cards=[{"kind": "memory", "id": mid, "body": body}])
        elif intent == "recall":
            hits = self.memory.search(text, limit=6)
            memory_hits = hits
            if hits:
                reply = Reply(
                    text="What I have on that:\n\n" + "\n".join(f"• {h['body']} ({h['age_days']}d ago)" for h in hits),
                    intent="recall", cards=[{"kind": "memory", "id": h["id"], "body": h["body"], "tags": h["tags"]} for h in hits],
                    follow_ups=["Forget the last one", "Update that note"],
                )
            else:
                reply = Reply(text="I have nothing stored on that yet. Say 'remember …' and I will keep it.", intent="recall")
        elif intent == "preferences":
            applied = self.adaptive.observe_turn(text)
            reply = Reply(
                text=self._preference_echo(applied),
                intent="preferences",
                cards=[{"kind": "traits", "traits": {k: v for k, v in self.adaptive.trait_vector().items() if abs(v) > 0.05}}],
            )
        elif intent == "brainstorm":
            reply = gen_brainstorm(text, interests=[i["topic"] for i in self.adaptive.interests()])
        elif intent == "write":
            reply = gen_write(text, traits=traits)
        elif intent == "advice":
            reply = gen_advice(text, traits=traits)
        elif intent == "plan":
            reply = gen_plan(text, traits=traits)
        elif intent == "summarise":
            reply = gen_summary(text, max_sentences=2 if traits.get("verbosity", 0) < -0.4 else 3)
        elif intent == "identity":
            reply = Reply(text=CAPABILITIES_TEXT, intent="identity")
        elif intent == "greeting":
            greeting = "Hey." if traits.get("formality", 0) < -0.3 else "Good to see you."
            profile = self.adaptive.interests(3)
            extra = f" You have been deep in {', '.join(p['topic'] for p in profile)} lately." if profile else " Tell me what you are working on and I will keep up."
            reply = Reply(text=greeting + extra, intent="greeting")
        elif intent == "control":
            # Deliberately a *proposal*: control is policy-checked at /api/control/execute
            # (privileged session, allowlisted target, confirmation for irreversible
            # actions). An assistant reply that quietly called the executor would bypass
            # every one of those checks, so it never touches it.
            reply = Reply(
                text=(
                    "That is a device action, so I will not run it from a chat reply — it goes through the "
                    "control policy, which checks the target you registered, the risk level, your rate budget and, "
                    "for anything irreversible, a confirmation you have to approve."
                ),
                intent="control",
                actions=[{"kind": "navigate", "view": "control", "label": "Open device controls", "requested": text[:120]}],
                follow_ups=["List my devices", "What can I control?"],
            )
        else:
            reply = self._chat(text, traits)

        # online path: let a real model write it, then apply the same style rules
        if llm_client is not None and intent not in {"remember", "preferences", "recall"}:
            llm_reply = self._llm_reply(text, llm_client, intent=intent, traits=traits)
            if llm_reply is not None:
                reply, memory_hits = llm_reply, llm_reply.meta.get("memory_hits", memory_hits)

        styled, edits = apply_style(reply.text, traits)
        reply.text = styled
        reply.meta["style_edits"] = edits
        reply.meta["applied_traits"] = {k: round(v, 3) for k, v in traits.items() if abs(v) > 0.1}
        if not memory_hits:
            # the chat path already attached its own hits as dicts; normalise both shapes
            memory_hits = [h for h in reply.meta.get("memory_hits", []) if isinstance(h, dict)]
        reply.meta["memory_hits"] = [h["id"] if isinstance(h, dict) else h for h in memory_hits]
        reply.meta["intent"] = intent

        # learning happens after the turn so the reply can be credited for it
        self.adaptive.observe_turn(text, assistant_text=styled, assistant_traits=traits, channel=channel, engagement_seconds=engagement_seconds)
        return reply

    def _preference_echo(self, applied: dict[str, Any]) -> str:
        explicit = applied.get("explicit") or {}
        if not explicit:
            return "I did not catch a preference in that. Try: 'be more concise', 'no emoji', 'keep it technical', or use the sliders in Settings."
        from jarvis.adaptive import TRAIT_LABELS

        bits = []
        for key, value in explicit.items():
            low, high, _ = TRAIT_LABELS.get(key, (key, key, ""))
            bits.append(f"{high}" if value > 0 else f"{low}")
        return "Got it — I will lean " + ", ".join(bits) + " from now on. Say 'revert' if I get it wrong."

    def _chat(self, text: str, traits: dict[str, float]) -> Reply:
        hits = self.memory.search(text, limit=4)
        topic = BRACKET.sub("", text).strip(" ?.,!")
        if hits:
            body = "From what you have told me:\n" + "\n".join(f"• {h['body']}" for h in hits[:3])
            body += f"\n\nOn top of that: {self._reflection(topic, traits)}"
            return Reply(text=body, intent="chat", cards=[{"kind": "memory", "id": h["id"], "body": h["body"], "tags": h["tags"]} for h in hits], meta={"memory_hits": hits})
        return Reply(text=f"{self._reflection(topic, traits)}\n\nIf you want me to take this further: 'brainstorm {topic[:40]}', 'plan {topic[:40]}', or 'draft a message about it'.", intent="chat")

    @staticmethod
    def _reflection(topic: str, traits: dict[str, float]) -> str:
        if not topic:
            return "What do you want to work on?"
        opener = "Here is how I would look at it:" if traits.get("directness", 0) > 0.2 else "Happy to think this through with you."
        return (
            f"{opener} The useful question is usually not '{topic[:60]}' but what would have to be true for the version you want to be the easy one. "
            "Give me your constraint — time, money, or the thing you will not compromise on — and I can be far more specific."
        )

    def _llm_reply(self, prompt: str, llm_client: Any, *, intent: str, traits: dict[str, float]) -> Reply | None:
        directive, _ = self.adaptive.style_directive()
        context, hits = self.memory.context_block(prompt)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Jarvis, a personal assistant. Reply using this learned profile for this user:\n"
                    + directive
                    + ("\n\nKnown facts about the user:\n" + context if context else "")
                ),
            },
            {"role": "user", "content": prompt},
        ]
        try:
            text = llm_client.complete(messages)
        except Exception as exc:  # a failed remote call must degrade, not surface
            return Reply(text=f"(offline mode — the model was unreachable: {exc}. Here is the local answer instead.)\n\n{self._chat(prompt, traits).text}", intent=intent, engine="local-fallback", meta={"llm_error": str(exc)})
        if not text:
            return None
        return Reply(text=text, intent=intent, engine="llm", meta={"memory_hits": [h["id"] for h in hits]})

    # ----------------------------------------------------------------- tasks
    def add_task(self, description: str, *, due: float | None = None) -> dict[str, Any]:
        meta = {"kind": "todo", "status": "open"}
        if due:
            meta["due"] = due
        mid = self.memory.remember(f"TODO: {description}", tags=["todo", "status:open"], source="task")
        # resolved before the transaction opens: _task_conversation() writes, and
        # Database.write() is not reentrant by design (nested BEGIN is an error)
        conversation_id = self._task_conversation()
        tags = ["todo", "status:open"] + ([f"due:{int(due)}"] if due else [])
        with self.db.write() as conn:
            conn.execute("UPDATE memories SET body=?, tags=? WHERE id=?", (json.dumps({"text": description, **meta}), json.dumps(tags), mid))
            conn.execute("INSERT INTO messages(id, conversation_id, user_id, role, content, created_at, meta) VALUES(?,?,?,?,?,?,?)",
                         (f"msg_{time.time_ns()}", conversation_id, self.user_id, "system", f"task added: {description}", time.time(), json.dumps({"memory_id": mid})))
        return {"id": mid, "description": description, "due": due, "status": "open"}

    def tasks(self, *, status: str = "open") -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, body, created_at, tags FROM memories WHERE user_id=? AND deleted_at IS NULL AND tags LIKE ? ORDER BY created_at DESC",
            (self.user_id, f"%status:{status}%"),
        )
        out = []
        for r in rows:
            try:
                payload = json.loads(r["body"])
                if isinstance(payload, dict) and payload.get("kind") == "todo":
                    out.append({"id": r["id"], "description": payload.get("text", ""), "due": payload.get("due"), "status": payload.get("status", status), "created_at": r["created_at"]})
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            out.append({"id": r["id"], "description": r["body"], "due": None, "status": status, "created_at": r["created_at"]})
        return out

    def complete_task(self, task_id: str) -> bool:
        row = self.db.one("SELECT body FROM memories WHERE user_id=? AND id=?", (self.user_id, task_id))
        if row is None:
            return False
        try:
            payload = json.loads(row["body"])
        except json.JSONDecodeError:
            payload = {"kind": "todo", "text": row["body"]}
        payload["status"] = "done"
        with self.db.write() as conn:
            conn.execute("UPDATE memories SET body=?, tags=? WHERE id=?", (json.dumps(payload), json.dumps(["todo", "status:done"]), task_id))
        return True

    def _task_conversation(self) -> str:
        row = self.db.one("SELECT id FROM conversations WHERE user_id=? AND title='Tasks' LIMIT 1", (self.user_id,))
        if row:
            return row["id"]
        cid = f"conv_{time.time_ns()}"
        with self.db.write() as conn:
            conn.execute("INSERT INTO conversations(id, user_id, title, created_at, updated_at, device_id) VALUES(?,?,?,?,?,?)",
                         (cid, self.user_id, "Tasks", time.time(), time.time(), self.settings.device_id))
        return cid


class LLMClient:
    """Minimal OpenAI-compatible chat client. Returns None when offline/unconfigured."""

    def __init__(self, settings, *, http_client: Any = None):
        self.settings = settings
        self.http = http_client

    def __bool__(self) -> bool:
        creds = self.settings.provider_credentials()
        return bool(creds["llm"] or self.settings.llm_base_url)

    def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.6, max_tokens: int = 900) -> str | None:
        if not self.settings.llm_base_url:
            return None
        import httpx

        client = self.http or httpx
        try:
            resp = client.post(
                self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                json={"model": self.settings.llm_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"} if self.settings.llm_api_key else {},
                timeout=45.0,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise ConnectionError(f"llm request failed: {exc}") from exc
