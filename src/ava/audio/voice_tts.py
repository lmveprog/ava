"""ava's voice: **one engine, and a safety net**.

`chatterbox` (the default) runs on the mac's gpu. it never leaves the machine,
costs nothing and doesn't run out. `say` sits behind it, and only if the model
won't load at all.

⚠️ **the choice here is about timbre, not speed.** measured on this mac (m5 pro),
same text, so the price is on the table:

    engine                 "Oui ?"    31 s briefing    leaves the mac
    chatterbox (local)     3-4 s      ~45 s            no
    mistral (network)      0.51 s     2.86 s           **yes**
    kokoro (local, mlx)    0.065 s    1.70 s           no

kokoro is twenty times faster and chatterbox still won, decided by ear: an
assistant you hear all day is judged on timbre before milliseconds. **the
slowness barely shows** because the only two moments that matter are made in
advance — `prewarm()` caches the acknowledgements at startup, and the briefing
is built before anyone asks for it. only a brand-new sentence pays the rtf.

what did change is that **one engine no longer falls back to another**. the
`mistral -> chatterbox -> elevenlabs -> say` cascade meant a failed sentence came
out in a *different voice*: it looked like a bug in the timbre, it was a
fallback. the timbre you choose is the timbre you hear.

everything goes through a disk cache keyed on (text + engine + settings): a
sentence already said is never synthesised twice.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from ava import paths
from ava import net as net

CACHE_DIR = paths.cache_dir("ava_welcome")
DEFAULT_REFERENCE = paths.VOICES_DIR / "reference.wav"

ENGINES = ("kokoro", "mistral", "chatterbox", "elevenlabs", "system")
DEFAULT_ENGINE = "chatterbox"

# 82M parameters in 4 bits (~80 MB): it sits in memory without being felt,
# where chatterbox took several GB and 9 s to load.
KOKORO_MODEL = os.getenv("AVA_KOKORO_MODEL", "mlx-community/Kokoro-82M-4bit").strip()
KOKORO_DEFAULT_VOICE = "ff_siwis"
KOKORO_RATE = 24000

MISTRAL_TTS_MODEL = os.getenv("AVA_TTS_MODEL", "voxtral-mini-tts-latest").strip()
MISTRAL_BASE = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()
DEFAULT_MISTRAL_VOICE = "fr_marie_neutral"

# chatterbox goes off the rails on very long blocks, so we split by sentence.
MAX_CHUNK_CHARS = 280

_model = None
_model_lock = threading.Lock()
_model_error = ""


def _settings() -> dict:
    try:
        from ava.config import STORE
        return STORE.snapshot().get("voice", {})
    except Exception:  # noqa: BLE001
        return {}


def engine_name() -> str:
    value = str(_settings().get("engine", DEFAULT_ENGINE)).strip().lower()
    return value if value in ENGINES else DEFAULT_ENGINE


def reference_path() -> Path | None:
    """The voice clip chatterbox clones, if there is one.

    Reference clips are personal — mine is not in the repo and yours shouldn't
    be either. Drop any wav in `voices/` and it gets picked up; name it
    `reference.wav` if you keep several.
    """
    raw = str(_settings().get("reference", "")).strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_file() else None
    if DEFAULT_REFERENCE.is_file():
        return DEFAULT_REFERENCE
    clips = sorted(paths.VOICES_DIR.glob("*.wav")) if paths.VOICES_DIR.is_dir() else []
    return clips[0] if clips else None


def _params() -> tuple[float, float, float]:
    # settled on by ear and by comparing durations: cfg 0.35 holds the pace
    # better, temperature 0.65 keeps it from drifting.
    voice = _settings()

    def number(key, fallback, low, high):
        try:
            value = float(voice.get(key, fallback))
        except (TypeError, ValueError):
            value = fallback
        return max(low, min(high, value))

    return (number("exaggeration", 0.5, 0.2, 1.0),
            number("cfg_weight", 0.35, 0.1, 1.0),
            number("temperature", 0.65, 0.1, 1.2))


def _cache_path(text: str, suffix: str, *parts: str) -> Path:
    key = "|".join([text, *parts]).encode("utf-8")
    return CACHE_DIR / (hashlib.sha256(key).hexdigest()[:16] + suffix)


def split_sentences(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Cut on sentence boundaries, regrouping while it still fits."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", str(text or "").strip()) if p.strip()]
    chunks: list[str] = []
    for piece in pieces:
        while len(piece) > limit:
            # an endless sentence: cut at the last comma that helps.
            cut = piece.rfind(",", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            chunks.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if chunks and len(chunks[-1]) + len(piece) + 1 <= limit:
            chunks[-1] = f"{chunks[-1]} {piece}"
        elif piece:
            chunks.append(piece)
    return chunks or ([str(text).strip()] if str(text).strip() else [])


# below this, a sentence doesn't deserve its own network round trip — glue it
# onto the next one.
MIN_UNIT_CHARS = 30


def split_speech_units(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split sentence by sentence, without regrouping like `split_sentences`.

    For a remote engine it's all upside: the pieces go out in parallel (the
    briefing costs its longest sentence, not the sum), the cuts land on real
    sentence boundaries so the prosody doesn't suffer, and the transcript lines
    up to the word since we measure each sentence instead of estimating it.
    """
    units: list[str] = []
    for piece in split_sentences(text, limit):
        for sentence in re.split(r"(?<=[.!?…])\s+", piece):
            sentence = sentence.strip()
            if not sentence:
                continue
            # a fragment on its own ("Voilà.") isn't worth a call: stick it to
            # the previous one while it still fits under the limit.
            if (units and len(sentence) < MIN_UNIT_CHARS
                    and len(units[-1]) + len(sentence) + 1 <= limit):
                units[-1] = f"{units[-1]} {sentence}"
            else:
                units.append(sentence)
    return units


