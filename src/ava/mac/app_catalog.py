"""Index Spotlight des applications macOS, avec résolution vocale prudente."""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import re
import subprocess
import threading
import time
import unicodedata


def normalize_app_name(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    value = re.sub(r"\b(?:l |la |le |les |application |app )\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class AppCatalog:
    def __init__(self, ttl_s: float = 180) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._updated_at = 0.0
        self._apps: dict[str, tuple[str, str]] = {}

    def _spotlight_paths(self) -> list[str]:
        try:
            result = subprocess.run(
                ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
                capture_output=True, text=True, check=False, timeout=12,
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip().endswith(".app")]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return []

    def refresh(self, force: bool = False) -> dict[str, tuple[str, str]]:
        with self._lock:
            if not force and self._apps and time.monotonic() - self._updated_at < self.ttl_s:
                return dict(self._apps)
            paths = self._spotlight_paths()
            if not paths:
                roots = (
                    Path("/Applications"), Path("/System/Applications"),
                    Path.home() / "Applications",
                )
                for root in roots:
                    if root.exists():
                        paths.extend(str(path) for path in root.glob("**/*.app"))
            apps: dict[str, tuple[str, str]] = {}
            for raw_path in paths:
                path = Path(raw_path)
                # Un bundle imbriqué dans un autre bundle est un composant, pas une app utilisateur.
                if any(parent.suffix == ".app" for parent in path.parents):
                    continue
                name = path.stem.strip()
                normalized = normalize_app_name(name)
                if not normalized:
                    continue
                current = apps.get(normalized)
                if current is None or raw_path.startswith("/Applications/"):
                    apps[normalized] = (name, raw_path)
            self._apps = apps
            self._updated_at = time.monotonic()
            return dict(apps)

    def resolve(self, spoken: str) -> tuple[str, str] | None:
        wanted = normalize_app_name(spoken)
        if not wanted:
            return None
        apps = self.refresh()
        if wanted in apps:
            return apps[wanted]

        partial: list[tuple[float, tuple[str, str]]] = []
        wanted_tokens = set(wanted.split())
        for key, app in apps.items():
            key_tokens = set(key.split())
            if wanted in key or key in wanted:
                coverage = min(len(wanted), len(key)) / max(len(wanted), len(key))
                partial.append((0.78 + 0.2 * coverage, app))
                continue
            token_score = len(wanted_tokens & key_tokens) / max(1, len(wanted_tokens | key_tokens))
            ratio = SequenceMatcher(None, wanted, key).ratio()
            partial.append((max(token_score, ratio), app))
        if not partial:
            return None
        partial.sort(key=lambda item: item[0], reverse=True)
        best_score, best = partial[0]
        second_score = partial[1][0] if len(partial) > 1 else 0.0
        # Un nom mal entendu ne doit jamais ouvrir arbitrairement une autre app.
        if best_score < 0.73 or best_score - second_score < 0.06:
            return None
        return best

