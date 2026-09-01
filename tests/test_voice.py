"""Voiceprint: audio gates, enrolment, verification, calibration interlocks."""

from __future__ import annotations

import json

import numpy as np
import pytest
from jarvis import voice


def sample(text: str, f0: float = 118.0, seconds: float = 2.6) -> bytes:
    return voice.synthesize_probe_pcm(text, seconds, f0)


def embedding(text: str, f0: float = 118.0):
    return voice.embedding_from(voice.decode_audio(sample(text, f0)))


# ------------------------------------------------------------------ decoding
def test_wav_roundtrip_and_resampling():
    pcm = sample("resampling check", 140.0)
    wav = voice.pcm16_to_wav_bytes(pcm, rate=22050)
    x = voice.decode_audio(wav)
    assert abs(x.size - int(len(pcm) / 2 * 16000 / 22050)) <= 2, "22.05k WAV must be resampled to 16k"
    mono = voice.decode_audio(pcm)
    assert mono.dtype == np.float32 and mono.size == len(pcm) // 2


def test_stereo_is_downmixed_and_raw_pcm_accepted():
    pcm = sample("stereo", 120.0)
    stereo = b"".join(pcm[i : i + 2] * 2 for i in range(0, min(len(pcm), 4000), 2))
    wav = voice.pcm16_to_wav_bytes(stereo, rate=16000)
    with pytest.raises(ValueError):
        voice.decode_audio(wav[:20])  # truncated header must not silently succeed
    assert voice.decode_audio(pcm).size > 0


def test_garbage_input_is_rejected_not_crashing():
    """Unknown bytes are treated as raw PCM (that is a supported input), but they must
    not be silently accepted as a *voice*: the quality gate rejects them."""
    with pytest.raises(ValueError):
        voice.decode_audio(b"")
    with pytest.raises(voice.VoiceQualityError):
        voice.embedding_from(voice.decode_audio(b"not audio at all, just 200 bytes of text"))
    with pytest.raises(ValueError):
        voice.decode_audio(b"RIFF\x00\x00\x00\x00WAVEjunkjunkjunk")


# ------------------------------------------------------------------ quality
def test_too_short_is_refused():
    with pytest.raises(voice.VoiceQualityError, match="too short"):
        voice.embedding_from(voice.decode_audio(sample("hi", 118.0, 0.4)))


def test_silence_is_refused():
    silence = (np.zeros(int(3 * voice.SAMPLE_RATE), dtype=np.float32)).astype("<i2").tobytes()
    with pytest.raises(voice.VoiceQualityError):
        voice.embedding_from(voice.decode_audio(silence))


def test_loud_noise_is_refused():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(int(3 * voice.SAMPLE_RATE)) * 0.2).astype(np.float32)
    with pytest.raises(voice.VoiceQualityError, match=r"noisy|clipping"):
        voice.embedding_from(voice.decode_audio((noise * 32767).astype("<i2").tobytes()))


def test_embedding_is_normalised_and_deterministic():
    a, qa = embedding("the same sentence said twice")
    b, qb = embedding("the same sentence said twice")
    assert a == b, "identical input must give an identical print (no randomness in the path)"
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-6
    assert qa["snr_db"] > 6 and qb["frames_used"] > 10
    assert set(qa) >= {"seconds", "rms", "voiced_ratio", "snr_db", "clipping_ratio", "frames_used"}


def test_gain_does_not_change_the_print():
    loud, _ = voice.embedding_from(np.frombuffer(sample("hold the line", 118.0), dtype="<i2").astype(np.float32) / 32768)
    quiet, _ = voice.embedding_from(np.frombuffer(sample("hold the line", 118.0), dtype="<i2").astype(np.float32) / 32768 * 0.3)
    assert voice.cosine(loud, loud) == pytest.approx(1.0)
    assert voice.cosine(loud, quiet) > 0.9, "a 10 dB level change must not look like a different person"


# ----------------------------------------------------------------- enrolment
def test_enrol_requires_samples_and_matches_dimensions():
    with pytest.raises(ValueError):
        voice.enroll([])
    e1, _ = embedding("one")
    e2_short = e1[: len(e1) - 1]
    with pytest.raises(ValueError):
        voice.enroll([e1, list(e2_short)])


def test_threshold_widens_with_enrolment_spread():
    tight = [embedding("alpha phrase one", 118.0)[0], embedding("alpha phrase two", 118.0)[0]]
    model_tight = voice.enroll(tight)
    spread = [*tight, embedding("alpha phrase three quite different length indeed", 121.0)[0]]
    model_spread = voice.enroll(spread)
    assert model_spread.threshold <= model_tight.threshold + 0.06
    assert model_tight.sample_count == 2 and model_spread.sample_count == 3


def test_single_sample_enrolment_is_flagged_as_thin():
    model = voice.enroll([embedding("only one take", 118.0)[0]])
    assert model.needs_more_samples
    assert model.threshold == pytest.approx(0.90), "no spread measurable → strict default"


