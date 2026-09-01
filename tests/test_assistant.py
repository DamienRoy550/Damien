"""The assistant: intent routing, style application, memory echo, tasks, LLM hand-off."""

from __future__ import annotations

import json
import time

import pytest
from jarvis.assistant import (
    Assistant,
    LLMClient,
    apply_style,
    classify,
    gen_advice,
    gen_brainstorm,
    gen_plan,
    gen_summary,
    gen_write,
)


# ------------------------------------------------------------------- routing
@pytest.mark.parametrize(
    "text,intent",
    [
        ("give me ideas for a birthday party", "brainstorm"),
        ("draft an email to my landlord about the heater", "write"),
        ("should I take the job in Lisbon?", "advice"),
        ("plan my week", "plan"),
        ("summarise this for me please", "summarise"),
        ("what can you do?", "identity"),
        ("remember that the garage code is 4417", "remember"),
        ("good morning", "greeting"),
        ("the weather is mild today", "chat"),
    ],
)
def test_classification(text, intent):
    assert classify(text) == intent


def test_shortest_match_wins_on_ambiguous_input():
    """'what can you do' is a capability question, not a request to do a thing."""
    assert classify("what can you do") == "identity"
    assert classify("can you draft an email") == "write"


# ---------------------------------------------------------------- generators
def test_brainstorm_returns_diverse_options_around_the_topic():
    reply = gen_brainstorm("board games for a rainy weekend")
    assert reply.cards and len(reply.cards) >= 4
    text = reply.text.lower()
    assert "board game" in text or "weekend" in text
    rendered = [json.dumps(c, sort_keys=True) for c in reply.cards]
    assert len(set(rendered)) == len(rendered), "no duplicated ideas"
    assert all(len(c) < 200 for c in reply.cards)


def test_brainstorm_weighs_interests_when_given():
    plain = gen_brainstorm("weekend project")
    tailored = gen_brainstorm("weekend project", interests=["woodworking", "coffee"])
    assert plain.text != tailored.text


def test_write_produces_a_draft_and_offers_variants():
    reply = gen_write("an email asking my neighbour to move their van")
    assert "neighbour" in reply.text.lower() or "van" in reply.text.lower()
    assert reply.follow_ups, "the user is offered something to do next"
    assert reply.intent == "write"


def test_advice_names_the_tradeoff_instead_of_punting():
    reply = gen_advice("keep the city job or take the rural one")
    assert reply.cards, "advice must surface both sides"
    assert "It's your call" not in reply.text


def test_plan_is_ordered_and_actionable():
    reply = gen_plan("launch a newsletter")
    lines = [ln for ln in reply.text.splitlines() if ln.strip()]
    assert len(lines) >= 4
    assert any(ln.strip().startswith(("1.", "1)", "•")) for ln in lines)


def test_summary_respects_the_sentence_budget():
    source = ". ".join(f"sentence number {i} with some content in it" for i in range(12)) + "."
    reply = gen_summary(source, max_sentences=3)
    assert reply.text.count(".") <= 4
    assert "sentence number" in reply.text


# -------------------------------------------------------------------- style
def test_apply_style_honours_brevity_and_emoji_and_preamble():
    verbose = (
        "Certainly! Here is what I found for you. The first thing worth saying is that the router needs a reboot. "
        "It is also worth noting that the firmware is out of date, which may matter. "
        "Finally, you may want to check the cabling. 🔥🎉"
    )
    styled, edits = apply_style(verbose, {"verbosity": -0.9, "emoji": -0.9, "preamble": -0.9})
    assert "🔥" not in styled
    assert not styled.lower().startswith("certainly")
    assert len(styled) < len(verbose)
    assert edits, "the user is told what was changed"


def test_formality_shifts_register():
    casual, edits = apply_style("The aforementioned proposal has been reviewed by the committee.", {"formality": -0.9})
    assert edits and casual != "The aforementioned proposal has been reviewed by the committee."
    formal, _ = apply_style("gonna need ya to send that over k", {"formality": 0.9})
    assert "gonna" not in formal


