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
