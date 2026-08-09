"""where ava keeps her things.

everything that is *state* — config, tokens, caches, downloaded models — lives
next to the checkout, not inside the package. that way `git pull` never touches
your settings and deleting the clone deletes everything ava ever wrote.

set AVA_HOME if you'd rather keep that state somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "ui" / "web"
OVERLAY_HTML = WEB_DIR / "ava.html"

HOME = Path(os.getenv("AVA_HOME") or PACKAGE_DIR.parents[1]).expanduser()

ENV_FILE = HOME / ".env"
CONFIG_FILE = HOME / "config.json"
MODELS_DIR = HOME / "models"
VOICES_DIR = HOME / "voices"
BUILTIN_SKILLS_DIR = HOME / "skills"
USER_SKILLS_DIR = Path.home() / "Documents" / "ava-skills"

_CACHE_DIR = HOME / ".cache"


def write_private(path: Path, text: str) -> None:
    """Write a secret to disk atomically, and never world-readable.

    The old shape was write-then-chmod, which leaves the file at whatever the
    umask says for a moment — long enough to matter for a refresh token. Here
    the 0600 is part of the create, and the rename is atomic so a crash never
    leaves half a token behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    descriptor = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def cache_dir(*parts: str) -> Path:
    """A cache folder that only the current user can read.

    Tokens, cached intents and the trace log all end up in here, so the mode
    matters: 0700 on the folder means a second account on the mac can't walk in
    even if a file is written with a sloppier umask.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_CACHE_DIR, 0o700)
    path = _CACHE_DIR.joinpath(*parts)
    if parts:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path
