"""Speech input: VAD, transcription adapters, and offline keyword spotting.

Offline strategy (requirement: "voice recognition ... must work without internet")
-----------------------------------------------------------------------------------
* **Speaker verification is offline** — it lives in :mod:`jarvis.voice` and needs no
  network, model download, or third party.
* **Command recognition for a small closed set is offline** — ``CommandSpotter``
  does template matching over the same log-mel features, so "assistant on",
  "start sync", "pause devices" etc. work with no model at all.
* **Open-vocabulary dictation is not something this codebase can invent offline.**
  ``transcribe`` therefore looks for a *local* engine first (whisper.cpp /
  faster-whisper / vosk — all of which run fully offline) and only then the
  configured remote endpoint. If none exists it returns a structured
  ``engine="unavailable"`` result rather than silently pretending to have
  transcribed audio. That distinction matters: a fake transcript in an assistant
  is worse than an honest "no engine installed".
"""

from __future__ import annotations

import itertools
import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from jarvis import voice as voice_mod
from jarvis.net import run_with_timeout

ENERGY_FLOOR_DB = -38.0
#: a candidate shorter than this many voiced frames cannot be aligned reliably
MIN_TEMPLATE_FRAMES = 8

MIN_SPEECH_MS = 220.0
HANGOVER_MS = 240.0


@dataclass
class Segment:
    start_s: float
    end_s: float
    rms: float

    @property
    def duration(self) -> float:
        return float(self.end_s - self.start_s)

    def as_dict(self) -> dict[str, Any]:
        return {"start_s": round(self.start_s, 3), "end_s": round(self.end_s, 3), "rms": round(self.rms, 5)}


HOP_MS = 10.0