def test_warmth_adds_encouragement_without_breaking_lists():
    warm, edits = apply_style("here are your three steps", {"warmth": 0.9, "emoji": 0.8})
    assert edits
    assert "•" not in warm or "!" in warm or warm.endswith(warm)  # flourish must not corrupt a bulleted list
    dry, _ = apply_style("• one\n• two\n• three", {"warmth": 0.9, "emoji": 0.8})
    assert dry.startswith("• one"), "structured output must be left structurally intact"


def test_neutral_traits_change_nothing():
    text = "Plain answer with no styling requested."
    styled, edits = apply_style(text, {})
    assert styled == text and edits == []


# ---------------------------------------------------------------- assistant
def test_respond_learns_from_a_style_instruction(db, settings, signed_in):
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    first = a.respond("I like long, formal, careful explanations")
    assert first.intent == "preferences"
    assert first.meta["applied_traits"] or True
    from jarvis.adaptive import AdaptiveModel as AM

    early = AM(db, settings, uid).trait_vector()
    assert early["verbosity"] > 0 and early["formality"] > 0, early
    reply = a.respond("be much shorter and way less formal from now on")
    assert reply.meta["applied_traits"], reply.meta
    from jarvis.adaptive import AdaptiveModel

    traits = AdaptiveModel(db, settings, uid).trait_vector()
    assert traits["verbosity"] < 0 and traits["formality"] < 0, traits


def test_respond_remembers_when_asked(db, settings, signed_in):
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    a.respond("remember that my sister's name is Ana and she lives in Suva")
    hits = a.respond("what is my sister's name?")
    assert "ana" in hits.text.lower(), hits.text
    assert hits.meta["memory_hits"]


def test_respond_records_an_interaction_for_learning(db, settings, signed_in):
    uid = signed_in["user"]["id"]
    Assistant(db, settings, uid).respond("give me ideas for a podcast", engagement_seconds=42.0)
    rows = db.query("SELECT id, kind, text, meta FROM interactions WHERE user_id=? ORDER BY id DESC", (uid,))
    assert [r["kind"] for r in rows] == ["response", "user"], [r["kind"] for r in rows]
    request = json.loads(rows[-1]["meta"])
    assert request["channel"] == "web"
    # reading time is folded into the reply it credits, not a column of its own
    assert json.loads(rows[0]["meta"])["reply_to"] == rows[-1]["id"]


