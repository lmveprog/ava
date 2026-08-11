"""Semantic end-of-turn detection: has the speaker actually finished?

The RMS silence gate can't tell a thinking pause from the end of a sentence, so
it waits a fixed 0.68 s after EVERY utterance — the single biggest chunk of
"Ava takes too long to answer". Smart Turn v3 (pipecat-ai, BSD-2) reads the
waveform itself — prosody, not transcript — and answers in ~15 ms on CPU
whether the turn sounds complete.

Adopted from the 2026 crawl: Pipecat and LiveKit Agents both endpoint this
exact way (a light VAD for activity + this model for the decision). The model
is 8 MB, lives in models/, and both onnxruntime and the Whisper feature
extractor were already in the venv — zero new dependencies.

Behaviour when anything is missing (model file, imports): `probability` returns
None and the caller keeps the old fixed-silence behaviour. Nothing breaks.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from ava import paths

MODEL_PATH = paths.MODELS_DIR / "smart-turn-v3.2-cpu.onnx"
SAMPLE_RATE = 16000
MAX_SECONDS = 8            # the model was trained on the last 8 s of a turn
COMPLETE_THRESHOLD = 0.55  # a hair above the paper's 0.5: fewer early cuts

_lock = threading.Lock()
_session = None
_extractor = None
_broken = False


def available() -> bool:
    return MODEL_PATH.exists() and not _broken


def _load():
    global _session, _extractor, _broken
    with _lock:
        if _session is not None:
            return _session, _extractor
        try:
            import onnxruntime
            from transformers import WhisperFeatureExtractor
            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 2
            _session = onnxruntime.InferenceSession(
                str(MODEL_PATH), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            _extractor = WhisperFeatureExtractor(chunk_length=MAX_SECONDS)
            return _session, _extractor
        except Exception:  # noqa: BLE001 — missing wheel, corrupt file...
            _broken = True
            raise


def probability(pcm: bytes) -> float | None:
    """P(turn is finished) for 16 kHz mono int16 audio, or None if unavailable.

    Called during the silence that follows speech, on the whole utterance so
    far. ~15 ms on this machine: cheap enough to ask several times per second.
    """
    if not available() or not pcm:
        return None
    try:
        session, extractor = _load()
    except Exception:  # noqa: BLE001
        return None
    try:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        audio = audio[-SAMPLE_RATE * MAX_SECONDS:]
        features = extractor(
            audio, sampling_rate=SAMPLE_RATE, max_length=SAMPLE_RATE * MAX_SECONDS,
            padding="max_length", return_tensors="np", do_normalize=True,
        )
        input_features = features.input_features.astype(np.float32)
        outputs = session.run(None, {"input_features": input_features})
        return float(outputs[0][0].item())
    except Exception:  # noqa: BLE001
        return None


class TurnGate:
    """The two-tier endpoint used inside `_record_utterance`.

    Fast path: 0.24 s after the voice stops, ask the model — "finished" ends
    the capture right there (0.68 s -> ~0.3 s felt latency). Slow path: the
    model hears an unfinished sentence, so we grant a longer pause (1.1 s)
    instead of cutting the speaker off mid-thought. No model: the caller's
    fixed silence wins, exactly as before.
    """

    ASK_AFTER_S = 0.24
    RECHECK_EVERY_S = 0.20
    INCOMPLETE_PATIENCE_S = 1.1

    def __init__(self) -> None:
        self._last_ask = 0.0

    def decision(self, silence_s: float, frames: list[bytes]) -> str:
        # returns "complete", "wait", or "unknown" (no model -> caller decides)
        if not available():
            return "unknown"
        if silence_s < self.ASK_AFTER_S:
            return "wait"
        now = time.monotonic()
        if now - self._last_ask < self.RECHECK_EVERY_S:
            return "wait" if silence_s < self.INCOMPLETE_PATIENCE_S else "unknown"
        self._last_ask = now
        score = probability(b"".join(frames))
        if score is None:
            return "unknown"
        if score >= COMPLETE_THRESHOLD:
            return "complete"
        # sounded unfinished: be patient, but not forever
        return "wait" if silence_s < self.INCOMPLETE_PATIENCE_S else "unknown"
