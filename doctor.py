#!/usr/bin/env python3
"""diagnostic sans action destructive pour l'installation locale d'ava."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from ava_config import STORE


HERE = Path(__file__).resolve().parent


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

    model = HERE / "models" / "vosk-model-small-fr-0.22"
    checks.append(Check(
        "modele:vosk-fr", "ok" if model.is_dir() else "error",
        str(model) if model.is_dir() else "modele absent",
    ))

    overlay = HERE / "overlay" / "ava.html"
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
    env_path = HERE / ".env"
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
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="verifie l'installation locale d'ava")
    parser.add_argument("--json", action="store_true", help="sortie json")
    args = parser.parse_args()
    checks = run_checks()
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    else:
        icons = {"ok": "✓", "warning": "!", "error": "✗"}
        for check in checks:
            print(f"{icons[check.status]} {check.name:<24} {check.detail}")
    return 1 if any(c.required and c.status == "error" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