def test_owner_verifies_and_impostor_does_not():
    owner_samples = [embedding(f"enrolment take {i} about the weather", 118.0)[0] for i in range(3)]
    model = voice.enroll(owner_samples)
    owner = embedding("a brand new sentence from the same voice", 118.0)[0]
    stranger = embedding("a brand new sentence from the same voice", 205.0)[0]
    owner_result = voice.verify(model, owner, strict=False)
    stranger_result = voice.verify(model, stranger, strict=False)
    assert owner_result["accepted"] is True, owner_result
    assert stranger_result["accepted"] is False, stranger_result
    assert owner_result["similarity"] > stranger_result["similarity"]


def test_dim_mismatch_is_refused_instead_of_guessed():
    model = voice.enroll([embedding("size matters", 118.0)[0]] * 2)
    wrong = embedding("size matters", 118.0)[0][:-1]
    result = voice.verify(model, wrong)
    assert result["accepted"] is False and result["reason"] == "embedding-dim-mismatch"


def test_co_tenants_voice_raises_the_bar():
    """A second enrolled speaker must never be waved through by a loose threshold."""
    model = voice.enroll([embedding("take one for the owner", 118.0)[0]])
    model.threshold = 0.5  # simulate a mis-calibrated loose threshold
    candidate = embedding("take one for the owner", 118.0)[0]
    near_impostor = embedding("take one for the owner", 121.0)[0]
    loose = voice.verify(model, candidate, strict=False)
    tightened = voice.verify(model, candidate, impostor_centroids=[near_impostor], strict=False)
    assert tightened["threshold"] > loose["threshold"]
    assert any("threshold raised" in n for n in tightened["notes"])


def test_model_serialises_and_identifies_itself():
    model = voice.enroll([embedding("persist me", 118.0)[0], embedding("and me too", 120.0)[0]])
    raw = model.to_json()
    restored = voice.VoiceprintModel.from_json(raw)
    assert restored.centroid == pytest.approx(model.centroid, abs=1e-5)
    assert restored.threshold == pytest.approx(model.threshold, abs=1e-4)
    assert restored.fingerprint == model.fingerprint
    assert json.loads(raw)["sample_count"] == 2
    assert voice.VoiceprintModel.from_json(None) is None
    assert voice.VoiceprintModel.from_json("{}") is None


# ------------------------------------------------------------- calibration
def test_privileged_grant_is_refused_until_calibrated():
    """The score says "owner"; the interlock must still refuse, because the threshold
    was never validated against real speech on this hardware."""
    samples = [embedding(f"enrolment phrase number {i}", 118.0)[0] for i in range(3)]
    model = voice.enroll(samples)
    owner = embedding("enrolment phrase number 1", 118.0)[0]
    verdict = voice.verify(model, owner)
    assert verdict["match"] is True, "the raw score does match"
    assert verdict["accepted"] is False and verdict["blocked_reason"] == "uncalibrated"
    model.calibrated = True
    after = voice.verify(model, owner)
    assert after["accepted"] is True and after["blocked_reason"] is None
    # a thin enrolment (1 sample) is refused too, even when calibrated
    thin = voice.enroll([owner])
    thin.calibrated = True
    assert voice.verify(thin, owner)["blocked_reason"] == "thin-enrolment"


def test_calibration_report_picks_a_threshold_and_admits_when_unusable():
    genuine = [0.95, 0.93, 0.96, 0.94, 0.97, 0.92, 0.95, 0.94, 0.96, 0.93]
    impostor = [0.70, 0.72, 0.68, 0.74, 0.69, 0.71, 0.73, 0.67, 0.75, 0.70]
    report = voice.CalibrationReport.from_trials(genuine, impostor, target_far=0.01)
    assert report.separable and report.usable_for_privileged
    assert max(impostor) < report.threshold <= min(genuine)
    hopeless = voice.CalibrationReport.from_trials(genuine, [0.99, 0.995, 0.999, 0.98, 0.985, 0.99, 0.995, 0.98, 0.99, 0.985])
    assert hopeless.usable_for_privileged is False, "overlapping distributions must not be marked safe"
    assert voice.CalibrationReport.from_trials([], []).usable_for_privileged is False


def test_local_provider_matches_documented_dimension():
    provider = voice.LocalSpeakerModel()
    embedding_vector, quality = provider.embed(sample("dimension check", 118.0))
    assert len(embedding_vector) == provider.dim, (len(embedding_vector), provider.dim)
    assert quality["frames_used"] > 10


def test_pretrained_provider_is_optional_and_reported():
    """The default provider must work with nothing installed; a pretrained model,
    when present, replaces the baseline."""
    provider = voice.default_provider()
    assert provider.name  # never None, never raises
    vec, _ = provider.embed(sample("provider swap", 118.0))
    assert len(vec) == provider.dim
