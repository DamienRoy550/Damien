"""Speech detection, keyword spotting and transcription honesty."""

from __future__ import annotations

import numpy as np
import pytest
from jarvis import asr, voice

TAKE_SECONDS = 2.4


def speech(text: str, f0: float = 122.0, seconds: float = TAKE_SECONDS) -> bytes:
    return voice.synthesize_probe_pcm(text, seconds, f0)


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


# ----------------------------------------------------------------------- VAD
def test_speech_between_silence_is_found():
    lead = np.zeros(int(0.6 * 16000), dtype=np.float32)
    x = voice.decode_audio(speech("here is the actual sentence being spoken"))
    tail = np.zeros(int(0.5 * 16000), dtype=np.float32)
    segments = asr.detect_speech(np.concatenate([lead, x, tail]))
    assert len(segments) == 1, [s.as_dict() for s in segments]
    seg = segments[0]
    assert abs(seg.start_s - 0.6) < 0.25, seg.as_dict()
    assert abs(seg.end_s - (0.6 + x.size / 16000)) < 0.35, seg.as_dict()
    assert seg.duration > 1.0 and seg.rms > 0.01


def test_pure_silence_yields_no_speech():
    assert asr.detect_speech(np.zeros(16000 * 3, dtype=np.float32)) == []


def test_two_utterances_are_two_segments():
    one = voice.decode_audio(speech("first thing said out loud"))
    two = voice.decode_audio(speech("second thing said out loud"))
    gap = np.zeros(int(1.2 * 16000), dtype=np.float32)
    segments = asr.detect_speech(np.concatenate([one, gap, two, gap]))
    assert len(segments) == 2, [s.as_dict() for s in segments]
    assert segments[1].start_s > segments[0].end_s + 0.5


def test_hangover_does_not_split_a_normal_sentence():
    """Pauses inside one sentence must not chop it into pieces: the endpointer is what
    decides where a command ends, and splitting mid-phrase breaks recognition."""
    x = voice.decode_audio(speech("a sentence with natural little pauses in it", 120.0, 3.4))
    segments = asr.detect_speech(x)
    assert len(segments) == 1, [s.as_dict() for s in segments]
    assert segments[0].duration > 2.0


def test_vad_is_used_by_the_transcriber_before_any_engine_call(settings):
    """No engine installed: silence must be reported as 'no speech', not as a missing
    engine — the user needs to know they said nothing."""
    settings.asr_provider = "offline"
    transcript = asr.Transcriber(settings).transcribe(pcm(np.zeros(16000 * 2, dtype=np.float32)))
    assert transcript.available is True and transcript.engine == "vad"
    assert transcript.error == "no speech detected"


# ------------------------------------------------------------------- spotter
@pytest.fixture()
def spotter():
    """Enrolment as a user actually does it: the same words, said a few times."""
    s = asr.CommandSpotter()
    for take in ("arm the system now", "arm the system now", "arm the system now please"):
        s.enroll("arm system", speech(take))
    s.apply_calibrated_threshold()
    return s


def test_enrolment_keeps_every_take(spotter):
    """One template per phrase (the take nearest the centroid), but every take retained
    for calibration — losing them would silently tighten the bar after a restart."""
    phrases = spotter.export()
    assert [p["phrase"] for p in phrases] == ["arm system"]
    assert len(phrases[0]["takes"]) == 3
    assert all(len(t) > 10 for t in phrases[0]["takes"])
    assert len(spotter.templates) == 1


def test_enrolment_reports_a_suggested_threshold_only_when_it_can_measure(spotter):
    single = asr.CommandSpotter()
    assert single.enroll("x", speech("arm the system now"))["suggested_threshold"] is None
    assert single.enroll("x", speech("arm the system now"))["suggested_threshold"] is not None


def test_the_trained_phrase_is_recognised(spotter):
    result = spotter.recognize(speech("arm the system now"))
    assert result["matched"] == "arm system", result
    assert result["reason"] == "ok" and result["distance"] <= result["threshold"]