def detect_speech(x: np.ndarray, *, hop: int = voice_mod.HOP, frame_len: int = voice_mod.FRAME_LEN) -> list[Segment]:
    """Energy + spectral-variation VAD. Returns voiced segments, merging short gaps."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size < frame_len:
        return []
    n = 1 + (x.size - frame_len) // hop
    starts = np.arange(n) * hop
    frames = x[starts[:, None] + np.arange(frame_len)[None, :]]
    window = np.hanning(frame_len).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * window, n=voice_mod.N_FFT, axis=1)) ** 2
    log_energy = 10.0 * np.log10(np.maximum(spec.sum(axis=1), 1e-12))
    floor = float(np.percentile(log_energy, 10))
    active = log_energy > max(floor + 8.0, -70.0)
    # zero-crossing spread adds a cheap voiced/unvoiced discriminator
    zcr = np.mean(np.abs(np.diff(np.sign(frames), axis=1)) > 0, axis=1)
    active &= zcr < 0.62
    if not active.any():
        return []
    segs: list[Segment] = []
    i = 0
    min_frames = int(MIN_SPEECH_MS / HOP_MS)
    hangover = int(HANGOVER_MS / HOP_MS)
    while i < len(active):
        if not active[i]:
            i += 1
            continue
        j = i
        gap = 0
        while j < len(active) and (active[j] or gap < hangover):
            gap = 0 if active[j] else gap + 1
            j += 1
        span = [k for k in range(i, min(j, len(active))) if active[k]]
        if len(span) >= max(3, min_frames):
            rms = float(np.sqrt(np.mean(frames[span] ** 2)))
            segs.append(Segment(span[0] * hop / voice_mod.SAMPLE_RATE, span[-1] * hop / voice_mod.SAMPLE_RATE, rms))
        i = j
    return segs


@dataclass
class Transcript:
    text: str | None
    engine: str
    language: str = "en"
    confidence: float | None = None
    available: bool = True
    latency_ms: int = 0
    seconds: float = 0.0
    segments: list[Segment] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "engine": self.engine,
            "language": self.language,
            "confidence": self.confidence,
            "available": self.available,
            "latency_ms": self.latency_ms,
            "seconds": round(self.seconds, 2),
            "segments": [s.as_dict() for s in self.segments],
            "error": self.error,
        }


class Transcriber:
    def __init__(self, settings):
        self.settings = settings

    def transcribe(self, pcm: bytes, *, language: str = "en") -> Transcript:
        started = time.time()
        try:
            x = voice_mod.decode_audio(pcm)
        except Exception as exc:
            return Transcript(None, "input-error", available=False, error=f"could not decode audio: {exc}")
        segments = detect_speech(x)
        seconds = float(x.size / voice_mod.SAMPLE_RATE)
        if not segments:
            return Transcript(None, "vad", seconds=seconds, error="no speech detected", available=True, latency_ms=self._ms(started))
        engine = self._pick_engine()
        if engine == "unavailable":
            return Transcript(
                None,
                "unavailable",
                seconds=seconds,
                segments=segments,
                available=False,
                error=(
                    "no offline ASR engine found and no remote endpoint configured; "
                    "install whisper.cpp or faster-whisper for offline dictation, or set "
                    "JARVIS_ASR_PROVIDER=whisper-http + JARVIS_ASR_ENDPOINT. Voiceprint auth, "
                    "offline commands, and typed input all work without an engine."
                ),
                latency_ms=self._ms(started),
            )
        if engine == "faster-whisper":
            return self._faster_whisper(x, segments, seconds, language, started)
        if engine == "whisper.cpp":
            return self._whisper_cpp(x, segments, seconds, language, started)
        if engine == "vosk":
            return self._vosk(x, segments, seconds, started)
        return self._remote_http(x, segments, seconds, language, started)

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.time() - started) * 1000)

    def _pick_engine(self) -> str:
        if shutil.which("whisper-cli"):
            return "whisper.cpp"
        try:
            import faster_whisper  # type: ignore  # noqa: F401

            return "faster-whisper"
        except Exception:
            pass
        try:
            import vosk  # type: ignore  # noqa: F401

            if Path(os.environ.get("VOSK_MODEL_DIR", "")).is_dir():
                return "vosk"
        except Exception:
            pass
        if self.settings.asr_provider == "whisper-http" and self.settings.asr_endpoint:
            return "whisper-http"
        return "unavailable"

    def _faster_whisper(self, x, segments, seconds, language, started) -> Transcript:
        try:
            from faster_whisper import WhisperModel  # type: ignore

            model = WhisperModel(os.environ.get("JARVIS_WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
            info = model.transcribe(x, language=None if language == "auto" else language, vad_filter=False)
            text = " ".join(s.text.strip() for s in info.segments).strip()
            return Transcript(text or None, "faster-whisper(local)", seconds=seconds, segments=segments, latency_ms=self._ms(started))
        except Exception as exc:
            return Transcript(None, "faster-whisper(local)", seconds=seconds, segments=segments, available=False, error=str(exc), latency_ms=self._ms(started))

    def _whisper_cpp(self, x, segments, seconds, language, started) -> Transcript:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            path = Path(fh.name)
            path.write_bytes(voice_mod.pcm16_to_wav_bytes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()))
        cmd = ["whisper-cli", "-m", os.environ.get("JARVIS_WHISPER_MODEL_PATH", "~/.cache/whisper/ggml-base.bin"), "-f", str(path), "-nt", "-otxt"]
        code, out, err = run_with_timeout([c.replace("~", str(Path.home())) for c in cmd], timeout=120)
        path.unlink(missing_ok=True)
        if code != 0:
            return Transcript(None, "whisper.cpp(local)", seconds=seconds, segments=segments, available=False, error=(err or out)[-400:], latency_ms=self._ms(started))
        text = (out or "").strip()
        return Transcript(text or None, "whisper.cpp(local)", seconds=seconds, segments=segments, latency_ms=self._ms(started))

    def _vosk(self, x, segments, seconds, started) -> Transcript:
        try:
            from vosk import KaldiRecognizer, Model  # type: ignore

            model = Model(os.environ["VOSK_MODEL_DIR"])
            rec = KaldiRecognizer(model, voice_mod.SAMPLE_RATE)
            pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
            parts = []
            for i in range(0, len(pcm), 16000):
                if rec.AcceptWaveform(pcm[i : i + 16000]):
                    parts.append(json.loads(rec.Result()).get("text", ""))
            parts.append(json.loads(rec.FinalResult()).get("text", ""))
            text = " ".join(p for p in parts if p).strip()
            return Transcript(text or None, "vosk(local)", seconds=seconds, segments=segments, latency_ms=self._ms(started))
        except Exception as exc:
            return Transcript(None, "vosk(local)", seconds=seconds, segments=segments, available=False, error=str(exc), latency_ms=self._ms(started))

    def _remote_http(self, x, segments, seconds, language, started) -> Transcript:
        import httpx

        wav = voice_mod.pcm16_to_wav_bytes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        try:
            resp = httpx.post(
                self.settings.asr_endpoint.rstrip("/") + "/v1/audio/transcriptions",
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={"model": os.environ.get("JARVIS_ASR_MODEL", "whisper-1"), "language": language},
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"} if self.settings.llm_api_key else {},
                timeout=30.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            return Transcript(
                (payload.get("text") or "").strip() or None,
                "whisper-http(remote)",
                seconds=seconds,
                segments=segments,
                confidence=payload.get("confidence"),
                latency_ms=self._ms(started),
            )
        except Exception as exc:
            return Transcript(None, "whisper-http(remote)", seconds=seconds, segments=segments, available=False, error=str(exc), latency_ms=self._ms(started))


# ---------------------------------------------------------------------------
# offline closed-vocabulary commands
# ---------------------------------------------------------------------------
@dataclass
class CommandTemplate:
    phrase: str
    frames: np.ndarray
    enrolled_at: float = field(default_factory=time.time)


def _content_frames(pcm: bytes) -> np.ndarray:
    """13 MFCCs per frame, cepstrally mean-normalised.

    Deliberately *not* the voiceprint embedding: a time-averaged vector throws away
    exactly the information a keyword spotter needs (which sounds occurred in which
    order), which is why template matching over it accepts the wrong phrase. Frame
    sequences plus DTW are what separate content from speaker.
    """
    x = voice_mod.decode_audio(pcm)
    n = x.size
    if n < voice_mod.FRAME_LEN:
        raise voice_mod.VoiceQualityError("clip too short for command recognition")
    starts = np.arange(1 + (n - voice_mod.FRAME_LEN) // voice_mod.HOP) * voice_mod.HOP
    frames = x[starts[:, None] + np.arange(voice_mod.FRAME_LEN)[None, :]]
    pre = np.concatenate([frames[:, :1], frames[:, 1:] - voice_mod.PRE_EMPHASIS * frames[:, :-1]], axis=1)
    power = np.abs(np.fft.rfft(pre * np.hanning(voice_mod.FRAME_LEN).astype(np.float32), n=voice_mod.N_FFT, axis=1)) ** 2
    mel = np.log(np.maximum(power @ voice_mod._FB.T, 1e-10)).astype(np.float32)
    mfcc = (mel @ voice_mod._DCT.T).astype(np.float32)
    mfcc = mfcc - mfcc.mean(axis=0, keepdims=True)
    e = mel.mean(axis=1)
    keep = e >= np.percentile(e, 30)
    out = mfcc[keep] if keep.sum() >= 4 else mfcc
    if out.shape[0] < 4:
        raise voice_mod.VoiceQualityError("not enough voiced frames for command recognition")
    return np.ascontiguousarray(out)


def dtw_cost(a: np.ndarray, b: np.ndarray, *, band_ratio: float = 0.3) -> float:
    """Mean per-frame DTW distance between two feature sequences (Sakoe-Chiba band).

    The row recurrence is walked column by column on purpose. A fully vectorised row
    update (``costs + min(prev, diag, left)`` with ``left`` read from the same row
    before it was written) looks equivalent and is not: it forbids horizontal steps, so
    time warping disappears, two takes of the same phrase with different lengths come
    back as infinite distance, and any threshold calibrated from those numbers is
    nonsense. Plain Python floats inside the band are also faster here than numpy scalar
    indexing, and the band keeps the cost at O(n * band) instead of O(n * m).
    """
    n, m = a.shape[0], b.shape[0]
    if n == 0 or m == 0:
        return float("inf")
    # The band is always wide enough to reach the far corner: with a pure Sakoe-Chiba
    # band, two takes whose lengths differ by more than the band are "unreachable" and
    # come back as infinite cost, which poisons every number derived from it. Length
    # difference is instead paid for honestly, as insertion cost along the path.
    band = max(12, int(band_ratio * max(n, m)), abs(n - m) + 12)
    d = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
    inf = float("inf")
    prev: list[float] = [0.0] + [inf] * m
    for i in range(1, n + 1):
        lo, hi = max(1, i - band), min(m, i + band)
        di = d[i - 1]
        cur: list[float] = [inf] * (m + 1)
        for j in range(lo, hi + 1):
            best = prev[j]
            if prev[j - 1] < best:
                best = prev[j - 1]
            left = cur[j - 1]
            if left < best:
                best = left
            cur[j] = float(di[j - 1]) + best
        prev = cur
    total = prev[m]
    if not math.isfinite(total):
        return inf
    # normalise by path length so phrase length does not dominate the threshold
    return total / max(n, m)


class CommandSpotter:
    """Closed-vocabulary command spotting that needs no model and no network.

    Enrol a phrase once from real speech; recognition is DTW over MFCC frame
    sequences with an ambiguity margin, so a *near miss* returns "no match"
    instead of executing the wrong command. Template matching is only reliable for
    a small vocabulary, so that is the range it is offered for — open-vocabulary
    dictation goes through an ASR engine (see :class:`Transcriber`).
    """

    #: Accept below this mean per-frame MFCC distance. This number came from the
    #: synthetic demo battery (same-phrase variants <= ~3.5, different-phrase >= ~16)
    #: and is only a safe-by-default placeholder: real deployments must set it from
    #: enrollment data via :meth:`apply_calibrated_threshold`, because mic, distance
    #: and speaking rate move this scale far more than any constant can cover.
    distance_threshold = 10.0
    #: hard ceiling. Calibration may *lower* the bar, never raise it past this: an
    #: unlimited threshold turns a single-command vocabulary into "matches anything".
    max_distance = 12.0
    #: used when there is not enough enrolment data to measure this speaker's variability
    single_take_threshold = 6.0
    #: winner must beat the runner-up by at least this much
    ambiguity_margin = 4.0

    def __init__(self, *, distance_threshold: float | None = None, ambiguity_margin: float | None = None):
        self.templates: list[CommandTemplate] = []
        self._samples: dict[str, list[np.ndarray]] = {}
        self._enrolled_at: dict[str, float] = {}
        if distance_threshold is not None:
            self.distance_threshold = distance_threshold
        if ambiguity_margin is not None:
            self.ambiguity_margin = ambiguity_margin

    def enroll(self, phrase: str, pcm: bytes, *, replace: bool = False) -> dict[str, Any]:
        """Add one recording of a phrase. Multiple takes per phrase are kept and used
        to calibrate the acceptance threshold from this user's own variability."""
        return self._add_frames(phrase, _content_frames(pcm), replace=replace)

    def _add_frames(self, phrase: str, frames: np.ndarray, *, replace: bool = False) -> dict[str, Any]:
        key = phrase.lower().strip()
        if replace:
            self._samples.pop(key, None)
        self._samples.setdefault(key, []).append(frames)
        self._rebuild_templates()
        takes = self._samples[key]
        return {
            "phrase": key,
            "frames": int(frames.shape[0]),
            "takes": len(takes),
            "replaced": replace,
            "templates": len(self.templates),
            "suggested_threshold": self.suggest_threshold(),
        }

    def _rebuild_templates(self) -> None:
        """One template per phrase: the take nearest the centroid of that phrase's takes.

        Averaging MFCC frame *sequences* is meaningless (they are not aligned), so the
        representative has to be an actual take. Recognition scores against every
        template, which is what keeps near-miss phrases from winning.
        """
        self.templates = []
        for key, takes in self._samples.items():
            if len(takes) == 1:
                chosen = takes[0]
            else:
                centroid = np.mean([np.concatenate([f.mean(0), f.std(0)]) for f in takes], axis=0)
                scores = [float(np.linalg.norm(np.concatenate([f.mean(0), f.std(0)]) - centroid)) for f in takes]
                chosen = takes[int(np.argmin(scores))]
            self.templates.append(CommandTemplate(key, chosen))

    @property
    def calibrated(self) -> bool:
        """True once at least one phrase has more than one take, i.e. when a threshold can be
        measured from this user's own variability instead of assumed. Kept as a property so the
        API and :meth:`recognize` cannot disagree about what "calibrated" meant today."""
        return bool(self._samples and any(len(v) > 1 for v in self._samples.values()))

    def phrases(self) -> list[str]:
        return sorted(self._samples)

    def takes(self) -> dict[str, int]:
        return {key: len(self._samples[key]) for key in sorted(self._samples)}

    def remove(self, phrase: str) -> bool:
        """Forget a phrase and every take of it.

        An enrolled command is biometric-ish training data, so whatever the user can add
        through the API they must be able to delete through the API — otherwise "enrol your
        own wake words" quietly becomes "hand us a recording you can never take back".
        """
        key = phrase.lower().strip()
        if key not in self._samples:
            return False
        del self._samples[key]
        self._rebuild_templates()
        return True

    def suggest_threshold(self, *, margin: float = 1.6) -> float | None:
        """Highest within-phrase distance x margin, so "say it again, differently" passes.

        Deriving the bar from the speaker's own takes beats a fixed constant: mic,
        distance and speaking rate vary far more across users than any constant can
        absorb. Capped at :attr:`max_distance`, because the number is only a measure of
        *this* user's consistency — it says nothing about how far a different phrase or
        a different voice sits, and an uncalibrated ceiling would accept those too.
        Returns None until at least one phrase has two takes.
        """
        within: list[float] = []
        for _phrase, takes in self._samples.items():
            for a, b in itertools.combinations(takes, 2):
                cost = dtw_cost(a, b)
                if math.isfinite(cost):
                    within.append(cost)
        if not within:
            return None
        return round(min(max(within) * margin, self.max_distance), 3)

    def apply_calibrated_threshold(self, *, margin: float = 1.6) -> float | None:
        """Set the acceptance distance from the enrolled takes.

        With a single take per phrase there is no variability to measure, so the bar is
        *tightened* to :attr:`single_take_threshold` rather than left at the default: the
        cost of asking the user to re-record is far below the cost of executing a command
        nobody said.
        """
        value = self.suggest_threshold(margin=margin)
        self.distance_threshold = value if value is not None else min(self.distance_threshold, self.single_take_threshold)
        return self.distance_threshold

    def recognize(self, pcm: bytes, *, threshold: float | None = None) -> dict[str, Any]:
        if not self.templates:
            return {"matched": None, "reason": "no command templates enrolled"}
        frames = _content_frames(pcm)
        if frames.shape[0] < MIN_TEMPLATE_FRAMES:
            return {"matched": None, "reason": "no usable speech frames — say the whole phrase"}
        scored = sorted((dtw_cost(frames, t.frames), t.phrase) for t in self.templates)
        best, best_phrase = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else float("inf")
        thr = self.distance_threshold if threshold is None else threshold
        if not math.isfinite(thr):
            # an infinite threshold means calibration never ran; refusing is the only
            # safe reading, otherwise "matched" would be returned for any input at all
            return {"matched": None, "distance": None, "threshold": None, "reason": "threshold uncalibrated", "calibrated": False}
        ok = best <= thr and (runner_up - best) >= self.ambiguity_margin
        return {
            "matched": best_phrase if ok else None,
            "distance": round(best, 3),
            "runner_up_distance": None if runner_up == float("inf") else round(runner_up, 3),
            "threshold": thr,
            "calibrated": self.calibrated,
            "reason": "ok" if ok else ("no speech resembling a command" if not math.isfinite(best) else (f"too far ({best:.1f} > {thr})" if best > thr else "ambiguous with another command")),
        }

    def export(self) -> list[dict[str, Any]]:
        """Every take, not just the representative.

        Persisting only the representative would silently lose the calibration data on
        restart: with one take left, :meth:`apply_calibrated_threshold` can measure no
        variability and falls back to the strict default, so the user's commands would
        start refusing to match after each reboot.
        """
        return [
            {
                "phrase": key,
                "takes": [f.tolist() for f in frames_list],
                "frames": frames_list[0].tolist(),  # legacy single-take shape, still read by load()
                "enrolled_at": self._enrolled_at.get(key, time.time()),
            }
            for key, frames_list in self._samples.items()
        ]

    def load(self, data: list[dict[str, Any]]) -> None:
        """Restore from :meth:`export`. Accepts the legacy single-``frames`` shape so a
        commands.json written by an older build still works."""
        self._samples, self._enrolled_at, self.templates = {}, {}, []
        for item in data:
            key = str(item.get("phrase", "")).lower().strip()
            if not key:
                continue
            raw = item.get("takes") or [item.get("frames")]
            for frames in raw:
                arr = np.asarray(frames, dtype=np.float32)
                if arr.ndim == 2 and arr.shape[1] > 0:
                    self._samples.setdefault(key, []).append(arr)
                    self._enrolled_at[key] = float(item.get("enrolled_at") or time.time())
        self._rebuild_templates()
