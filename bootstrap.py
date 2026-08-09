#!/usr/bin/env python3
"""set ava up in one command, without touching the system python.

    python3 bootstrap.py

makes a .venv next to this file, installs ava into it in editable mode, and
downloads the french vosk model used for the offline wake word.
"""

from __future__ import annotations

import argparse
import hashlib
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
# pinned against the archive this project was built and tested on. alphacephei
# serves it over plain https with no signature, so the hash is what tells us we
# got the same 42 MB of model weights and not something else.
MODEL_SHA256 = "cabf6180e177eb9b3a9a9d43a437bd5e549f3a7d09525e5d69a3fed787be12ad"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def install_python_dependencies(extras: bool) -> None:
    if not venv_python().exists():
        print("-> creating .venv")
        venv.EnvBuilder(with_pip=True).create(VENV)
    print("-> installing ava and its dependencies")
    target = f"{HERE}[extras]" if extras else str(HERE)
    subprocess.run([str(venv_python()), "-m", "pip", "install", "-e", target], check=True)


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Refuse any member that would land outside `destination` (zip slip)."""
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if root not in target.parents and target != root:
            raise RuntimeError("invalid vosk archive")
    archive.extractall(destination)


def _download(url: str, handle) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=90) as response:  # noqa: S310
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
            digest.update(chunk)
    handle.flush()
    return digest.hexdigest()


def install_vosk_model() -> None:
    marker = MODEL_DIR / "am" / "final.mdl"
    if marker.is_file():
        print("ok  french wake word model already installed")
        return
    (HERE / "models").mkdir(parents=True, exist_ok=True)
    print("-> downloading the french wake word model (about 41 MB)")
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp:
        got = _download(MODEL_URL, temp)
        if got != MODEL_SHA256:
            raise RuntimeError(
                "the vosk model does not match its expected checksum — "
                f"got {got}, expected {MODEL_SHA256}. nothing was installed."
            )
        with zipfile.ZipFile(temp.name) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"corrupt vosk archive: {bad_member}")
            _safe_extract(archive, HERE / "models")
    if not marker.is_file():
        raise RuntimeError("the downloaded vosk model is incomplete")
    print("ok  french wake word model installed")


def main() -> int:
    parser = argparse.ArgumentParser(description="install ava locally")
    parser.add_argument("--skip-model", action="store_true",
                        help="don't install the offline wake word model")
    parser.add_argument("--extras", action="store_true",
                        help="also install the optional cloud engines")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("python 3.11 or newer is required")
    install_python_dependencies(args.extras)
    if not args.skip_model:
        install_vosk_model()
    print("\nava is ready. check the install with:  .venv/bin/ava-doctor")
    print("then start her with:                 .venv/bin/ava")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
