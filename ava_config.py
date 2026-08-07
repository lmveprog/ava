"""configuration locale, validee et sauvegardee atomiquement pour ava."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "identity": {"name": "Ava", "city": "Paris"},
    "voice": {"voice_id": "", "model_id": "eleven_multilingual_v2", "system_fallback": "Thomas"},
    "wake": {
        "phrases": ["bonjour ava", "ok ava"],
        "clap": {"enabled": True, "sensitivity": 50, "min_gap_ms": 120, "max_gap_ms": 350},
    },
    "morning": {
        "spotify_uri": "",
        "apps": [
            {"name": "Terminal", "position": "tl"},
            {"name": "Safari", "position": "tr"},
            {"name": "Notes", "position": "bl"},
            {"name": "Music", "position": "br"},
        ],
    },
    "conversation": {"base_url": "http://127.0.0.1:1234/v1", "model": ""},
    "computer_use": {"enabled": True, "confirmation_ttl_seconds": 30},
    "ui": {"start_hidden": True, "startup_hint_seconds": 5},
}


class ConfigError(ValueError):
    pass


def clap_min_rms(sensitivity: int) -> float:
    value = max(0, min(100, int(sensitivity)))
    return round(0.50 - 0.0044 * value, 3)


def _string(value: Any, fallback: str, max_length: int) -> str:
    return value.strip()[:max_length] if isinstance(value, str) else fallback


def _integer(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(minimum, min(maximum, int(value)))


def _boolean(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


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
    ui = candidate.get("ui", {})
    for section in (identity, voice, wake, clap, morning, conversation, computer, ui):
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
            if name and position in {"tl", "tr", "bl", "br"} and position not in used_positions:
                clean_apps.append({"name": name, "position": position})
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
            "voice_id": _string(voice.get("voice_id"), base["voice"]["voice_id"], 200),
            "model_id": _string(voice.get("model_id"), base["voice"]["model_id"], 100),
            "system_fallback": _string(voice.get("system_fallback"), base["voice"]["system_fallback"], 60),
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
            "apps": clean_apps,
        },
        "conversation": {
            "base_url": _string(conversation.get("base_url"), base["conversation"]["base_url"], 300),
            "model": _string(conversation.get("model"), base["conversation"]["model"], 200),
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


STORE = ConfigStore(Path(__file__).with_name("config.json"))
