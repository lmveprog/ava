"""la voix d'ava : **un moteur, et un filet**.

`chatterbox` (defaut) tourne sur le gpu du mac. il ne sort jamais de la machine,
ne coute rien et ne s'epuise pas. `say` reste derriere, uniquement si le modele
ne se charge pas du tout.

⚠️ **le choix ici est un choix de timbre, pas de vitesse.** mesures sur ce mac
(m5 pro), meme texte, pour que le prix soit connu :

    moteur                 « Oui ? »   briefing de 31 s   sort du mac
    chatterbox (local)     3-4 s       ~45 s              non
    mistral (reseau)       0,51 s      2,86 s             **oui**
    kokoro (local, mlx)    0,065 s     1,70 s             non

kokoro est vingt fois plus rapide et Matheus a tranche pour chatterbox apres
avoir compare a l'oreille : une assistante qu'on entend toute la journee se juge
au timbre avant la milliseconde. **la lenteur ne se voit presque pas** parce que
les deux seuls moments qui comptent sont pre-fabriques : `prewarm()` met les
accuses de reception en cache au demarrage, et le briefing est prepare avant
d'etre demande. seule une phrase inedite paie le rtf.

ce qui a change en revanche, c'est qu'**un moteur ne retombe plus sur un autre**.
la cascade `mistral -> chatterbox -> elevenlabs -> say` faisait sortir une phrase
ratee avec une *autre voix* : on croyait a un bug du timbre, c'etait un repli.
le timbre choisi est le timbre entendu.

tout passe par un cache disque indexe sur (texte + moteur + reglages) : une
phrase deja dite ne se resynthetise jamais.
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

# 82 M de parametres en 4 bits (~80 Mo) : il tient en memoire sans se faire
# sentir, la ou chatterbox mobilisait plusieurs Go et 9 s de chargement.
KOKORO_MODEL = os.getenv("AVA_KOKORO_MODEL", "mlx-community/Kokoro-82M-4bit").strip()
KOKORO_DEFAULT_VOICE = "ff_siwis"
KOKORO_RATE = 24000

MISTRAL_TTS_MODEL = os.getenv("AVA_TTS_MODEL", "voxtral-mini-tts-latest").strip()
MISTRAL_BASE = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()
DEFAULT_MISTRAL_VOICE = "fr_marie_neutral"

# chatterbox part en vrille sur les tres longs blocs : on decoupe par phrases.
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
    # valeurs retenues apres comparaison a l'oreille et sur les durees :
    # cfg 0.35 tient mieux le debit, temperature 0.65 limite les derives.
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
    """Coupe aux frontieres de phrases, en regroupant tant que ca tient."""
    pieces = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", str(text or "").strip()) if p.strip()]
    chunks: list[str] = []
    for piece in pieces:
        while len(piece) > limit:
            # phrase interminable : on coupe a la derniere virgule utile.
            cut = piece.rfind(",", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            chunks.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if chunks and len(chunks[-1]) + len(piece) + 1 <= limit:
            chunks[-1] = f"{chunks[-1]} {piece}"
        elif piece:
            chunks.append(piece)
    return chunks or ([str(text).strip()] if str(text).strip() else [])


# en dessous de ce seuil, une phrase ne merite pas son propre aller-retour
# reseau : on la recolle a la suivante.
MIN_UNIT_CHARS = 30


def split_speech_units(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Decoupe phrase par phrase, sans regrouper comme `split_sentences`.

    Pour un moteur distant c'est tout benefice : les morceaux partent en
    parallele (le briefing coute le temps de sa plus longue phrase, pas la
    somme), les coupures tombent sur de vraies frontieres de phrase donc la
    prosodie ne souffre pas, et le transcript se cale au mot pres puisqu'on
    mesure la duree reelle de chaque phrase au lieu de l'estimer.
    """
    units: list[str] = []
    for piece in split_sentences(text, limit):
        for sentence in re.split(r"(?<=[.!?…])\s+", piece):
            sentence = sentence.strip()
            if not sentence:
                continue
            # une bribe toute seule ("Voilà.") ne vaut pas un appel : on la colle
            # a la precedente tant que ca tient dans la limite.
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
    """Importe chatterbox, en encaissant la course a l'import de transformers.

    transformers expose ses classes par un module paresseux qui n'est pas
    thread-safe : si ava importe encore ses propres modules pendant qu'on
    charge la voix en fond, on se prend un « cannot import name LlamaModel ».
    Ce n'est jamais definitif — une seconde plus tard, ca passe.
    """
    last = None
    for _attempt in range(attempts):
        try:
            return loader()
        except ImportError as exc:
            last = exc
            time.sleep(pause)
    raise last if last else ImportError("chatterbox introuvable")


