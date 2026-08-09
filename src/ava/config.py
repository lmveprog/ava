"""configuration locale, validee et sauvegardee atomiquement pour ava."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable
import urllib.parse

from ava import paths


# les voix francaises du tts mistral : « marie » est la seule fr_FR du
# catalogue, declinee en six humeurs. tout le reste est anglophone, donc
# inutilisable ici — d'ou la liste en dur plutot qu'un appel reseau au demarrage.
MISTRAL_VOICES = {
    "fr_marie_neutral", "fr_marie_happy", "fr_marie_curious",
    "fr_marie_excited", "fr_marie_sad", "fr_marie_angry",
}

# les voix francaises de kokoro (apache 2.0). `ff_siwis` est la seule voix fr_FR
# du catalogue, entrainee sur du francais natif — les autres sont anglophones.
KOKORO_VOICES = {"ff_siwis"}

# le moteur reste **local** : ni cle, ni quota, ni latence reseau. entre les deux
# voix locales, kokoro est vingt fois plus rapide mais c'est chatterbox qui a ete
# retenu — a l'oreille. une assistante qu'on entend toute la journee se juge au
# timbre avant la milliseconde, et sa lenteur ne se voit presque pas : les accuses
# de reception et le briefing sont fabriques d'avance.
DEFAULT_VOICE_ENGINE = "chatterbox"

# les quatre quadrants, plus une place a part : `small` pose une petite fenetre
# en bas a droite. Spotify n'a aucune raison d'occuper un quart de l'ecran —
# on veut voir ce qui joue, pas le lire.
APP_POSITIONS = frozenset({"tl", "tr", "bl", "br", "small"})

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "identity": {"name": "Ava", "city": "Paris"},
    "voice": {
        "engine": DEFAULT_VOICE_ENGINE,
        "kokoro_voice": "ff_siwis",
        "mistral_voice": "fr_marie_neutral",
        # la voix change de couleur selon ce qu'ava raconte (briefing enjoue,
        # question curieuse...). desactivable pour garder un timbre unique.
        "expressive": True,
        "reference": "",
        "exaggeration": 0.5,
        "cfg_weight": 0.35,
        "temperature": 0.65,
        "voice_id": "",
        "model_id": "eleven_multilingual_v2",
        "system_fallback": "Thomas",
    },
    # connecteur google (agenda) : le client oauth appartient a l'utilisateur,
    # les jetons vivent a part dans .cache/google_token.json.
    "google": {"client_id": "", "client_secret": ""},
    "wake": {
        "phrases": ["bonjour ava", "ok ava"],
        "clap": {"enabled": True, "sensitivity": 50, "min_gap_ms": 120, "max_gap_ms": 350},
    },
    "morning": {
        "spotify_uri": "",
        "open_apps_on_start": False,
        "apps": [
            {"name": "Terminal", "position": "tl"},
            {"name": "Safari", "position": "tr"},
            {"name": "Notes", "position": "bl"},
            {"name": "Music", "position": "br"},
        ],
    },
    "conversation": {
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "",
        "continuous_listening": True,
        "followup_timeout_seconds": 8,
        "max_continuous_turns": 12,
    },
    "computer_use": {"enabled": True, "confirmation_ttl_seconds": 30},
    # les competences peuvent lancer des scripts : on veut pouvoir les couper
    # d'un reglage, sans deplacer de dossier.
    "skills": {"enabled": True},
    "ui": {
        "start_hidden": False,
        "start_expanded": True,
        "show_illustrations": True,
        "startup_animation": True,
        "startup_duration_seconds": 9,
        "startup_hint_seconds": 0,
    },
}


class ConfigError(ValueError):
    pass


def clap_min_rms(sensitivity: int) -> float:
    """Niveau minimum pour qu'un pic ait le droit d'etre un clap.

    Mesures du 08/08 : les vraies mains font 0,69 / 0,98 / 2,08 ; la frappe au
    clavier plafonne vers 0,28-0,44. L'ancienne courbe donnait 0,28 au milieu du
    curseur, donc taper deux touches a 200 ms d'intervalle reveillait Ava. On
    remonte le plancher au-dessus du clavier tout en gardant de la marge.
    """
    value = max(0, min(100, int(sensitivity)))
    return round(0.90 - 0.007 * value, 3)


def _string(value: Any, fallback: str, max_length: int) -> str:
    return value.strip()[:max_length] if isinstance(value, str) else fallback


def _integer(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(minimum, min(maximum, int(value)))


def _boolean(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _decimal(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    # nan et inf traversaient les bornes sans bruit : min(1.2, nan) rend 1.2,
    # donc un nan se transformait en maximum au lieu d'etre refuse.
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return round(max(minimum, min(maximum, number)), 3)


def _http_url(value: Any, fallback: str) -> str:
    """Une url de moteur local, et rien d'autre : ni javascript:, ni file:."""
    candidate = _string(value, "", 300)
    if not candidate:
        return fallback
    parsed = urllib.parse.urlparse(candidate)
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else fallback


