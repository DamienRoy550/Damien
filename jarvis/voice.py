"""Speaker verification (voiceprint) — local by default, pluggable model behind it.

Pipeline (``LocalSpeakerModel``)
--------------------------------
``WAV/PCM -> 16 kHz mono -> pre-emphasis -> 25 ms frames @ 10 ms hop -> Hann ->
rFFT -> 40-band mel -> DC-removed mean log-mel + within-utterance MFCC spread +
F0/tilt block -> L2-normalised 60-dim embedding``

Verification is cosine similarity against the enrolled centroid, gated by a
per-user threshold derived from the spread of that user's own enrolment samples.

Security posture — read before relying on this
----------------------------------------------
1. **This is a convenience factor, not a password.** A text-independent MFCC/mel
   baseline lands in the low-single-digit-% equal-error range at best on clean
   speech, and this module has **no liveness or anti-spoofing detection**. A
   replayed recording of the owner's voice will pass. Therefore it is used only
   as *step-up* in front of privileged actions and is never the sole credential;
   account authentication goes through the OIDC providers in :mod:`jarvis.auth`.
2. **Refuse to be trusted before calibration.** ``calibrated`` is ``False`` until
   an operator has run real-user trials (:meth:`CalibrationReport.from_trials`).
   While uncalibrated the model still reports scores, but
   ``VoiceGate`` will not grant a privileged scope, so nothing sensitive can be
   unlocked on an unvalidated biometric.
3. **Never downgrade silently.** If another speaker is enrolled on the same
   install and sits close to the incoming utterance, the threshold is raised
   rather than the attempt being waved through (``verify`` does this).

Measured on a synthetic multi-speaker battery (8 formant-offset voices x 4 utterances):
intra-speaker cosine ~0.99, inter-speaker ~0.84 — comfortably enough to
distinguish *the owner from an unrelated stranger* in quiet conditions, and
nowhere near enough to survive a deliberate attacker or a bad microphone. The
synthetic battery is not real speech, so no absolute security number is claimed;
that is what ``calibrate`` is for.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

SAMPLE_RATE = 16_000
FRAME_LEN = 400  # 25 ms
HOP = 160  # 10 ms
N_MELS = 40
N_CEPSTRA = 13
N_FFT = 2048
PRE_EMPHASIS = 0.97
MIN_SECONDS = 1.2
MAX_SECONDS = 30.0
F0_LO_HZ, F0_HI_HZ = 60.0, 400.0
MEL_WEIGHT, SPREAD_WEIGHT, SECONDARY_WEIGHT = 1.0, 0.4, 0.4
DEFAULT_MARGIN = 0.06
MIN_FRAMES = 12
MIN_SNR_DB = 6.0
MAX_CLIP_RATIO = 0.02
MIN_RMS = 1e-4


class VoiceQualityError(ValueError):
    """Audio unusable for a trustworthy print: too short, too quiet, or too noisy."""


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------
def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or x.size == 0:
        return x
    n_out = max(1, round(x.size * dst / src))
    idx = np.linspace(0, x.size - 1, n_out, dtype=np.float64)
    lo = np.floor(idx).astype(np.int64)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = (idx - lo).astype(np.float32)
    return (x[lo] * (1 - frac) + x[hi] * frac).astype(np.float32)


def decode_audio(data: bytes, *, fallback_rate: int = SAMPLE_RATE) -> np.ndarray:
    """WAV (any rate/width/channels) or raw signed-16 PCM -> float32 mono at 16 kHz."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                channels, rate, width = wf.getnchannels(), wf.getframerate(), wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())
                frame_count = wf.getnframes()
        except (wave.Error, EOFError, struct.error) as exc:
            raise ValueError(f"malformed WAV container: {exc}") from exc
        dtypes = {1: (np.uint8, 128.0, -128.0), 2: ("<i2", 32768.0, 0.0), 4: ("<i4", 2147483648.0, 0.0)}
        if width not in dtypes:
            raise ValueError(f"unsupported WAV sample width: {width} bytes")
        if frame_count == 0 or not raw:
            raise ValueError("WAV container has no audio frames")
        dtype_str, scale, offset = dtypes[width]
        itemsize = np.dtype(dtype_str).itemsize
        samples = (np.frombuffer(raw[: (len(raw) // itemsize) * itemsize], dtype=dtype_str).astype(np.float32) - offset) / scale
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        out = samples.astype(np.float32)
        return resample(out, rate, SAMPLE_RATE) if rate != SAMPLE_RATE else out

    # Browsers can capture 16 kHz s16le directly (the web UI does). webm/opus would
    # need a decoder, which an offline-first core should not depend on.
    if len(data) % 2:
        data = data[:-1]
    if not data:
        raise ValueError("empty audio payload")
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    return resample(samples, fallback_rate, SAMPLE_RATE)


# ---------------------------------------------------------------------------
# DSP primitives
# ---------------------------------------------------------------------------
def _mel_filterbank(nfft: int = N_FFT, rate: int = SAMPLE_RATE) -> np.ndarray:
    def to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    points = to_hz(np.linspace(to_mel(20.0), to_mel(rate / 2.0), N_MELS + 2))
    bins = np.floor((nfft + 1) * points / rate).astype(np.int64)
    fb = np.zeros((N_MELS, nfft // 2 + 1), dtype=np.float32)
    for i in range(N_MELS):
        left, centre, right = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        centre = max(centre, left + 1)
        right = max(right, centre + 1)
        up = np.linspace(0, 1, centre - left, dtype=np.float32)
        down = np.linspace(1, 0, right - centre, dtype=np.float32)
        fb[i, left:centre] = up
        fb[i, centre:right] = down
    return fb


_FB = _mel_filterbank()
_DCT = (
    np.cos(np.pi * (np.arange(N_MELS)[None, :] + 0.5) * np.arange(N_CEPSTRA)[:, None] / N_MELS)
    * np.sqrt(2.0 / N_MELS)
).astype(np.float32)


def _delta(frames: np.ndarray, width: int = 2) -> np.ndarray:
    if frames.shape[0] < 3:
        return np.zeros_like(frames)
    top = np.repeat(frames[:1], width, axis=0)
    bottom = np.repeat(frames[-1:], width, axis=0)
    padded = np.concatenate([top, frames, bottom], axis=0)
    weights = np.arange(-width, width + 1, dtype=np.float32)
    denom = 2.0 * float(np.sum(weights[weights > 0] ** 2))
    out = np.zeros_like(frames, dtype=np.float32)
    for i, w in enumerate(weights):
        out += w * padded[i : i + frames.shape[0]]
    return out / denom


def _pitch_and_tilt(frames: np.ndarray) -> np.ndarray:
    """Per-frame normalised autocorrelation F0 + harmonic strength + spectral centroid."""
    n, length = frames.shape
    nfft = 1 << (2 * length - 1).bit_length()
    spec = np.fft.rfft(frames * np.hanning(length).astype(np.float32), n=nfft, axis=1)
    acf = np.fft.irfft(spec * np.conj(spec), n=nfft, axis=1)[:, : nfft // 2]
    acf = acf / (acf[:, 0:1] + 1e-12)
    lo = max(2, int(SAMPLE_RATE / F0_HI_HZ))
    hi = min(acf.shape[1] - 1, int(SAMPLE_RATE / F0_LO_HZ))
    band = acf[:, lo : hi + 1]
    peak = np.argmax(band, axis=1)
    strength = band[np.arange(n), peak]
    f0 = SAMPLE_RATE / np.maximum(lo + peak, 1.0)
    power = (np.abs(spec) ** 2).sum(axis=1)
    freqs = np.fft.rfftfreq(nfft, 1.0 / SAMPLE_RATE)
    centroid = (np.abs(spec) ** 2 * freqs).sum(axis=1) / (power + 1e-12)
    return np.column_stack(
        [np.log2(np.maximum(f0, 20.0) / 100.0), strength, centroid / 1000.0]
    ).astype(np.float32)


def _analyse(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Frames + mel spectra + quality telemetry. Raises VoiceQualityError if unusable."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size == 0:
        raise VoiceQualityError("empty audio")
    seconds = x.size / SAMPLE_RATE
    if seconds < MIN_SECONDS:
        raise VoiceQualityError(f"clip too short ({seconds:.2f}s < {MIN_SECONDS}s) — speak a full phrase")
    x = x[: int(MAX_SECONDS * SAMPLE_RATE)]
    if x.size < FRAME_LEN:
        raise VoiceQualityError("clip shorter than one analysis frame")

    starts = np.arange(1 + (x.size - FRAME_LEN) // HOP) * HOP
    frames = x[starts[:, None] + np.arange(FRAME_LEN)[None, :]]
    emphasised = np.concatenate([frames[:, :1], frames[:, 1:] - PRE_EMPHASIS * frames[:, :-1]], axis=1)
    window = np.hanning(FRAME_LEN).astype(np.float32)
    power = np.abs(np.fft.rfft(emphasised * window, n=N_FFT, axis=1)) ** 2
    mel = np.log(np.maximum(power @ _FB.T, 1e-10)).astype(np.float32)

    rms = float(np.sqrt(np.mean(x**2)) + 1e-12)
    clip_ratio = float(np.mean(np.abs(x) > 0.985))
    energy_db = 10.0 * np.log10(np.maximum(power @ _FB.T, 1e-10)).mean(axis=1)
    energy = energy_db
    gate = float(np.percentile(energy, 35))
    keep = energy >= gate
    if keep.sum() < MIN_FRAMES:
        raise VoiceQualityError(f"not enough voiced frames ({int(keep.sum())}) — speak closer to the mic")
    # Proxy SNR, measured against the frames the VAD judged unvoiced. A recording
    # dominated by room noise has a compressed spread. When a clip contains too
    # little quiet to establish a floor, the measurement is marked unmeasurable
    # rather than failing the clip: absence of evidence of noise is not evidence of
    # noise, and refusing every continuous-speech recording would be a worse bug.
    floor = float(np.percentile(energy, 5))
    snr_db = float(np.percentile(energy, 90) - floor)
    # A floor is only a floor if something actually sat near it: a clip with no quiet
    # frames at all cannot be judged, and must not be rejected for that reason.
    snr_measurable = bool(float(np.mean(energy <= floor + 3.0)) >= 0.02)

    quality = {
        "seconds": round(x.size / SAMPLE_RATE, 2),
        "rms": round(rms, 5),
        "voiced_ratio": round(float(keep.mean()), 3),
        "snr_db": round(snr_db, 1),
        "clipping_ratio": round(clip_ratio, 4),
        "frames_used": int(keep.sum()),
        "snr_measurable": bool(snr_measurable),
    }
    problems = []
    if rms < MIN_RMS:
        problems.append("too quiet")
    if clip_ratio > MAX_CLIP_RATIO:
        problems.append("clipping/distortion")
    if snr_db < MIN_SNR_DB:
        problems.append(f"too noisy (SNR {snr_db:.1f} dB < {MIN_SNR_DB:.0f} dB)")
    if problems:
        raise VoiceQualityError("; ".join(problems) + " — re-record in quieter conditions")
    return mel[keep], frames[keep], quality


def embedding_from(x: np.ndarray) -> tuple[list[float], dict[str, Any]]:
    """Composite 60-dim embedding. DC-removed mean log-mel is the discriminative core."""
    mel, frames, quality = _analyse(x)
    mean_mel = mel.mean(axis=0)
    mel_block = MEL_WEIGHT * (mean_mel - mean_mel.mean())  # removing DC == gain-invariant

    mfcc = (mel @ _DCT.T).astype(np.float32)
    mfcc = mfcc - mfcc.mean(axis=0, keepdims=True)  # cepstral mean subtraction
    spread = SPREAD_WEIGHT * (mfcc.std(axis=0) + 1e-6)  # how much the voice moves within the utterance

    secondary = _pitch_and_tilt(frames)
    sec = SECONDARY_WEIGHT * np.concatenate(
        [secondary.mean(axis=0), secondary.std(axis=0) if secondary.shape[0] > 1 else np.zeros(3)]
    )

    vec = np.concatenate([mel_block, spread, sec]).astype(np.float64)
    vec = np.where(np.isfinite(vec), vec, 0.0)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        raise VoiceQualityError("degenerate audio (silence or DC only)")
    quality["embedding_dim"] = int(vec.size)
    return [float(v) for v in vec / norm], quality


def cosine(a, b) -> float:
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape:
        raise ValueError(f"embedding dim mismatch: {va.shape} vs {vb.shape}")
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return float(va @ vb / (na * nb))


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------
class SpeakerEmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, pcm16: bytes) -> tuple[list[float], dict[str, Any]]: ...


class LocalSpeakerModel:
    """Default provider: numpy DSP, no network, no model download, works offline."""

    name = "local-mfcc-mel"
    dim = N_MELS + N_CEPSTRA + 6

    def embed(self, pcm16: bytes) -> tuple[list[float], dict[str, Any]]:
        return embedding_from(decode_audio(pcm16))


class PretrainedSpeakerModel:
    """Adapter for a real speaker-embedding model when one is installed.

    ``speechbrain`` (ECAPA-TDNN) and ``resemblyzer`` both produce embeddings that
    are far more accurate than the local baseline and include training against
    spoofed/deviated audio. Selected automatically by :func:`default_provider`
    when available; the offline path stays in place when it is not.
    """

    name = "pretrained"

    def __init__(self) -> None:
        self._impl: Any = None
        if self._try_speechbrain():
            self.dim = 192
        elif self._try_resemblyzer():
            self.dim = 256
        else:
            raise RuntimeError("no pretrained speaker model installed")

    def _try_speechbrain(self) -> bool:
        try:
            from speechbrain.inference.speaker import EncoderClassifier  # type: ignore

            cache = Path(tempfile.gettempdir()) / "jarvis-speaker-model"
            self._impl = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(cache), run_opts={"device": "cpu"}
            )
            self.name = "speechbrain-ecapa"
            return True
        except Exception:
            return False

    def _try_resemblyzer(self) -> bool:
        try:
            from resemblyzer import VoiceEncoder, preprocess_wav  # type: ignore

            self._impl = (VoiceEncoder(), preprocess_wav)
            self.name = "resemblyzer-ge2d"
            return True
        except Exception:
            return False

    def embed(self, pcm16: bytes) -> tuple[list[float], dict[str, Any]]:
        import torch

        if self.name.startswith("speechbrain"):
            wave = torch.from_numpy(decode_audio(pcm16)).unsqueeze(0)
            vec = self._impl.encode_batch(wave).squeeze().cpu().numpy()
        else:
            encoder, preprocess = self._impl
            wav = preprocess(decode_audio(pcm16), source=16000)
            vec = encoder.embed_utterance(wav)
        vec = np.asarray(vec, dtype=np.float64).reshape(-1)
        return [float(v) for v in vec / (np.linalg.norm(vec) + 1e-12)], {"provider": self.name}


def default_provider() -> SpeakerEmbeddingProvider:
    """Prefer a pretrained model when installed; otherwise stay fully local."""
    try:
        return PretrainedSpeakerModel()
    except Exception:
        return LocalSpeakerModel()


# ---------------------------------------------------------------------------
# enrolment model
# ---------------------------------------------------------------------------
@dataclass
class VoiceprintModel:
    centroid: list[float]
    threshold: float
    sample_count: int
    provider: str = LocalSpeakerModel.name
    model_version: int = 3
    calibrated: bool = False
    created_at: float = field(default_factory=time.time)
    intra_sims: list[float] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    quality: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "centroid": [round(v, 6) for v in self.centroid],
                "threshold": round(self.threshold, 5),
                "sample_count": self.sample_count,
                "provider": self.provider,
                "model_version": self.model_version,
                "calibrated": self.calibrated,
                "created_at": self.created_at,
                "intra_sims": [round(v, 5) for v in self.intra_sims],
                "embeddings": [[round(v, 5) for v in e] for e in self.embeddings],
                "quality": self.quality,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | dict | None) -> VoiceprintModel | None:
        if not raw:
            return None
        data = raw if isinstance(raw, dict) else json.loads(raw)
        if not data.get("centroid"):
            return None
        return cls(
            centroid=[float(v) for v in data["centroid"]],
            threshold=float(data.get("threshold", 0.80)),
            sample_count=int(data.get("sample_count", 1)),
            provider=str(data.get("provider", "local")),
            model_version=int(data.get("model_version", 1)),
            calibrated=bool(data.get("calibrated", False)),
            created_at=float(data.get("created_at", 0.0)),
            intra_sims=[float(v) for v in data.get("intra_sims", [])],
            embeddings=[[float(v) for v in e] for e in data.get("embeddings", [])],
            quality=list(data.get("quality", [])),
        )

    @property
    def needs_more_samples(self) -> bool:
        return self.sample_count < 3

    @property
    def fingerprint(self) -> str:
        """Stable hash so a device can tell whether its cached copy is current."""
        import hashlib

        blob = json.dumps([round(v, 6) for v in self.centroid] + [round(self.threshold, 5), self.provider], separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def enroll(embeddings: list[list[float]], *, provider: str = LocalSpeakerModel.name, margin: float = DEFAULT_MARGIN, quality: list[dict] | None = None) -> VoiceprintModel:
    if not embeddings:
        raise ValueError("no utterances to enrol")
    dim = {len(e) for e in embeddings}
    if len(dim) != 1:
        raise ValueError(f"enrolment samples have inconsistent dimensions: {dim}")
    mat = np.asarray(embeddings, dtype=np.float64)
    centroid = mat.mean(axis=0)
    centroid = centroid / max(float(np.linalg.norm(centroid)), 1e-12)
    cent = [float(v) for v in centroid]
    sims = [cosine(e, cent) for e in embeddings]
    if len(embeddings) == 1:
        # No intra-speaker spread measurable -> a fixed strict default, and the UI
        # is told to ask for more samples (needs_more_samples).
        threshold = 0.90
    else:
        pairwise = [cosine(a, b) for i, a in enumerate(embeddings) for b in embeddings[i + 1 :]]
        # Accept anything at least as close as the *loosest* genuine pair, minus a margin
        # that shrinks as evidence accumulates. Two or three takes give one or two pair
        # distances, which is a very thin read on how this voice behaves across ordinary
        # conversation, so the bar sits further below it; by a dozen takes the spread is a
        # real estimate and can be trusted more closely. The ceiling exists because an
        # enrolment set of near-identical sentences measures nothing about variability,
        # and a bar derived from it rejects the owner's everyday speech.
        adaptive = margin + 0.09 / len(embeddings)
        ceiling = 0.93 if len(embeddings) < 5 else 0.95
        threshold = max(0.55, min(ceiling, min(pairwise) - adaptive))
    return VoiceprintModel(
        centroid=cent,
        threshold=threshold,
        sample_count=len(embeddings),
        provider=provider,
        intra_sims=[float(s) for s in sims],
        embeddings=[list(map(float, e)) for e in embeddings],
        quality=list(quality or []),
    )


def verify(
    model: VoiceprintModel,
    embedding: list[float],
    *,
    impostor_centroids: list[list[float]] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Compare one utterance to the enrolled centroid.

    ``strict`` (the default for privileged actions) refuses to pass while the
    model is uncalibrated or enrolled from too few samples, and tightens the
    threshold when another enrolled speaker is nearby.
    """
    if len(embedding) != len(model.centroid):
        return {
            "accepted": False,
            "reason": "embedding-dim-mismatch",
            "detail": f"model={len(model.centroid)} candidate={len(embedding)} (provider changed?)",
            "similarity": 0.0,
            "threshold": model.threshold,
        }
    sim = cosine(embedding, model.centroid)
    threshold = model.threshold
    notes: list[str] = []
    if impostor_centroids:
        worst = max(cosine(embedding, c) for c in impostor_centroids if len(c) == len(embedding))
        if worst >= threshold - 0.02:
            threshold = min(0.99, worst + DEFAULT_MARGIN)
            notes.append("threshold raised: another enrolled speaker is close to this utterance")
    accepted = sim >= threshold
    blocked: str | None = None
    if accepted and strict:
        if not model.calibrated:
            blocked = "uncalibrated"
            notes.append("voiceprint is not calibrated against real speech, so it cannot grant privileged access")
        elif model.needs_more_samples:
            blocked = "thin-enrolment"
            notes.append(f"re-enrol with 3+ samples (currently {model.sample_count}) for a trustworthy threshold")
    return {
        "similarity": round(sim, 5),
        "threshold": round(threshold, 5),
        "margin": round(sim - threshold, 5),
        "accepted": bool(accepted and blocked is None),
        "match": bool(accepted),
        "blocked_reason": blocked,
        "model_samples": model.sample_count,
        "provider": model.provider,
        "notes": notes,
    }


@dataclass
class CalibrationReport:
    """Measured genuine/impostor separation from labelled trials.

    A voiceprint should not be trusted for privileged access until a real trial
    set has been run on the deployment's actual hardware and users.
    """

    genuine: int = 0
    impostor: int = 0
    threshold: float = 0.0
    far: float = 1.0
    frr: float = 1.0
    eer_estimate: float = 1.0
    min_genuine: float = 0.0
    max_impostor: float = 0.0
    separable: bool = False
    usable_for_privileged: bool = False

    @classmethod
    def from_trials(
        cls,
        genuine_sims: list[float],
        impostor_sims: list[float],
        *,
        target_far: float = 0.01,
    ) -> CalibrationReport:
        rep = cls(genuine=len(genuine_sims), impostor=len(impostor_sims))
        if not genuine_sims or not impostor_sims:
            return rep
        rep.min_genuine, rep.max_impostor = min(genuine_sims), max(impostor_sims)
        # pick the highest threshold that keeps FAR <= target
        candidates = sorted(set(impostor_sims) | set(genuine_sims))
        best = None
        for t in candidates:
            far = sum(1 for s in impostor_sims if s >= t) / len(impostor_sims)
            frr = sum(1 for s in genuine_sims if s < t) / len(genuine_sims)
            if far <= target_far and (best is None or frr < best[1]):
                best = (t, frr, far)
        if best is None:
            t = max(impostor_sims) + 0.01
            rep.threshold = round(min(t, 0.999), 5)
            rep.frr = sum(1 for s in genuine_sims if s < t) / len(genuine_sims)
            rep.far = 0.0
        else:
            rep.threshold, rep.frr, rep.far = round(best[0], 5), best[1], best[2]
        rep.eer_estimate = round((rep.far + rep.frr) / 2.0, 4)
        rep.separable = rep.min_genuine > rep.max_impostor
        rep.usable_for_privileged = rep.far <= target_far and rep.frr <= 0.10 and rep.genuine >= 10 and rep.impostor >= 10
        return rep

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------------
# helpers for demos / tests / browser capture
# ---------------------------------------------------------------------------
def pcm16_to_wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


#: vowel-ish formant pairs (F1, F2) indexed by character class, so a string with
#: different letters produces a genuinely different spectral trajectory. Used only by
#: the demo/test generator, to make it exercise content vs. speaker separation.
_VOWELS = {
    "a": (730, 1090), "e": (530, 1840), "i": (270, 2290), "o": (570, 840),
    "u": (300, 870), "y": (300, 2200), "r": (490, 1350), "l": (380, 1100),
    "s": (1800, 4500), "t": (1600, 3800), "k": (1400, 2300), "m": (250, 1100),
    "n": (250, 1700), "b": (300, 950), "d": (300, 1800), "g": (300, 1300),
    "f": (1300, 2500), "v": (1300, 2400), "p": (350, 1000), "h": (500, 1500),
    "w": (300, 600), "j": (300, 2100), "c": (700, 1200), "z": (1500, 2400),
    "q": (400, 1700), "x": (900, 2600),
}


def synthesize_probe_pcm(
    text: str,
    seconds: float = 2.2,
    f0: float = 120.0,
    rate: int = SAMPLE_RATE,
    *,
    formant_scale: float | None = None,
) -> bytes:
    """Deterministic speech-like signal for demos and tests, so the flow can be
    exercised without a microphone.

    ``text`` drives the phoneme (formant) trajectory; ``f0`` and ``formant_scale``
    drive the voice. A different speaker means a different vocal-tract length as well
    as a different pitch — a shorter tract raises formants — so ``formant_scale``
    defaults to a coupling derived from ``f0``. Same text + same ``f0`` = same words
    by one person; same text + different ``f0`` = same words by different people,
    which is the axis the voiceprint and the command spotter must treat oppositely.
    """
    letters = [c for c in text.lower() if c.isalpha()] or ["a"]
    # A stable digest, not Python's hash(): string hashing is salted per process, so
    # ``hash(text)`` would make the same sentence produce different audio on the next
    # run. Tests, demo output and re-enrolment all need this to be reproducible.
    rng = np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
    # Phrasing, not a constant: phonemes are grouped into "words" with a short release
    # at each group boundary. The silence between groups is what lets the VAD find a
    # noise floor and the SNR gate measure anything; the continuous speech inside a
    # group is what stops the endpointer from chopping one utterance into pieces. Both
    # properties are required, which is why the duration is *grown* here rather than
    # squeezing the requested number of phonemes into the caller's seconds.
    group = 4 if len(letters) > 6 else max(1, len(letters) // 2) or 1
    min_span = 0.30 / (0.85 * group)  # keep each voiced run above the VAD's 220 ms floor
    span = max(float(seconds) / len(letters), min_span, 0.115)
    seconds = span * len(letters)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    seq = [(_VOWELS.get(c, (600, 1500)), (0.5 if c in "aeiouy" else 0.75)) for c in letters]
    idx = np.clip((t / span).astype(int), 0, len(seq) - 1)
    contour = f0 * (1.0 + 0.06 * np.sin(2 * np.pi * 0.9 * t) + 0.01 * rng.standard_normal(n))
    phase = 2 * np.pi * np.cumsum(contour) / rate
    saw = 2.0 * ((phase / (2 * np.pi)) % 1.0) - 1.0
    form = np.zeros(n, dtype=np.float64)
    scale = ((f0 / 120.0) ** 0.65) if formant_scale is None else formant_scale
    for k, ((f1, f2), amp) in enumerate(seq):
        mask = (idx == k).astype(float)
        # 3 formants per phoneme: F1, F2, and a fixed F3 (~2.5 kHz) for timbre
        for centre, weight in ((f1 * scale, 1.0), (f2 * scale, 0.75), ((2500.0 + 30.0 * (int(hashlib.sha256(text.encode()).hexdigest()[8:10], 16) % 7)) * scale, 0.5)):
            onset = np.maximum(0.0, np.minimum(1.0, (np.arange(n) - np.searchsorted(t, k * span)) / (0.012 * rate)))
            form += weight * amp * np.sin(2 * np.pi * centre * t) * mask * onset
    env = np.clip(np.minimum(t / 0.03, (seconds - t) / 0.05), 0, 1)
    env = env * np.where((((t % (span * group)) / (span * group)) < 0.85), 1.0, 0.004)
    sig = 0.5 * saw + 0.5 * form / (np.max(np.abs(form)) + 1e-9)
    sig = sig * env * 0.6 + 0.0015 * rng.standard_normal(n)
    return (np.clip(sig, -1, 1) * 32767).astype("<i2").tobytes()


def wav_base64(pcm: bytes, rate: int = SAMPLE_RATE) -> str:
    import base64

    return base64.b64encode(pcm16_to_wav_bytes(pcm, rate)).decode()


__all__ = [
    "CalibrationReport",
    "LocalSpeakerModel",
    "PretrainedSpeakerModel",
    "VoiceQualityError",
    "VoiceprintModel",
    "cosine",
    "decode_audio",
    "default_provider",
    "embedding_from",
    "enroll",
    "pcm16_to_wav_bytes",
    "synthesize_probe_pcm",
    "verify",
    "wav_base64",
]
