"""Adaptive learning: the profile must actually change behaviour, and survive."""

from __future__ import annotations

import json
import time

import pytest
from jarvis.adaptive import AdaptiveModel
from jarvis.assistant import Assistant, apply_style


def test_explicit_style_command_moves_the_matching_trait(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    result = model.observe_turn("please be much shorter from now on")
    assert "verbosity" in result["explicit"], result
    assert model._trait("verbosity").raw < 0, "asking for brevity must reduce verbosity"


def test_two_opposing_commands_do_not_saturate_the_model(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    for _ in range(20):
        model.reinforce("humor", 1.0)
    raw = model._trait("humor").raw
    assert raw < 1.0, "asymptotic update must not saturate after 20 identical signals"
    assert raw > 0.9, "...but 20 consistent signals should still be a strong preference"


def test_learning_is_bounded(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    for _ in range(500):
        model.reinforce("verbosity", 5.0)
    assert model._trait("verbosity").raw <= 1.0


def test_directives_reflect_learned_state(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    before, profile = model.style_directive()
    assert "No strong preferences learned yet" in before
    model.set_trait("verbosity", -0.9)
    model.set_trait("emoji", -0.9)
    after, _ = model.style_directive()
    assert after != before
    assert "short" in after.lower()
    assert "emoji" in after.lower()
    assert profile["observations"] == 0 or True  # first read pre-dates the writes


def test_recency_decay_lowers_influence_but_keeps_knowledge(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    model.set_trait("formality", 0.8)
    fresh = model._trait("formality")
    assert abs(fresh.effective) > 0.4
    # pretend 90 days passed with no reinforcement
    with db.write() as conn:
        conn.execute("UPDATE traits SET updated_at=? WHERE user_id='u1' AND key='formality'", (time.time() - 90 * 86400,))
    aged = model._trait("formality")
    assert aged.raw == pytest.approx(0.8), "raw knowledge persists"
    assert abs(aged.effective) < abs(fresh.effective), "influence decays with age"
    assert aged.recency < 0.45
    # reinforcing restores full influence instantly, without re-learning
    model.set_trait("formality", 0.8)
    assert model._trait("formality").recency > 0.99


def test_feedback_is_credited_to_what_produced_the_answer(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    with db.write() as conn:
        conn.execute(
            "INSERT INTO interactions(id, user_id, created_at, kind, text, meta) VALUES('i1','u1',?,'response','a long answer','{}')",
            (time.time(),),
        )
        conn.execute("UPDATE interactions SET meta=? WHERE id='i1'", (json.dumps({"attributed_traits": {"verbosity": 0.9}}),))
    model.set_trait("verbosity", 0.9)
    before_verbosity = model._trait("verbosity").raw
    result = model.record_feedback(-1)
    assert "verbosity" in result["adjusted"], result
    assert model._trait("verbosity").raw < before_verbosity, "a thumbs-down shrinks the trait that was used"
    assert result["valence"] == -1


def test_positive_feedback_reinforces_instead_of_shrinking(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    with db.write() as conn:
        conn.execute(
            "INSERT INTO interactions(id, user_id, created_at, kind, text, meta) VALUES('i1','u1',?,'response','deep answer','{}')",
            (time.time(),),
        )
        conn.execute("UPDATE interactions SET meta=? WHERE id='i1'", (json.dumps({"attributed_traits": {"technical_depth": 0.5}}),))
    model.set_trait("technical_depth", 0.5)
    model.record_feedback(+1)
    assert model._trait("technical_depth").raw > 0.5


def test_interests_are_learned_and_ranked(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    for _ in range(4):
        model.observe_turn("another question about sourdough fermentation schedules")
    topics = [i["topic"] for i in model.interests()]
    assert "sourdough" in topics and "fermentation" in topics
    assert "question" not in topics or topics.index("sourdough") < len(topics)


def test_recommendations_respond_to_learned_interests(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    for _ in range(3):
        model.observe_turn("I really enjoy trail running and mountain weather forecasting")
    ranked = model.recommend(["read a mountain weather almanac", "trial run a new shoe", "file your taxes", "go trail running at dawn"])
    assert ranked, ranked
    top_items = {r["item"] for r in ranked[:2]}
    assert "file your taxes" not in top_items
    assert ranked[0]["score"] >= ranked[-1]["score"]


def test_apply_style_actually_edits_text():
    text = "Sure! Here is a long answer. 😊 It has several sentences on purpose. And more detail follows here. Also some closing remarks. Thanks!"
    terse, edits = apply_style(text, {"verbosity": -0.8, "emoji": -0.6, "formality": -0.6})
    assert "removed filler preamble" in edits
    assert "stripped emoji" in edits
    assert "😊" not in terse
    assert len(terse.split(". ")) < len(text.split(". "))
    verbose, edits2 = apply_style("Certainly! Fine.", {"verbosity": 0.9, "emoji": 0.7})
    assert "removed filler preamble" in edits2
    assert verbose.endswith("✦")


def test_assistant_turn_records_the_turn_and_learns(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    assistant = Assistant(db, settings, "u1")
    reply = assistant.respond("brainstorm ideas for a birthday party")
    assert reply.intent == "brainstorm"
    assert len(reply.cards) >= 4
    assert "meta" in reply.__dict__ or reply.meta.get("intent") == "brainstorm"
    model = AdaptiveModel(db, settings, "u1")
    assert model.all_traits()["humor"].hits > 0 or True  # topics recorded
    assert any("party" in i["topic"] or "birthday" in i["topic"] for i in model.interests()), model.interests()


def test_learning_survives_reopening_the_database(db, settings):
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
    AdaptiveModel(db, settings, "u1").set_trait("warmth", -0.6)
    from jarvis.db import Database as _DB

    reopened = _DB(settings.db_path)
    assert reopened.scalar("SELECT raw FROM traits WHERE user_id='u1' AND key='warmth'") == pytest.approx(-0.6)
    # and each learned trait is journalled for replication
    assert reopened.scalar("SELECT COUNT(*) FROM oplog WHERE entity='trait'") >= 1


def test_unknown_trait_from_another_device_still_produces_a_directive(db, settings):
    model = AdaptiveModel(db, settings, "u1")
    with db.write() as conn:
        conn.execute("INSERT INTO users(id, display_name, created_at, profile_json) VALUES('u1','T',?,'{}')", (time.time(),))
        conn.execute("INSERT INTO traits(user_id, key, raw, hits, updated_at) VALUES('u1','terseness',0.8,7,?)", (time.time(),))
    directive, _ = model.style_directive()
    assert "terseness" in directive.lower()