def _choice(value: Any, fallback: str, allowed: set[str]) -> str:
    candidate = value.strip().lower() if isinstance(value, str) else ""
    return candidate if candidate in allowed else fallback


def normalize_config(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ConfigError("la configuration doit etre un objet json")
    base = DEFAULT_CONFIG
    identity = candidate.get("identity", {})
    voice = candidate.get("voice", {})
    wake = candidate.get("wake", {})
    clap = wake.get("clap", {}) if isinstance(wake, dict) else {}
    morning = candidate.get("morning", {})
    conversation = candidate.get("conversation", {})
    computer = candidate.get("computer_use", {})
    skills_section = candidate.get("skills", {})
    google = candidate.get("google", {})
    ui = candidate.get("ui", {})
    for section in (identity, voice, wake, clap, morning, conversation, computer,
                    skills_section, google, ui):
        if not isinstance(section, dict):
            raise ConfigError("une section de configuration est invalide")

    phrases = wake.get("phrases", base["wake"]["phrases"])
    if not isinstance(phrases, list):
        phrases = base["wake"]["phrases"]
    phrases = [
        _string(item, "", 40) for item in phrases
        if isinstance(item, str) and _string(item, "", 40)
    ][:8] or list(base["wake"]["phrases"])

    apps = morning.get("apps", base["morning"]["apps"])
    clean_apps = []
    used_positions = set()
    if isinstance(apps, list):
        for item in apps[:8]:
            if not isinstance(item, dict):
                continue
            name = _string(item.get("name"), "", 80)
            position = item.get("position")
            if name and position in APP_POSITIONS and position not in used_positions:
                entry = {"name": name, "position": position}
                # une adresse ouvre l'app *sur* une page : « Dia » seul ouvre
                # l'onglet d'accueil, ce qui ne montre rien.
                url = _string(item.get("url"), "", 400)
                if url.startswith(("http://", "https://")):
                    entry["url"] = url
                clean_apps.append(entry)
                used_positions.add(position)
    if not clean_apps:
        clean_apps = deepcopy(base["morning"]["apps"])

    min_gap_ms = _integer(clap.get("min_gap_ms"), base["wake"]["clap"]["min_gap_ms"], 80, 500)
    max_gap_ms = _integer(clap.get("max_gap_ms"), base["wake"]["clap"]["max_gap_ms"], 150, 1200)
    max_gap_ms = max(max_gap_ms, min_gap_ms + 30)

    return {
        "schema_version": 1,
        "identity": {
            "name": _string(identity.get("name"), base["identity"]["name"], 60),
            "city": _string(identity.get("city"), base["identity"]["city"], 80),
        },
        "voice": {
            "engine": _choice(voice.get("engine"), base["voice"]["engine"],
                              {"kokoro", "mistral", "chatterbox", "elevenlabs", "system"}),
            "kokoro_voice": _choice(voice.get("kokoro_voice"),
                                    base["voice"]["kokoro_voice"], KOKORO_VOICES),
            "mistral_voice": _choice(voice.get("mistral_voice"),
                                     base["voice"]["mistral_voice"], MISTRAL_VOICES),
            "expressive": _boolean(voice.get("expressive"), base["voice"]["expressive"]),
            "reference": _string(voice.get("reference"), base["voice"]["reference"], 400),
            "exaggeration": _decimal(voice.get("exaggeration"), base["voice"]["exaggeration"], 0.2, 1.0),
            "cfg_weight": _decimal(voice.get("cfg_weight"), base["voice"]["cfg_weight"], 0.1, 1.0),
            "temperature": _decimal(voice.get("temperature"), base["voice"]["temperature"], 0.1, 1.2),
            "voice_id": _string(voice.get("voice_id"), base["voice"]["voice_id"], 200),
            "model_id": _string(voice.get("model_id"), base["voice"]["model_id"], 100),
            "system_fallback": _string(voice.get("system_fallback"), base["voice"]["system_fallback"], 60),
        },
        "google": {
            "client_id": _string(google.get("client_id"), base["google"]["client_id"], 200),
            "client_secret": _string(google.get("client_secret"), base["google"]["client_secret"], 200),
        },
        "wake": {
            "phrases": phrases,
            "clap": {
                "enabled": _boolean(clap.get("enabled"), base["wake"]["clap"]["enabled"]),
                "sensitivity": _integer(clap.get("sensitivity"), base["wake"]["clap"]["sensitivity"], 0, 100),
                "min_gap_ms": min_gap_ms,
                "max_gap_ms": max_gap_ms,
            },
        },
        "morning": {
            "spotify_uri": _string(morning.get("spotify_uri"), base["morning"]["spotify_uri"], 300),
            "open_apps_on_start": _boolean(
                morning.get("open_apps_on_start"), base["morning"]["open_apps_on_start"],
            ),
            "apps": clean_apps,
        },
        "conversation": {
            "base_url": _http_url(conversation.get("base_url"), base["conversation"]["base_url"]),
            "model": _string(conversation.get("model"), base["conversation"]["model"], 200),
            "continuous_listening": _boolean(
                conversation.get("continuous_listening"),
                base["conversation"]["continuous_listening"],
            ),
            "followup_timeout_seconds": _integer(
                conversation.get("followup_timeout_seconds"),
                base["conversation"]["followup_timeout_seconds"], 3, 20,
            ),
            "max_continuous_turns": _integer(
                conversation.get("max_continuous_turns"),
                base["conversation"]["max_continuous_turns"], 2, 30,
            ),
        },
        "skills": {
            "enabled": _boolean(skills_section.get("enabled"), base["skills"]["enabled"]),
        },
        "computer_use": {
            "enabled": _boolean(computer.get("enabled"), base["computer_use"]["enabled"]),
            "confirmation_ttl_seconds": _integer(
                computer.get("confirmation_ttl_seconds"),
                base["computer_use"]["confirmation_ttl_seconds"], 10, 120,
            ),
        },
        "ui": {
            "start_hidden": _boolean(ui.get("start_hidden"), base["ui"]["start_hidden"]),
            "start_expanded": _boolean(
                ui.get("start_expanded"), base["ui"]["start_expanded"],
            ),
            "show_illustrations": _boolean(
                ui.get("show_illustrations"), base["ui"]["show_illustrations"],
            ),
            "startup_animation": _boolean(
                ui.get("startup_animation"), base["ui"]["startup_animation"],
            ),
            "startup_duration_seconds": _integer(
                ui.get("startup_duration_seconds"),
                base["ui"]["startup_duration_seconds"], 4, 20,
            ),
            "startup_hint_seconds": _integer(
                ui.get("startup_hint_seconds"), base["ui"]["startup_hint_seconds"], 0, 15,
            ),
        },
    }


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in patch.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.last_error = ""
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if not self.path.exists():
                return deepcopy(DEFAULT_CONFIG)
            with self.path.open("r", encoding="utf-8") as handle:
                return normalize_config(json.load(handle))
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return deepcopy(DEFAULT_CONFIG)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            normalized = normalize_config(_merge(self._data, patch))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # le fichier porte le secret du client oauth google : il ne doit
            # jamais redevenir lisible par tout le monde. sans ce chmod, chaque
            # enregistrement depuis le panneau de reglages remettait 644.
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self._data = normalized
            listeners = tuple(self._listeners)
            snapshot = deepcopy(normalized)
        for listener in listeners:
            listener(deepcopy(snapshot))
        return snapshot

    def clap_min_rms(self) -> float:
        sensitivity = self.snapshot()["wake"]["clap"]["sensitivity"]
        return clap_min_rms(sensitivity)


STORE = ConfigStore(paths.CONFIG_FILE)