def test_a_different_phrase_from_the_same_voice_is_not_matched(spotter):
    for phrase in ("delete every photo I own", "open the pod bay doors", "shut the house down tonight"):
        result = spotter.recognize(speech(phrase))
        assert result["matched"] is None, (phrase, result)


def test_the_same_phrase_from_a_different_voice_is_rejected(spotter):
    """Content match alone must not grant control — the *voice* is the point of the feature."""
    for f0 in (95.0, 160.0, 210.0, 330.0):
        result = spotter.recognize(speech("arm the system now", f0))
        assert result["matched"] is None, (f0, result)


def test_noise_silence_and_blips_are_not_commands(spotter):
    rng = np.random.default_rng(5)
    assert spotter.recognize(pcm(rng.standard_normal(16000 * 2) * 0.05))["matched"] is None
    assert spotter.recognize(pcm(np.zeros(16000 * 2, dtype=np.float32)))["matched"] is None
    assert spotter.recognize(pcm(np.zeros(1600, dtype=np.float32)))["matched"] is None


def test_unenrolled_voice_cannot_trigger_anything():
    result = asr.CommandSpotter().recognize(speech("arm the system now"))
    assert result == {"matched": None, "reason": "no command templates enrolled"}


def test_calibration_can_only_tighten_never_open_the_door():
    """The historical bug: one command in the vocabulary, nothing to compare it with,
    an infinite threshold — and every sound on earth 'matched'. The ceiling is the
    guard against that class of failure."""
    s = asr.CommandSpotter()
    s.enroll("solo", speech("arm the system now"))
    s.distance_threshold = float("inf")
    assert s.apply_calibrated_threshold() <= s.max_distance
    s2 = asr.CommandSpotter()
    s2.enroll("solo", speech("arm the system now"))
    s2.enroll("solo", speech("arm the system now again"))
    s2.distance_threshold = float("inf")
    assert s2.apply_calibrated_threshold() <= s2.max_distance
    assert s2.recognize(speech("completely different words said out loud"))["matched"] is None
    # and an explicitly insane threshold is refused rather than obeyed
    s2.distance_threshold = float("inf")
    verdict = s2.recognize(speech("arm the system now"))
    assert verdict["matched"] is None and verdict["reason"] == "threshold uncalibrated"


def test_single_take_enrolment_uses_the_strict_default():
    s = asr.CommandSpotter()
    s.enroll("solo", speech("arm the system now"))
    assert s.apply_calibrated_threshold() == pytest.approx(s.single_take_threshold)
    assert s.recognize(speech("arm the system now"))["matched"] == "solo"


def test_ambiguous_two_phrase_vocabulary_is_flagged_not_guessed():
    """Two near-identical commands must produce 'no match', never a coin flip: the
    wrong command executed is worse than no command."""
    s = asr.CommandSpotter()
    for take in ("turn on the lights", "turn on the lights now"):
        s.enroll("lights on", speech(take))
        s.enroll("lights on please", speech(take))
    s.apply_calibrated_threshold()
    result = s.recognize(speech("turn on the lights now"))
    assert result["matched"] is None
    assert "ambiguous" in result["reason"], result


def test_spotter_state_survives_export_and_load(spotter):
    payload = spotter.export()
    restored = asr.CommandSpotter()
    restored.load(payload)
    assert restored.apply_calibrated_threshold() == pytest.approx(spotter.distance_threshold, abs=0.01), "calibration survives a restart"
    assert [c["phrase"] for c in restored.export()] == [c["phrase"] for c in payload]
    assert restored.recognize(speech("arm the system now"))["matched"] == "arm system"


