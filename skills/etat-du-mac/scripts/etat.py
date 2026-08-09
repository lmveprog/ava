#!/usr/bin/env python3
"""l'etat de la machine, en une phrase deja prononcable.

les unites sont ecrites en toutes lettres (« gigaoctets », pas « Go ») : la
synthese vocale bute sur les abreviations, et « 42 Go » se prononce « quarante-
deux gé o ».
"""

from __future__ import annotations

import re
import shutil
import subprocess


def _run(command: list[str]) -> str:
    try:
        out = subprocess.run(command, capture_output=True, text=True,
                             timeout=10, check=False)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def battery() -> str:
    raw = _run(["pmset", "-g", "batt"])
    percent = re.search(r"(\d+)%", raw)
    if not percent:
        return ""
    level = int(percent.group(1))
    charging = "AC Power" in raw or "charging" in raw.lower()
    if charging:
        return f"la batterie est à {level} pour cent et se recharge"
    if level <= 20:
        return f"il ne te reste que {level} pour cent de batterie"
    return f"la batterie est à {level} pour cent"


def disk() -> str:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return ""
    free_gb = round(usage.free / 1_000_000_000)
    if free_gb < 15:
        return f"attention, il ne reste que {free_gb} gigaoctets de libre sur le disque"
    return f"il reste {free_gb} gigaoctets de libre sur le disque"


def uptime() -> str:
    raw = _run(["uptime"])
    match = re.search(r"up\s+(.+?),\s+\d+\s+user", raw)
    if not match:
        return ""
    value = match.group(1).strip()
    value = value.replace("days", "jours").replace("day", "jour")
    value = value.replace("hrs", "heures").replace("mins", "minutes")
    # « 3:42 » = 3 heures 42
    hours = re.fullmatch(r"(\d+):(\d+)", value)
    if hours:
        value = f"{int(hours.group(1))} heures et {int(hours.group(2))} minutes"
    return f"il tourne depuis {value}"


def main() -> int:
    parts = [piece for piece in (battery(), disk(), uptime()) if piece]
    if not parts:
        print("Je n'arrive pas à lire l'état de la machine.")
        return 1
    sentence = parts[0].capitalize()
    if len(parts) > 1:
        sentence += ", " + ", ".join(parts[1:-1] + [f"et {parts[-1]}"]) if len(parts) > 2 \
            else ", et " + parts[1]
    print(sentence + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
