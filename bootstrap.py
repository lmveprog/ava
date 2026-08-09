#!/usr/bin/env python3
"""Prepare un environnement Ava reproductible, sans modifier Python global."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import venv
import zipfile


HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
MODEL_NAME = "vosk-model-small-fr-0.22"
MODEL_DIR = HERE / "models" / MODEL_NAME
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install_python_dependencies() -> None:
    if not venv_python().exists():
        print("→ Création de .venv")
        venv.EnvBuilder(with_pip=True).create(VENV)
    print("→ Installation des dépendances Python")
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
        check=True,
    )


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if root not in target.parents and target != root:
            raise RuntimeError("archive Vosk invalide")
    archive.extractall(destination)


def install_vosk_model() -> None:
    marker = MODEL_DIR / "am" / "final.mdl"
    if marker.is_file():
        print("✓ Modèle vocal français déjà installé")
        return
    (HERE / "models").mkdir(parents=True, exist_ok=True)
    print("→ Téléchargement du modèle vocal français (environ 41 Mo)")
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp:
        with urllib.request.urlopen(MODEL_URL, timeout=90) as response:  # noqa: S310
            while chunk := response.read(1024 * 1024):
                temp.write(chunk)
        temp.flush()
        with zipfile.ZipFile(temp.name) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"archive Vosk corrompue : {bad_member}")
            _safe_extract(archive, HERE / "models")
    if not marker.is_file():
        raise RuntimeError("le modèle Vosk téléchargé est incomplet")
    print("✓ Modèle vocal français installé")


def main() -> int:
    parser = argparse.ArgumentParser(description="installe Ava localement")
    parser.add_argument("--skip-model", action="store_true", help="n'installe pas le wake word hors ligne")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 ou plus récent est requis")
    install_python_dependencies()
    if not args.skip_model:
        install_vosk_model()
    print("\nAva est prête. Lance : .venv/bin/python ava.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

