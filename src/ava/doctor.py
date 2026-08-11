#!/usr/bin/env python3
"""a read-only health check on a local ava install."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import requests



# without this the check reported keys as missing while ava herself read them
# perfectly well — the kind of false negative that sends you hunting for a fault
# that isn't there.
try:
    from dotenv import load_dotenv
    load_dotenv(paths.ENV_FILE)
except Exception:  # noqa: BLE001
    pass

from ava import paths
from ava.config import STORE  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def run_checks() -> list[Check]:
    checks: list[Check] = []
    version_ok = sys.version_info >= (3, 11)
    checks.append(Check(
        "python", "ok" if version_ok else "error",
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))

    for module in (
        "numpy", "sounddevice", "dotenv", "requests", "vosk",
        "faster_whisper", "webview", "elevenlabs",
    ):
        ok = importlib.util.find_spec(module) is not None
        checks.append(Check(f"module:{module}", "ok" if ok else "error", "installe" if ok else "absent"))

    for command in ("open", "osascript", "say", "afplay", "screencapture"):
        path = shutil.which(command)
        checks.append(Check(f"commande:{command}", "ok" if path else "error", path or "introuvable"))

    for command, purpose in (("ollama", "analyse d'ecran locale"), ("tesseract", "secours OCR")):
        path = shutil.which(command)
        checks.append(Check(
            f"commande:{command}", "ok" if path else "warning",
            path or f"optionnel — {purpose} indisponible",
            required=False,
        ))

    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=2)
        response.raise_for_status()
        models = [str(item.get("name", "")) for item in response.json().get("models", [])]
        vision = next((name for name in models if any(
            token in name.lower() for token in ("gemma3", "qwen3-vl", "qwen2.5vl", "llama3.2-vision", "llava")
        )), "")
        checks.append(Check(
            "vision:locale", "ok" if vision else "warning",
            vision or "Ollama actif, mais aucun modele vision detecte",
            required=False,
        ))
    except Exception:
        checks.append(Check(
            "vision:locale", "warning",
            "Ollama non joignable — lance l'application Ollama",
            required=False,
        ))

    model = paths.MODELS_DIR / "vosk-model-small-fr-0.22"
    checks.append(Check(
        "modele:vosk-fr", "ok" if model.is_dir() else "error",
        str(model) if model.is_dir() else "modele absent — lance python3 bootstrap.py",
    ))

    overlay = paths.OVERLAY_HTML
    checks.append(Check(
        "overlay", "ok" if overlay.is_file() else "error",
        str(overlay) if overlay.is_file() else "overlay/ava.html absent",
    ))

    config = STORE.snapshot()
    checks.append(Check(
        "configuration", "warning" if STORE.last_error else "ok",
        STORE.last_error or f"schema {config['schema_version']}, ville {config['identity']['city']}",
    ))

    env_text = ""
    env_path = paths.ENV_FILE
    if env_path.exists():
        try:
            env_text = env_path.read_text(encoding="utf-8")
        except OSError:
            pass
    has_key = "ELEVENLABS_API_KEY=" in env_text and not any(
        line.strip() == "ELEVENLABS_API_KEY=" for line in env_text.splitlines()
    )
    checks.append(Check(
        "voix:elevenlabs", "ok" if has_key else "warning",
        "cle presente" if has_key else "cle absente, voix macOS utilisee",
        required=False,
    ))

    def env_has(name: str) -> bool:
        return any(
            line.strip().startswith(f"{name}=") and line.strip() != f"{name}="
            for line in env_text.splitlines()
        )

    mails_ok = env_has("GMAIL_USER") and env_has("GMAIL_APP_PASSWORD")
    checks.append(Check(
        "mails:imap", "ok" if mails_ok else "warning",
        "identifiants presents (lecture a la voix)" if mails_ok
        else "GMAIL_USER / GMAIL_APP_PASSWORD absents : ava ouvrira gmail web",
        required=False,
    ))

    try:
        import os as _os
        from ava.services.obsidian import ObsidianMemory
        vault = Path(_os.getenv("AVA_VAULT_DIR",
                                str(Path.home() / "Documents" / "AvaVault")))
        ObsidianMemory(vault).ensure()
        checks.append(Check("memoire:vault", "ok", str(vault), required=False))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("memoire:vault", "warning", str(exc), required=False))

    turn_model = paths.MODELS_DIR / "smart-turn-v3.2-cpu.onnx"
    checks.append(Check(
        "audio:smart-turn", "ok" if turn_model.exists() else "warning",
        "fin de tour semantique active" if turn_model.exists()
        else "modele absent : endpointing au silence fixe (plus lent)",
        required=False,
    ))

    try:
        access = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get UI elements enabled'],
            capture_output=True, text=True, timeout=5, check=False,
        )
        enabled = access.returncode == 0 and access.stdout.strip().lower() == "true"
        checks.append(Check(
            "permission:accessibilite", "ok" if enabled else "warning",
            "activee" if enabled else "a autoriser pour Computer Use",
            required=False,
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("permission:accessibilite", "warning", str(exc), required=False))

    try:
        import sounddevice as sd
        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        checks.append(Check(
            "micro", "ok" if inputs else "error",
            f"{len(inputs)} entree(s) audio detectee(s)",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("micro", "error", str(exc)))

    checks.extend(_network_checks())
    checks.extend(_voice_checks())
    checks.extend(_google_checks())
    checks.extend(_understanding_checks())
    checks.extend(_skills_checks())
    checks.extend(_promethee_checks())
    return checks


def _voice_checks() -> list[Check]:
    """The local voice: three quiet dependencies, three known traps."""
    checks: list[Check] = []
    voice = STORE.snapshot().get("voice", {})
    engine = voice.get("engine", "kokoro")
    checks.append(Check(
        "voix:moteur", "ok" if engine in ("kokoro", "chatterbox", "system") else "warning",
        engine if engine in ("kokoro", "chatterbox", "system")
        else f"{engine} — sort du mac, dépend d'une clé et d'un quota",
        required=False,
    ))
    if engine == "kokoro":
        return checks + _kokoro_checks(voice)
    if engine == "mistral":
        return checks + _mistral_voice_checks(voice)
    if engine != "chatterbox":
        return checks

    ok = importlib.util.find_spec("chatterbox") is not None
    checks.append(Check("voix:chatterbox", "ok" if ok else "error",
                        "installe" if ok else "pip install git+https://github.com/resemble-ai/chatterbox.git"))

    # perth imports pkg_resources: without setuptools<81 the watermarker comes
    # back None and the model refuses to build, with a baffling message.
    try:
        import perth
        armed = getattr(perth, "PerthImplicitWatermarker", None) is not None
        checks.append(Check(
            "voix:perth", "ok" if armed else "error",
            "pret" if armed else "pkg_resources manquant -> pip install 'setuptools<81'",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("voix:perth", "error", str(exc)[:90]))

    try:
        import torch
        if torch.backends.mps.is_available():
            device, status = "mps (gpu du mac)", "ok"
        elif torch.cuda.is_available():
            device, status = "cuda", "ok"
        else:
            device, status = "cpu (lent : ~5x le temps reel)", "warning"
        checks.append(Check("voix:calcul", status, device, required=False))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("voix:calcul", "error", str(exc)[:90]))

    try:
        from ava.audio import voice_tts as voice_tts
        reference = voice_tts.reference_path()
        checks.append(Check(
            "voix:reference", "ok" if reference else "warning",
            str(reference) if reference else "aucun extrait -> voix par defaut du modele",
            required=False,
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("voix:reference", "warning", str(exc)[:90], required=False))
    return checks


def _kokoro_checks(voice: dict) -> list[Check]:
    """The local engine: three pieces, and nothing leaving the mac."""
    checks: list[Check] = []
    for module, remede in (("mlx_audio", "pip install mlx-audio"),
                           ("misaki", "pip install misaki"),
                           ("espeakng_loader", "pip install espeakng-loader")):
        present = importlib.util.find_spec(module) is not None
        checks.append(Check(f"voix:{module}", "ok" if present else "error",
                            "installe" if present else remede))
    # ⚠️ the french g2p goes through espeak: without it kokoro goes silent in
    # french while speaking english beautifully — a mute, disorienting failure.
    espeak = shutil.which("espeak-ng") or shutil.which("espeak") or ""
    checks.append(Check("voix:espeak", "ok" if espeak else "error",
                        espeak or "brew install espeak-ng (le français passe par lui)"))
    checks.append(Check("voix:timbre", "ok",
                        str(voice.get("kokoro_voice", "ff_siwis")), required=False))
    return checks


def _mistral_voice_checks(voice: dict) -> list[Check]:
    """The remote voice: a key, a timbre, and ffmpeg to join the pieces.

    The trap of the day: without ffmpeg a short sentence works (one piece) but
    the morning briefing never comes out — that one needs five joined together.
    The error only surfaces at the first "bonjour ava", so we look for it here.
    """
    checks: list[Check] = []
    key = os.getenv("MISTRAL_API_KEY", "").strip()
    checks.append(Check(
        "voix:cle", "ok" if key else "error",
        "presente" if key else "MISTRAL_API_KEY absente du .env",
    ))

    timbre = str(voice.get("mistral_voice", "")).strip()
    from ava.config import MISTRAL_VOICES
    checks.append(Check(
        "voix:timbre", "ok" if timbre in MISTRAL_VOICES else "warning",
        timbre or "aucun -> fr_marie_neutral par defaut", required=False,
    ))
    checks.append(Check(
        "voix:humeur", "ok",
        "activee" if voice.get("expressive", True) else "desactivee (timbre unique)",
        required=False,
    ))

    for command in ("ffmpeg", "ffprobe"):
        path = shutil.which(command)
        checks.append(Check(
            f"voix:{command}", "ok" if path else "error",
            path or "absent -> les phrases longues ne pourront pas etre recollees",
        ))

    if key:
        try:
            response = requests.get(
                "https://api.mistral.ai/v1/audio/voices",
                headers={"Authorization": f"Bearer {key}"}, timeout=6)
            ok = response.status_code == 200
            checks.append(Check(
                "voix:api", "ok" if ok else "error",
                "joignable" if ok else f"http {response.status_code}", required=False,
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("voix:api", "warning", str(exc)[:80], required=False))
    return checks


def _skills_checks() -> list[Check]:
    """The installed skills, in the Agent Skills format."""
    checks: list[Check] = []
    from ava.brain import skills as skills
    enabled = STORE.snapshot().get("skills", {}).get("enabled", True)
    installed = skills.discover() if enabled else []
    checks.append(Check(
        "competences", "ok" if enabled else "warning",
        f"{len(installed)} installée(s)" if enabled else "désactivées dans les réglages",
        required=False,
    ))
    for skill in installed:
        script = skill.script()
        # a skill advertising a script that isn't there will say nothing on the
        # day it's called, so we'd rather know now.
        broken = bool(skill.command) and script is None
        checks.append(Check(
            f"competence:{skill.name}", "error" if broken else "ok",
            "script introuvable ou hors du dossier" if broken
            else (str(script.name) if script else "instructions seules"),
            required=False,
        ))
    return checks


def _understanding_checks() -> list[Check]:
    """The intent router: optional, but we want to know whether it answers."""
    checks: list[Check] = []
    from ava.brain import understanding as understanding
    checks.append(Check("comprehension:modele", "ok", understanding.MODEL, required=False))
    checks.append(Check(
        "comprehension:cle", "ok" if understanding.api_key() else "warning",
        "presente" if understanding.api_key()
        else "absente -> ava retombe sur le routage par mots-cles",
        required=False,
    ))
    learned = len(understanding.ROUTER._cache)
    checks.append(Check(
        "comprehension:apprises", "ok", f"{learned} tournures en cache", required=False))
    return checks


def _network_checks() -> list[Check]:
    """What an outage costs, and whether the local fallbacks are there.

    Four subsystems leave the mac (voice, weather, news, calendar). Without a
    breaker they each paid their full timeout — minutes of silence over nothing
    more than a dead wifi, which is why this check exists.
    """
    from ava import net as net
    state = net.status()
    checks = [Check(
        "reseau:etat", "ok" if state["online"] else "warning",
        "en ligne" if state["online"]
        else f"replis locaux encore {state['seconds_left']:.0f} s "
             f"(dernier echec : {state['last_failure']})",
        required=False,
    )]
    checks.append(Check(
        "reseau:detection", "ok",
        f"connexion coupee a {net.CONNECT_TIMEOUT_S:.0f} s, "
        f"fenetre hors-ligne {net.OFFLINE_WINDOW_S:.0f} s",
        required=False,
    ))
    # with no fallback an outage leaves ava completely mute: it's the one link
    # whose absence only shows at the worst possible moment.
    local_voice = importlib.util.find_spec("chatterbox") is not None
    checks.append(Check(
        "reseau:voix-de-repli", "ok" if local_voice else "warning",
        "chatterbox local installe" if local_voice
        else "aucune voix locale -> repli sur la voix systeme `say`",
        required=False,
    ))
    return checks


def _google_checks() -> list[Check]:
    """The calendar connector fails quietly when a link is missing."""
    try:
        from ava.services.google_auth import AUTH
        status = AUTH.status()
    except Exception as exc:  # noqa: BLE001
        return [Check("google:agenda", "warning", str(exc)[:90], required=False)]

    if not status.get("configured"):
        return [Check("google:agenda", "warning",
                      "pas d'identifiants oauth (reglages > google agenda)", required=False)]
    if not status.get("connected"):
        return [Check("google:agenda", "warning",
                      "identifiants presents, compte non connecte", required=False)]

    checks = [Check("google:compte", "ok", status.get("email", "connecte"), required=False)]
    # the house special: google hands over a token but drops the calendar scope
    # when the api is disabled or the scope unregistered. without this check ava
    # looks connected and answers "je n'arrive pas a lire ton agenda".
    try:
        import json as _json
        from ava.services.google_auth import TOKEN_PATH
        granted = _json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("scope", "")
    except Exception:  # noqa: BLE001
        granted = ""
    has_calendar = "auth/calendar" in granted
    checks.append(Check(
        "google:scope", "ok" if has_calendar else "error",
        "lecture/ecriture agenda" if has_calendar else
        "scope calendar refuse -> activer l'API Calendar ET l'enregistrer dans "
        "google auth platform > acces aux donnees, puis se reconnecter",
        required=False,
    ))
    return checks


def _promethee_checks() -> list[Check]:
    """The wake-up drives Promethee through accessibility: two prerequisites."""
    checks: list[Check] = []
    installed = Path("/Applications/Promethee.app").exists()
    checks.append(Check("promethee:app", "ok" if installed else "warning",
                        "installee" if installed else "absente, le reveil sautera la session",
                        required=False))
    if not installed:
        return checks
    ok = importlib.util.find_spec("ApplicationServices") is not None
    checks.append(Check(
        "promethee:accessibilite", "ok" if ok else "error",
        "pyobjc pret" if ok else "pip install pyobjc-framework-ApplicationServices",
        required=False,
    ))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="check a local ava install")
    parser.add_argument("--json", action="store_true", help="json output")
    parser.add_argument("--traces", action="store_true",
                        help="where the time goes, according to the log book")
    args = parser.parse_args()
    checks = run_checks()
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    else:
        icons = {"ok": "✓", "warning": "!", "error": "✗"}
        for check in checks:
            print(f"{icons[check.status]} {check.name:<24} {check.detail}")
        if args.traces:
            from ava import traces as traces
            print("\n— temps par route —")
            print(traces.report("commande"))
            print("\n— fabrication de la voix —")
            print(traces.report("voix"))
    return 1 if any(c.required and c.status == "error" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