def test_replace_flag_restarts_a_phrase():
    s = asr.CommandSpotter()
    s.enroll("p", speech("first recording of this phrase"))
    s.enroll("p", speech("second recording of this phrase"))
    assert len(s.export()[0]["takes"]) == 2
    s.enroll("p", speech("third recording of this phrase"), replace=True)
    assert len(s.export()[0]["takes"]) == 1
    assert s.suggest_threshold() is None, "one take: nothing to calibrate from"


def test_removal_takes_the_whole_phrase_with_it(spotter):
    """Whatever can be enrolled has to be forgettable, takes and all."""
    spotter.enroll("other phrase", speech("a completely separate command"))
    assert spotter.phrases() == ["arm system", "other phrase"]
    assert spotter.takes() == {"arm system": 3, "other phrase": 1}
    assert spotter.calibrated is True
    assert spotter.remove("  ARM SYSTEM ") is True
    assert spotter.phrases() == ["other phrase"]
    assert spotter.export()[0]["phrase"] == "other phrase", "templates rebuilt, not just labels removed"
    assert spotter.recognize(speech("arm the system now"))["matched"] is None
    assert spotter.calibrated is False, "one single-take phrase left: nothing to calibrate from"
    assert spotter.remove("never enrolled") is False


def test_unrelated_phrases_do_not_collide():
    s = asr.CommandSpotter()
    for phrase, words in (("lights on", "turn on the lights"), ("garage", "open the garage door"), ("alarm", "set the alarm for seven")):
        for take in (words, words + " now"):
            s.enroll(phrase, speech(take))
    s.apply_calibrated_threshold()
    for phrase, words in (("lights on", "turn on the lights"), ("garage", "open the garage door"), ("alarm", "set the alarm for seven")):
        assert s.recognize(speech(words))["matched"] == phrase, words


# -------------------------------------------------------------- transcriber
def test_transcriber_reports_unavailable_instead_of_guessing(settings):
    """No local model installed → say so, and say what to install. A fabricated
    transcript is worse than none: the user would act on words nobody said."""
    settings.asr_provider = "offline"
    transcript = asr.Transcriber(settings).transcribe(speech("some words nobody will transcribe"))
    assert transcript.available is False
    assert transcript.engine == "unavailable"
    assert transcript.text is None or transcript.text == ""
    payload = transcript.as_dict()
    assert "whisper" in payload["error"], "the user is told how to get dictation"
    assert "voiceprint" in payload["error"].lower(), "and that the rest of the product still works"


def test_unreachable_remote_endpoint_degrades(settings):
    """A configured remote ASR that is down must return a typed error, not raise or hang."""
    settings.asr_provider = "whisper-http"
    settings.asr_endpoint = "http://127.0.0.1:1"
    transcript = asr.Transcriber(settings).transcribe(speech("remote engine is down"))
    assert transcript.available is False
    assert transcript.engine == "whisper-http(remote)"
    assert transcript.error


def test_transcribe_rejects_undecodable_audio(settings):
    transcript = asr.Transcriber(settings).transcribe(b"")
    assert transcript.available is False and "decode" in transcript.error


def test_dtw_cost_is_symmetric_finite_and_discriminative():
    a = asr._content_frames(speech("open the garage door"))
    b = asr._content_frames(speech("open the garage door slowly"))
    c = asr._content_frames(speech("delete the whole archive please"))
    assert asr.dtw_cost(a, a) == pytest.approx(0.0, abs=1e-6)
    same, diff = asr.dtw_cost(a, b), asr.dtw_cost(a, c)
    assert np.isfinite(same) and np.isfinite(diff), "length differences must cost, not explode"
    assert same < diff
    assert diff == pytest.approx(asr.dtw_cost(c, a), rel=1e-6), "cost must not depend on argument order"


def test_dtw_handles_degenerate_input():
    empty = np.zeros((0, 13), dtype=np.float32)
    assert asr.dtw_cost(empty, empty) == float("inf")
    one = np.zeros((1, 13), dtype=np.float32)
    assert np.isfinite(asr.dtw_cost(one, one))