def _load_model():
    """Charge le modele une seule fois (~8 s au demarrage, puis il reste chaud)."""
    global _model, _model_error
    with _model_lock:
        if _model is not None or _model_error:
            return _model
        try:
            import torch
            ChatterboxMultilingualTTS = _import_chatterbox()
            device = _device()
            # sur mps, certains noyaux manquent encore : on retombe sur le cpu
            # au lieu de planter en pleine phrase.
            if device == "mps":
                os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            started = time.time()
            try:
                _model = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
            except TypeError:
                # version pypi (v2) : pas de selecteur de modele.
                _model = ChatterboxMultilingualTTS.from_pretrained(device=device)
            print(f"[voix] chatterbox chargé sur {device} en {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            _model_error = str(exc)
            print(f"[voix] chatterbox indisponible : {exc}")
            _model = None
    return _model


# le modele tourne a peu pres au rythme de la parole (rtf ~1,5 sur mps) : une
# reponse courte coute donc quelques secondes. ces phrases-la reviennent tout le
# temps, on les fabrique une fois au demarrage et elles sortent ensuite du cache.
WARM_PHRASES = (
    "Oui ?",
    "C'est fait.",
    "Tout de suite.",
    "Je m'en occupe.",
    "Je n'ai pas compris, tu peux répéter ?",
    "J'ouvre Spotify.",
    "Bonne journée !",
)


# le cache grossit d'un fichier par phrase jamais dite deux fois : 12 Mo apres
# une matinee, donc plusieurs Go a l'annee si personne ne balaie.
CACHE_MAX_BYTES = 400 * 1024 * 1024
CACHE_MAX_AGE_DAYS = 30


def prune_cache(max_bytes: int = CACHE_MAX_BYTES, max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """Fait le menage : d'abord les vieux fichiers, puis les plus anciens tant
    que le dossier depasse la taille voulue. Renvoie le nombre d'octets liberes."""
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
    entries.sort()                      # les plus vieux d'abord
    cutoff = time.time() - max_age_days * 86400
    total = sum(size for _mtime, size, _path in entries)
    freed = 0

    def drop(path: Path, size: int) -> int:
        # le sidecar de timings suit toujours son audio.
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
            continue                    # emporte avec son audio
        if mtime < cutoff or total - freed > max_bytes:
            freed += drop(path, size)
    return freed


def prewarm(phrases=WARM_PHRASES) -> None:
    """Remplit le cache des phrases courantes, en fond.

    Sur kokoro le modele monte en 0,2 s, mais la **premiere** phrase paie encore
    la construction du pipeline francais (~1 s, contre 0,065 s ensuite) : c'est
    exactement ce qu'on retire du chemin en le faisant ici, au demarrage.
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
    """Duree plausible d'une phrase, pour reperer une generation qui derape."""
    words = len(str(text or "").split())
    return max(0.55, words / WORDS_PER_MINUTE * 60)


def _trim_and_level(wav, sr: int):
    """Coupe les silences de bord et egalise le niveau.

    Sans ca, les morceaux d'une meme phrase n'ont pas le meme volume et les
    blancs s'accumulent : a l'oreille, ca fait « la voix bugue ».
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
    """Fenetre de duree toleree, plus large sur les phrases courtes.

    Une phrase de trois mots peut legitimement etre expediee en deux fois moins
    de temps que la moyenne ; la refuser ferait payer trois generations pour
    rien. Sur les phrases longues au contraire, la moyenne est fiable et un
    ecart signale une vraie derive.
    """
    words = len(str(chunk or "").split())
    if words <= 4:
        return 0.32, 2.3
    if words <= 10:
        return 0.45, 2.0
    return 0.6, 1.7


def _tries_for(chunk: str) -> int:
    # rejouer une longue phrase coute cher : on se contente de deux prises.
    return 2 if len(str(chunk or "").split()) > 15 else MAX_TRIES


def _generate_chunk(model, chunk: str, reference, exaggeration, cfg_weight, temperature):
    """Genere un morceau, et recommence si la duree n'a aucun sens.

    Chatterbox part parfois en roue libre sur les textes tres courts : « Oui ? »
    a donne 3,3 s de babillage puis 0,76 s au coup suivant. On garde la prise
    dont la duree colle le mieux a ce qu'on attend.
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
    """Note la duree reelle de chaque phrase a cote de l'audio.

    C'est ce qui permet au transcript de suivre la voix : sans ca, l'overlay
    etale les mots lineairement sur la duree totale et le texte derive dans les
    phrases longues.
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
    """Instant d'apparition de chaque mot, en millisecondes.

    On repartit les mots a l'interieur de LEUR phrase, pas sur tout le
    briefing : une phrase courte suivie d'une longue ne decale plus tout.
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
    # le decoupage en phrases peut avoir avale un espace : on recale sur le
    # nombre de mots reellement affiches.
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

# la voix prend la couleur de ce qu'elle dit. c'est trois lignes de code parce
# que le catalogue expose deja marie en six humeurs — inutile de bricoler du
# pitch shifting derriere.
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
    """Devine l'humeur qui va avec la phrase.

    Volontairement conservateur : dans le doute on reste neutre. Une assistante
    qui s'enthousiasme a contretemps sonne plus faux qu'une assistante plate.
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
    """Le timbre a utiliser : celui des reglages, module par l'humeur."""
    configured = str(_settings().get("mistral_voice", "")).strip() or DEFAULT_MISTRAL_VOICE
    if not _settings().get("expressive", True) or not mood:
        return configured
    return MOOD_VOICES.get(mood, configured)


def _mistral_key() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


def _mistral_chunk(text: str, voice: str, timeout: float = 20.0) -> bytes:
    """Un aller-retour http, un morceau de mp3. Leve en cas d'echec."""
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


# lance par le launchagent, ava herite d'un PATH minimal
# (`/usr/bin:/bin:/usr/sbin:/sbin`) : homebrew n'y est pas. `ffmpeg` etait donc
# introuvable une fois ava demarree automatiquement — et comme le recollage rate
# en silence, **tout briefing de plus d'une phrase devenait muet**, alors que
# tout marchait quand on la lancait a la main depuis un terminal.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
_tools: dict[str, str] = {}


def tool_path(name: str) -> str:
    """Le chemin absolu d'un binaire, quel que soit le PATH herite."""
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
    _tools[name] = found or name        # a defaut, on laisse le systeme essayer
    return _tools[name]


def _mp3_duration(path: Path) -> float:
    """Duree annoncee par l'en-tete. Rapide, mais approximative."""
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
    """Duree exacte, en decodant vraiment.

    L'en-tete mp3 sous-estime : sur un briefing de cinq phrases elle donnait
    26,61 s la ou le decodage donne 28,42 s — et c'est bien 28,42 s qu'on
    obtient apres recollage. Utiliser l'en-tete decalait donc le transcript de
    presque deux secondes sur la fin.
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
    """Recolle les morceaux en un seul flux, sans blanc aux jointures.

    La copie directe (`-f concat -c copy`) serait gratuite mais chaque mp3 traine
    son silence d'amorce et de fin : mesure sur un briefing de 5 phrases, ca
    ajoutait **1,6 s** au total, soit ~320 ms de blanc par jointure. A l'oreille
    ca hache le debit, et le transcript — cale sur la duree des morceaux —
    derivait d'autant. Le filtre `concat` decode puis reencode une seule fois :
    la soudure est franche et les durees redeviennent additives.
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
        # le cache reste lisible hors ligne : c'est la seule voix qui ne coute
        # rien du tout, il ne faut surtout pas la faire dependre du reseau.
        return path

    # sans cette garde, un briefing de six phrases hors ligne payait six fois le
    # timeout avant de tomber sur le repli local.
    if not net.reachable("voix"):
        return None

    chunks = split_speech_units(text)
    if not chunks:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # les morceaux partent ensemble : un briefing de six phrases se fabrique
        # dans le temps de la plus lente, pas dans la somme des six.
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
        # filet : s'il reste un ecart entre la somme des morceaux et le fichier
        # final, on l'etale sur les jointures plutot que de laisser le
        # transcript deriver jusqu'a la derniere phrase.
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
    """Charge le modele local une fois. ~0,2 s, contre 8,9 s pour chatterbox."""
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

    # ⚠️ le g2p francais de kokoro passe par espeak, qui **tronque les longs
    # textes** faute de decoupage interne (il le dit lui-meme dans un warning).
    # on decoupe donc phrase par phrase — ce qui donne au passage la duree
    # reelle de chaque phrase pour caler le transcript.
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
    """Cette phrase a-t-elle deja ete fabriquee ?

    Sert aux mesures : distinguer une reponse qui sort du disque (instantanee)
    d'une qui demande une synthese, sinon la latence moyenne ne veut rien dire.
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
    """Le modele local est-il deja en memoire ? (sans le charger)"""
    return _model is not None


def warm_local_voice() -> None:
    """Monte la voix locale en tache de fond, sans faire attendre personne."""
    if _model is not None or _model_error:
        return
    thread = threading.Thread(target=_load_model, daemon=True, name="ava-voix-locale")
    thread.start()


def synthesize(text: str, mood: str = "") -> Path | None:
    """Rend un fichier audio jouable par afplay, ou None pour passer a `say`.

    ⚠️ **un seul chemin.** l'ancienne version enchainait quatre moteurs
    (mistral -> chatterbox -> elevenlabs -> say) : chaque repli avait l'air
    gratuit, mais ensemble ils rendaient le comportement imprevisible — la voix
    changeait de timbre en cours de route selon qui avait repondu, et un incident
    reseau se payait en cascade de timeouts avant d'arriver au dernier. avec un
    moteur local plus rapide que tous les autres, il n'y a plus rien a rattraper :
    `kokoro`, et `say` seulement si le modele lui-meme ne se charge pas.
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
        # ⚠️ plus de saut vers mistral ni elevenlabs : le timbre choisi est le
        # timbre entendu. avant, une phrase que chatterbox ratait sortait avec
        # une *autre* voix, et on croyait a un bug de la voix elle-meme.
        return _chatterbox_audio(value)
    if engine == "mistral":
        found = _mistral_audio(value, mood or mood_for(value))
        if found:
            return found
        # hors ligne, chatterbox prend le relais : autonome, mais il faut
        # d'abord le monter en memoire. mesure : 8,9 s de chargement puis 9 s de
        # synthese, soit **18 s de silence** apres un « ouvre spotify » — le
        # temps que personne n'accepte d'attendre d'une assistante vocale. tant
        # qu'il n'est pas chaud, la voix systeme repond tout de suite et le
        # modele monte derriere ; les phrases suivantes retrouvent la vraie voix.
        if not local_voice_ready() and net.is_offline():
            warm_local_voice()
            return _eleven_audio(value)
        return _chatterbox_audio(value) or _eleven_audio(value)
    return _eleven_audio(value)


def speak_file(path: Path) -> None:
    subprocess.run([tool_path("afplay"), str(path)], check=False)


def say_fallback(text: str, system_voice: str = "Thomas") -> None:
    subprocess.run([tool_path("say"), "-v", system_voice, text], check=False)