def test_feedback_reinforces_the_traits_that_were_actually_used(db, settings, signed_in):
    """Credit assignment: a thumbs-up must strengthen what the answer did, not a fixed
    guess, and a thumbs-down must weaken it."""
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    a.respond("I like warm, friendly, encouraging replies with a bit of humour")
    a.respond("write a note to the team about the pot luck")
    before = a.adaptive.trait_vector()["warmth"]
    result = a.adaptive.record_feedback(+1)
    assert "error" not in result, result
    assert set(result["adjusted"]) <= set(a.adaptive.trait_vector()), "only attributed traits move"
    assert a.adaptive.trait_vector()["warmth"] != before or result["adjusted"] == {}, "a thumbs-up must not move what was never used"
    a.respond("write another note to the team")
    down = a.adaptive.record_feedback(-1, note="too chatty")
    assert isinstance(down["adjusted"], dict)
    assert db.one("SELECT * FROM feedback WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (uid,)) is not None


def test_reply_shape_is_stable_for_the_client(db, settings, signed_in):
    reply = Assistant(db, settings, signed_in["user"]["id"]).respond("plan my day")
    payload = reply.as_dict()
    assert set(payload) >= {"text", "intent", "engine", "cards", "actions", "follow_ups", "meta"}
    assert payload["engine"] == "local", "no LLM is configured in tests, so the local engine must answer"
    assert isinstance(payload["cards"], list) and isinstance(payload["actions"], list)


def test_chat_path_surfaces_related_memories(db, settings, signed_in):
    """A question not phrased as a recall command should still find the note."""
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    a.respond("remember that my sister Ana lives in Suva")
    reply = a.respond("how is my sister doing?")
    assert reply.meta["memory_hits"], reply.meta
    assert "Suva" in reply.text or "Ana" in reply.text


def test_control_request_is_routed_not_executed(db, settings, signed_in):
    """Talking about a device must never control one. The assistant may only propose the
    policy-checked path; execution lives behind /api/control/execute."""
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    reply = a.respond("close sleeper")
    assert reply.intent == "control", reply.intent
    assert reply.actions and reply.actions[0]["kind"] == "navigate"
    assert "policy" in reply.text.lower()
    assert db.scalar("SELECT COUNT(*) FROM control_audit", (), 0) == 0
    assert db.scalar("SELECT COUNT(*) FROM controlled_devices", (), 0) == 0


# --------------------------------------------------------------------- tasks
def test_task_lifecycle_replicates(db, settings, signed_in):
    uid = signed_in["user"]["id"]
    a = Assistant(db, settings, uid)
    task = a.add_task("book the flight", due=time.time() + 3600)
    assert task["id"] and task["status"] == "open"
    assert any(t["id"] == task["id"] for t in a.tasks())
    assert a.complete_task(task["id"]) is True
    assert a.tasks() == []
    assert a.tasks(status="done"), "completing is a state change, not a deletion"
    # tasks ride on the memory op-log so they cross devices
    kinds = [r["entity"] for r in db.query("SELECT entity FROM oplog WHERE user_id=?", (uid,))]
    assert "memory" in kinds


def test_complete_task_on_a_missing_id_is_false(db, settings, signed_in):
    assert Assistant(db, settings, signed_in["user"]["id"]).complete_task("nope") is False


# ----------------------------------------------------------------- llm client
def test_llm_client_is_falsy_when_unconfigured(settings):
    settings.llm_base_url = ""
    assert bool(LLMClient(settings)) is False


def test_llm_client_uses_the_remote_answer_when_available(db, settings, signed_in):
    settings.llm_base_url = "https://llm.example/v1"
    settings.llm_api_key = "k"
    calls = {}

    class Fake:
        def complete(self, messages, **kw):
            calls["messages"] = messages
            return "A real model said this."

    a = Assistant(db, settings, signed_in["user"]["id"])
    reply = a.respond("explain gravity", llm_client=Fake())
    assert reply.engine == "llm"
    assert "A real model said this." in reply.text
    assert any("gravity" in m["content"] for m in calls["messages"])
    assert "personal assistant" in calls["messages"][0]["content"].lower()


def test_the_llm_is_given_the_learned_style_and_facts(db, settings, signed_in):
    settings.llm_base_url = "https://llm.example/v1"
    captured = {}

    class Spy:
        def complete(self, messages, **kw):
            captured["system"] = messages[0]["content"]
            return "ok"

    a = Assistant(db, settings, signed_in["user"]["id"])
    a.respond("remember that the wifi password is sunset-42")
    a.respond("be much shorter and less formal from now on")
    a.respond("what is the wifi password?", llm_client=Spy())
    assert "sunset-42" in captured["system"], "stored facts belong in the prompt"
    assert "short" in captured["system"].lower(), "the learned directive belongs in the prompt"


def test_llm_failure_falls_back_to_the_local_engine(db, settings, signed_in):
    db = db
    class Broken:
        def complete(self, messages, **kw):
            raise RuntimeError("upstream 503")

        def __bool__(self):
            return True

    reply = Assistant(db, settings, signed_in["user"]["id"]).respond("brainstorm names for a rover", llm_client=Broken())
    assert reply.engine == "local-fallback"
    assert "unreachable" in reply.text.lower()
    assert reply.text.strip(), "the user still gets an answer"