# --- chatterbox --------------------------------------------------------------

def _device() -> str:
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _real_import():
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    return ChatterboxMultilingualTTS


def _import_chatterbox(loader=_real_import, attempts: int = 4, pause: float = 1.5):
    """Import chatterbox, absorbing the race in transformers' own imports.

    transformers exposes its classes through a lazy module that isn't
    thread-safe: if ava is still importing her own modules while the voice loads
    in the background, you get "cannot import name LlamaModel". It's never
    permanent — a second later it works.
    """
    last = None
    for _attempt in range(attempts):
        try:
            return loader()
        except ImportError as exc:
            last = exc
            time.sleep(pause)
    raise last if last else ImportError("chatterbox not found")


def _load_model():
    """Load the model once (~8 s at startup, then it stays warm)."""
    global _model, _model_error
    with _model_lock:
        if _model is not None or _model_error:
            return _model
        try:
            import torch
            ChatterboxMultilingualTTS = _import_chatterbox()
            device = _device()
            # some kernels are still missing on mps: fall back to the cpu
            # rather than crashing mid-sentence.
            if device == "mps":
                os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            started = time.time()
            try:
                _model = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
            except TypeError:
                # the pypi build (v2) has no model selector.
                _model = ChatterboxMultilingualTTS.from_pretrained(device=device)
            print(f"[voix] chatterbox chargé sur {device} en {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            _model_error = str(exc)
            print(f"[voix] chatterbox indisponible : {exc}")
            _model = None
    return _model


# the model runs at roughly the pace of speech (rtf ~1.5 on mps), so a short
# answer costs a few seconds. these lines come up constantly — make them once at
# startup and they come out of the cache from then on.
WARM_PHRASES = (
    "Oui ?",
    "C'est fait.",
    "Tout de suite.",
    "Je m'en occupe.",
    "Je n'ai pas compris, tu peux répéter ?",
    "J'ouvre Spotify.",
    "Bonne journée !",
)


# the cache grows by one file per sentence never said twice: 12 MB after a
# morning, so several GB a year if nobody sweeps up.
CACHE_MAX_BYTES = 400 * 1024 * 1024
CACHE_MAX_AGE_DAYS = 30


def prune_cache(max_bytes: int = CACHE_MAX_BYTES, max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """Sweep up: old files first, then the oldest remaining while the folder is
    over the size we want. Returns the number of bytes freed."""
    if not CACHE_DIR.is_dir():
        return 0
    entries = []
    for path in CACHE_DIR.iterdir():
        if path.suffix not in {".wav", ".mp3", ".json"}:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    entries.sort()                      # oldest first
    cutoff = time.time() - max_age_days * 86400
    total = sum(size for _mtime, size, _path in entries)
    freed = 0

    def drop(path: Path, size: int) -> int:
        # the timing sidecar always follows its audio.
        gone = 0
        for victim in (path, path.with_suffix(".timing.json")):
            try:
                gone += victim.stat().st_size if victim is not path else size
                victim.unlink()
            except OSError:
                pass
        return gone

    for mtime, size, path in entries:
        if path.suffix == ".json":
            continue                    # goes with its audio
        if mtime < cutoff or total - freed > max_bytes:
            freed += drop(path, size)
    return freed


def prewarm(phrases=WARM_PHRASES) -> None:
    """Fill the cache with the everyday lines, in the background.

    On kokoro the model comes up in 0.2 s, but the **first** sentence still pays
    for building the french pipeline (~1 s, against 0.065 s after): that's
    exactly what doing it here, at startup, takes off the path.
    """
    engine = engine_name()

    def worker() -> None:
        try:
            prune_cache()
        except Exception:  # noqa: BLE001 - le menage ne doit jamais bloquer la voix
            pass
        if engine == "system":
            return
        if engine == "kokoro" and _load_kokoro() is None:
            return
        if engine == "chatterbox" and _load_model() is None:
            return
        for phrase in phrases:
            try:
                synthesize(phrase)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=worker, daemon=True).start()


WORDS_PER_MINUTE = 165          # debit mesure de la voix de reference
MAX_TRIES = 3


def expected_seconds(text: str) -> float:
    """A plausible duration for a sentence, to catch a generation going wrong."""
    words = len(str(text or "").split())
    return max(0.55, words / WORDS_PER_MINUTE * 60)


def _trim_and_level(wav, sr: int):
    """Trim the silence at the edges and even out the level.

    Without it, pieces of the same sentence come out at different volumes and
    the gaps pile up: to the ear, "the voice is glitching".
    """
    import torch

    mono = wav.abs().max(dim=0).values
    if mono.numel() == 0:
        return wav
    loud = (mono > max(1e-4, float(mono.max()) * 0.02)).nonzero()
    if loud.numel():
        keep = int(sr * 0.05)
        start = max(0, int(loud[0]) - keep)
        end = min(mono.numel(), int(loud[-1]) + keep)
        wav = wav[:, start:end]
    peak = float(wav.abs().max()) if wav.numel() else 0.0
    if peak > 0:
        wav = wav * (0.92 / peak)
    return torch.clamp(wav, -1.0, 1.0)


def acceptable_ratio(chunk: str) -> tuple[float, float]:
    """The duration window we accept, wider on short sentences.

    A three-word line can legitimately be delivered in half the average time;
    refusing it would cost three generations for nothing. On long sentences the
    average is reliable, and a gap does signal something going wrong.
    """
    words = len(str(chunk or "").split())
    if words <= 4:
        return 0.32, 2.3
    if words <= 10:
        return 0.45, 2.0
    return 0.6, 1.7


def _tries_for(chunk: str) -> int:
    # regenerating a long sentence is expensive, so two takes will do.
    return 2 if len(str(chunk or "").split()) > 15 else MAX_TRIES


def _generate_chunk(model, chunk: str, reference, exaggeration, cfg_weight, temperature):
    """Generate a piece, and try again if the duration makes no sense.

    Chatterbox sometimes free-wheels on very short text: "Oui ?" produced 3.3 s
    of babbling, then 0.76 s on the next take. We keep whichever take lands
    closest to the expected duration.
    """
    target = expected_seconds(chunk)
    low, high = acceptable_ratio(chunk)
    best = None
    best_gap = None
    for attempt in range(_tries_for(chunk)):
        wav = model.generate(
            chunk, language_id="fr",
            audio_prompt_path=str(reference) if reference else None,
            exaggeration=exaggeration, cfg_weight=cfg_weight,
            temperature=temperature,
        ).detach().cpu()
        wav = _trim_and_level(wav, model.sr)
        ratio = (wav.shape[-1] / model.sr) / target
        gap = abs(ratio - 1.0)
        if best_gap is None or gap < best_gap:
            best, best_gap = wav, gap
        if low <= ratio <= high:
            return best
        print(f"[voix] prise {attempt + 1} hors-cible (x{ratio:.2f}) : {chunk[:40]!r}")
    return best


def _write_timings(audio_path: Path, chunks: list[str], seconds: list[float],
                   *, gap_s: float = 0.0) -> None:
    """Write each sentence's real duration next to the audio.

    This is what lets the transcript follow the voice: without it, the overlay
    spreads the words linearly over the total duration and the text drifts on
    long sentences.
    """
    marks = []
    offset = 0.0
    for chunk, duration in zip(chunks, seconds):
        marks.append({"text": chunk, "start_ms": round(offset * 1000),
                      "ms": round(duration * 1000)})
        offset += duration + gap_s
    try:
        audio_path.with_suffix(".timing.json").write_text(
            json.dumps(marks, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def word_delays(audio_path: Path | None, text: str, total_ms: int) -> list[int]:
    """When each word appears, in milliseconds.

    Words are spread inside THEIR own sentence, not across the whole briefing: a
    short sentence followed by a long one no longer shifts everything.
    """
    words = str(text or "").split()
    if not words:
        return []
    marks = []
    if audio_path is not None:
        try:
            marks = json.loads(
                audio_path.with_suffix(".timing.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marks = []
    if not marks:
        step = max(24, total_ms / len(words))
        return [round(index * step) for index in range(len(words))]

    delays: list[int] = []
    for mark in marks:
        chunk_words = str(mark.get("text", "")).split()
        if not chunk_words:
            continue
        start = float(mark.get("start_ms", 0))
        span = max(1.0, float(mark.get("ms", 0)))
        step = span / len(chunk_words)
        delays.extend(round(start + index * step) for index in range(len(chunk_words)))
    # splitting into sentences may have eaten a space: realign on the number of
    # words actually shown.
    if len(delays) < len(words):
        last = delays[-1] if delays else 0
        delays.extend([last] * (len(words) - len(delays)))
    return delays[:len(words)]


def _chatterbox_audio(text: str) -> Path | None:
    reference = reference_path()
    exaggeration, cfg_weight, temperature = _params()
    path = _cache_path(text, ".wav", "chatterbox", str(reference or ""),
                       f"{exaggeration:.2f}", f"{cfg_weight:.2f}", f"{temperature:.2f}")
    if path.exists() and path.stat().st_size > 0:
        return path

    model = _load_model()
    if model is None:
        return None
    try:
        import torch
        import torchaudio

        waves = []
        spoken = []
        silence = torch.zeros(1, int(model.sr * 0.14))
        for chunk in split_sentences(text):
            wav = _generate_chunk(model, chunk, reference, exaggeration,
                                  cfg_weight, temperature)
            if wav is None:
                continue
            waves.append(wav)
            waves.append(silence)
            spoken.append(chunk)
        if not waves:
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part.wav")
        torchaudio.save(str(tmp), torch.cat(waves[:-1], dim=1), model.sr)
        tmp.replace(path)
        _write_timings(path, spoken, [w.shape[-1] / model.sr for w in waves[::2]],
                       gap_s=0.14)
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"[voix] chatterbox a échoué : {exc}")
        return None


# --- mistral (voxtral tts) ----------------------------------------------------

# the voice takes on the colour of what it says. three lines of code, because
# the catalogue already ships marie in six moods — no need to hand-roll pitch
# shifting behind it.
MOOD_VOICES = {
    "neutral": "fr_marie_neutral",
    "happy": "fr_marie_happy",
    "curious": "fr_marie_curious",
    "excited": "fr_marie_excited",
    "sad": "fr_marie_sad",
}

_GREETINGS = ("bonjour", "bonsoir", "bonne journée", "bonne soirée", "salut",
              "content", "bravo", "parfait", "super", "félicitations")
_APOLOGIES = ("désolée", "désolé", "je n'ai pas réussi", "en échec",
              "je n'ai pas compris", "impossible")


def mood_for(text: str) -> str:
    """Guess the mood that goes with the sentence.

    Deliberately conservative: when in doubt, stay neutral. An assistant who
    gets excited at the wrong moment rings falser than a flat one.
    """
    value = str(text or "").strip()
    if not value:
        return "neutral"
    low = value.lower()
    if any(word in low for word in _APOLOGIES):
        return "sad"
    if any(low.startswith(word) for word in _GREETINGS):
        return "happy"
    if value.endswith("?"):
        return "curious"
    return "neutral"


def mistral_voice(mood: str = "") -> str:
    """Which timbre to use: the one from the settings, coloured by the mood."""
    configured = str(_settings().get("mistral_voice", "")).strip() or DEFAULT_MISTRAL_VOICE
    if not _settings().get("expressive", True) or not mood:
        return configured
    return MOOD_VOICES.get(mood, configured)


def _mistral_key() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


def _mistral_chunk(text: str, voice: str, timeout: float = 20.0) -> bytes:
    """One http round trip, one piece of mp3. Raises on failure."""
    import requests

    response = requests.post(
        f"{MISTRAL_BASE}/audio/speech",
        headers={"Authorization": f"Bearer {_mistral_key()}",
                 "Content-Type": "application/json"},
        json={"model": MISTRAL_TTS_MODEL, "input": text, "voice": voice},
        timeout=net.timeout(timeout),
    )
    response.raise_for_status()
    import base64
    payload = response.json()
    data = payload.get("audio_data")
    if not data:
        raise RuntimeError(f"reponse tts sans audio : {str(payload)[:200]}")
    return base64.b64decode(data)


# started by the launch agent, ava inherits a minimal PATH
# (`/usr/bin:/bin:/usr/sbin:/sbin`) with no homebrew in it. so `ffmpeg` was
# nowhere to be found once ava started automatically — and since the concat
# fails silently, **every briefing longer than one sentence went mute**, while
# everything worked when she was started by hand from a terminal.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
_tools: dict[str, str] = {}


def tool_path(name: str) -> str:
    """The absolute path of a binary, whatever PATH we inherited."""
    if name in _tools:
        return _tools[name]
    import shutil
    found = shutil.which(name) or ""
    if not found:
        for directory in _TOOL_DIRS:
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found = str(candidate)
                break
    _tools[name] = found or name        # failing that, let the system try
    return _tools[name]


def _mp3_duration(path: Path) -> float:
    """The duration the header claims. Fast, but approximate."""
    try:
        out = subprocess.run(
            [tool_path("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10, check=False)
        return float(out.stdout.strip() or 0.0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


DECODE_RATE = 24000


def _decoded_duration(path: Path) -> float:
    """The exact duration, by actually decoding.

    The mp3 header undercounts: on a five-sentence briefing it said 26.61 s
    where decoding gives 28.42 s — and 28.42 s is what you really get after
    concatenation. Trusting the header therefore pushed the transcript almost
    two seconds off by the end.
    """
    try:
        out = subprocess.run(
            [tool_path("ffmpeg"), "-v", "error", "-i", str(path), "-f", "s16le",
             "-ac", "1", "-ar", str(DECODE_RATE), "-"],
            capture_output=True, timeout=60, check=False)
        samples = len(out.stdout) / 2
        return samples / DECODE_RATE if samples else _mp3_duration(path)
    except (OSError, subprocess.SubprocessError):
        return _mp3_duration(path)


def _concat_mp3(parts: list[Path], target: Path) -> bool:
    """Join the pieces into one stream, with no gap at the seams.

    A straight copy (`-f concat -c copy`) would be free, but every mp3 drags its
    lead-in and lead-out silence along: measured on a 5-sentence briefing, that
    added **1.6 s** overall, about 320 ms of dead air per seam. It chops up the
    pace, and the transcript — pinned to the pieces' durations — drifted by the
    same amount. The `concat` filter decodes and re-encodes once: the joins are
    clean and the durations add up again.
    """
    try:
        command = [tool_path("ffmpeg"), "-y", "-loglevel", "error"]
        for piece in parts:
            command += ["-i", str(piece)]
        streams = "".join(f"[{index}:a]" for index in range(len(parts)))
        command += [
            "-filter_complex", f"{streams}concat=n={len(parts)}:v=0:a=1[out]",
            "-map", "[out]", "-b:a", "128k", str(target),
        ]
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
        return result.returncode == 0 and target.exists() and target.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        return False


def _mistral_audio(text: str, mood: str = "") -> Path | None:
    if not _mistral_key():
        return None
    voice = mistral_voice(mood)
    path = _cache_path(text, ".mp3", "mistral", MISTRAL_TTS_MODEL, voice)
    if path.exists() and path.stat().st_size > 0:
        # the cache stays readable offline: it's the one voice that costs
        # nothing at all, so it must never be made to depend on the network.
        return path

    # without this guard, an offline six-sentence briefing paid the timeout six
    # times before dropping to the local fallback.
    if not net.reachable("voix"):
        return None

    chunks = split_speech_units(text)
    if not chunks:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # the pieces go out together: a six-sentence briefing takes as long as
        # its slowest sentence, not the sum of all six.
        if len(chunks) == 1:
            audio = [_mistral_chunk(chunks[0], voice)]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                audio = list(pool.map(lambda c: _mistral_chunk(c, voice), chunks))
    except Exception as exc:  # noqa: BLE001
        net.note_failure("voix", exc)
        print(f"[voix] mistral a échoué : {exc}")
        return None
    net.note_success("voix")

    tmp = path.with_suffix(".part.mp3")
    parts: list[Path] = []
    try:
        for index, blob in enumerate(audio):
            piece = path.with_suffix(f".p{index}.mp3")
            piece.write_bytes(blob)
            parts.append(piece)
        if len(parts) == 1:
            parts[0].replace(tmp)
        elif not _concat_mp3(parts, tmp):
            print("[voix] concaténation ffmpeg impossible")
            return None
        durations = ([_decoded_duration(p) for p in parts] if len(parts) > 1
                     else [_decoded_duration(tmp)])
        total = _decoded_duration(tmp)
        tmp.replace(path)
        # safety net: if a gap remains between the sum of the pieces and the
        # final file, spread it over the seams rather than letting the transcript
        # drift all the way to the last sentence.
        residual = total - sum(durations)
        gap = residual / (len(durations) - 1) if len(durations) > 1 and residual > 0 else 0.0
        _write_timings(path, chunks, durations, gap_s=gap)
        return path
    except OSError as exc:
        print(f"[voix] écriture du cache impossible : {exc}")
        return None
    finally:
        for piece in parts:
            piece.unlink(missing_ok=True)


# --- kokoro, le moteur local ---------------------------------------------------

_kokoro = None
_kokoro_lock = threading.Lock()
_kokoro_error = ""


def kokoro_voice() -> str:
    return str(_settings().get("kokoro_voice", "")).strip() or KOKORO_DEFAULT_VOICE


def _load_kokoro():
    """Load the local model once. ~0.2 s, against 8.9 s for chatterbox."""
    global _kokoro, _kokoro_error
    with _kokoro_lock:
        if _kokoro is not None or _kokoro_error:
            return _kokoro
        try:
            from mlx_audio.tts.utils import load_model
            started = time.time()
            _kokoro = load_model(KOKORO_MODEL)
            print(f"[voix] kokoro chargé en {time.time() - started:.2f}s")
        except Exception as exc:  # noqa: BLE001
            _kokoro_error = str(exc)
            print(f"[voix] kokoro indisponible : {exc}")
            _kokoro = None
    return _kokoro


def kokoro_ready() -> bool:
    return _kokoro is not None


def _kokoro_audio(text: str) -> Path | None:
    voice = kokoro_voice()
    path = _cache_path(text, ".wav", "kokoro", KOKORO_MODEL, voice)
    if path.exists() and path.stat().st_size > 0:
        return path
    model = _load_kokoro()
    if model is None:
        return None
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:
        print(f"[voix] kokoro : dépendance manquante ({exc})")
        return None

    # ⚠️ kokoro's french g2p goes through espeak, which **truncates long text**
    # for lack of internal splitting (it says so itself in a warning). so we cut
    # sentence by sentence — which also hands us each sentence's real duration
    # for lining the transcript up.
    chunks = split_speech_units(text)
    if not chunks:
        return None
    pieces: list = []
    seconds: list[float] = []
    started = time.time()
    try:
        for chunk in chunks:
            audio = [np.asarray(part.audio) for part in model.generate(
                text=chunk, voice=voice, lang_code="f", speed=1.0, verbose=False)]
            if not audio:
                continue
            joined = np.concatenate(audio)
            pieces.append(joined)
            seconds.append(len(joined) / KOKORO_RATE)
    except Exception as exc:  # noqa: BLE001
        print(f"[voix] kokoro a échoué : {exc}")
        return None
    if not pieces:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part.wav")
    try:
        sf.write(str(tmp), np.concatenate(pieces), KOKORO_RATE)
        tmp.replace(path)
    except OSError as exc:
        print(f"[voix] écriture du cache impossible : {exc}")
        return None
    _write_timings(path, chunks, seconds)
    if os.getenv("AVA_DEBUG") == "1":
        total = sum(seconds)
        spent = time.time() - started
        print(f"[voix] kokoro : {total:.1f}s d'audio en {spent:.2f}s "
              f"({total / spent:.0f}x le temps réel)")
    return path


# --- elevenlabs ---------------------------------------------------------------

def _eleven_audio(text: str) -> Path | None:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice = _settings()
    voice_id = str(voice.get("voice_id", "")).strip() or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    model_id = str(voice.get("model_id", "")).strip() or "eleven_multilingual_v2"
    if not key or not voice_id:
        return None
    path = _cache_path(text, ".mp3", voice_id, model_id)
    if path.exists() and path.stat().st_size > 0:
        return path
    if not net.reachable("voix:elevenlabs"):
        return None
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=key)
        audio = client.text_to_speech.convert(
            voice_id=voice_id, model_id=model_id, text=text,
            output_format="mp3_44100_128")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part.mp3")
        with open(tmp, "wb") as handle:
            for chunk in audio:
                if chunk:
                    handle.write(chunk)
        tmp.replace(path)
        net.note_success("voix:elevenlabs")
        return path
    except Exception as exc:  # noqa: BLE001
        net.note_failure("voix:elevenlabs", exc)
        message = str(exc)
        if "401" in message or "quota" in message.lower() or "402" in message:
            print("[voix] elevenlabs : plus de crédit -> bascule sur le moteur local.")
        else:
            print(f"[voix] elevenlabs : {exc}")
        return None


# --- api publique -------------------------------------------------------------

def is_cached(text: str, mood: str = "") -> bool:
    """Has this sentence already been made?

    Used for measurements: telling an answer that comes off disk (instant) from
    one that needs synthesising, or the average latency means nothing.
    """
    value = str(text or "").strip()
    if not value:
        return False
    engine = engine_name()
    if engine == "kokoro":
        path = _cache_path(value, ".wav", "kokoro", KOKORO_MODEL, kokoro_voice())
        return path.exists() and path.stat().st_size > 0
    if engine != "mistral" or not _mistral_key():
        return False
    voice = mistral_voice(mood or mood_for(value))
    path = _cache_path(value, ".mp3", "mistral", MISTRAL_TTS_MODEL, voice)
    return path.exists() and path.stat().st_size > 0


def local_voice_ready() -> bool:
    """Is the local model in memory already? (without loading it)"""
    return _model is not None


def warm_local_voice() -> None:
    """Bring the local voice up in the background, without holding anyone up."""
    if _model is not None or _model_error:
        return
    thread = threading.Thread(target=_load_model, daemon=True, name="ava-voix-locale")
    thread.start()


def synthesize(text: str, mood: str = "") -> Path | None:
    """Return an audio file afplay can play, or None to fall through to `say`.

    ⚠️ **one path only.** the old version chained four engines
    (mistral -> chatterbox -> elevenlabs -> say): each fallback looked free, but
    together they made the behaviour unpredictable — the voice changed timbre
    halfway through depending on who answered, and one network hiccup cost a
    cascade of timeouts before reaching the last. with a local engine faster
    than all the others there is nothing left to catch: `kokoro`, and `say` only
    if the model itself won't load.
    """
    value = str(text or "").strip()
    if not value:
        return None
    engine = engine_name()
    if engine == "system":
        return None
    if engine == "kokoro":
        return _kokoro_audio(value)
    if engine == "chatterbox":
        # ⚠️ no more jumping to mistral or elevenlabs: the timbre you chose is
        # the timbre you hear. a sentence chatterbox fumbled used to come out in
        # a *different* voice, and it looked like a bug in the voice itself.
        return _chatterbox_audio(value)
    if engine == "mistral":
        found = _mistral_audio(value, mood or mood_for(value))
        if found:
            return found
        # offline, chatterbox takes over: self-contained, but it has to be
        # loaded first. measured: 8.9 s to load then 9 s to synthesise, so
        # **18 s of silence** after an "ouvre spotify" — longer than anyone will
        # wait on a voice assistant. until it's warm the system voice answers
        # right away and the model comes up behind it; the sentences after get
        # the real voice back.
        if not local_voice_ready() and net.is_offline():
            warm_local_voice()
            return _eleven_audio(value)
        return _chatterbox_audio(value) or _eleven_audio(value)
    return _eleven_audio(value)


def speak_file(path: Path) -> None:
    subprocess.run([tool_path("afplay"), str(path)], check=False)


def say_fallback(text: str, system_voice: str = "Thomas") -> None:
    subprocess.run([tool_path("say"), "-v", system_voice, text], check=False)
