"""Ava's long-term memory: a real Obsidian vault, plain markdown, all linked.

Layout (created on first use):
    Ava.md               -- home / index page
    Mémoire.md           -- the facts Matheus asks Ava to remember
    Journal/YYYY-MM-DD.md -- one note per day: briefing, commands, events
    Notes/<slug>.md      -- quick notes dictated by voice

Everything is connected with [[wikilinks]]: the daily note points to the notes
created that day, each fact points to the day it was learned, and back. Standard
library only — the vault is just files, which is the whole point of Obsidian.
"""

from __future__ import annotations

import datetime
import re
import threading
import unicodedata
import urllib.parse
from pathlib import Path

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]

INDEX_TEMPLATE = """# Ava

Bienvenue dans la mémoire d'Ava. Tout ce qu'elle entend, note et retient vit ici.

- [[Mémoire]] — ce qu'Ava a retenu pour toi
- Le journal du jour est dans `Journal/`
- Tes notes dictées sont dans `Notes/`
"""

MEMORY_HEADER = """# Mémoire

Les faits qu'Ava garde en tête. Dis « Ava, retiens que ... » pour en ajouter un.

"""


def _fold(text: str) -> str:
    """Lowercase without accents, for matching only (never for display)."""
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in value if unicodedata.category(c) != "Mn")


def slugify(text: str, max_len: int = 60) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", _fold(text)).strip("-")
    return value[:max_len].rstrip("-") or "note"


class ObsidianMemory:
    def __init__(self, vault_dir: Path | str) -> None:
        self.vault = Path(vault_dir).expanduser()
        self._lock = threading.Lock()

    # --- paths ---------------------------------------------------------------

    @property
    def journal_dir(self) -> Path:
        return self.vault / "Journal"

    @property
    def notes_dir(self) -> Path:
        return self.vault / "Notes"

    @property
    def memory_path(self) -> Path:
        return self.vault / "Mémoire.md"

    def ensure(self) -> None:
        # The .obsidian folder marks the directory as a vault, so Obsidian opens
        # it directly instead of walking through the "create a vault" flow.
        (self.vault / ".obsidian").mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(exist_ok=True)
        self.notes_dir.mkdir(exist_ok=True)
        index = self.vault / "Ava.md"
        if not index.exists():
            index.write_text(INDEX_TEMPLATE, encoding="utf-8")
        if not self.memory_path.exists():
            self.memory_path.write_text(MEMORY_HEADER, encoding="utf-8")

    # --- the daily note ------------------------------------------------------

    def daily_name(self, day: datetime.date | None = None) -> str:
        return (day or datetime.date.today()).isoformat()

    def daily_path(self, day: datetime.date | None = None) -> Path:
        return self.journal_dir / f"{self.daily_name(day)}.md"

    def _ensure_daily(self, day: datetime.date | None = None) -> Path:
        self.ensure()
        d = day or datetime.date.today()
        path = self.daily_path(d)
        if not path.exists():
            title = f"{FR_DAYS[d.weekday()]} {d.day} {FR_MONTHS[d.month - 1]} {d.year}"
            path.write_text(
                f"# {title}\n\n[[Ava]] · [[Mémoire]]\n\n", encoding="utf-8",
            )
        return path

    def _append_daily(self, line: str) -> None:
        with self._lock:
            path = self._ensure_daily()
            with path.open("a", encoding="utf-8") as f:
                f.write(line.rstrip() + "\n")

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().strftime("%H:%M")

    # --- what Ava writes -----------------------------------------------------

    def log_interaction(self, command: str, response: str = "") -> None:
        command = " ".join(command.split())
        response = " ".join(response.split())
        line = f"- {self._now()} — 🎙️ « {command} »"
        if response:
            line += f" → {response}"
        self._append_daily(line)

    def log_event(self, text: str) -> None:
        self._append_daily(f"- {self._now()} — {' '.join(text.split())}")

    def morning_briefing(self, spoken_text: str) -> None:
        # The morning briefing leaves a written trace in the daily note — the
        # exact text Ava spoke, so screen and page never disagree.
        text = " ".join(spoken_text.split())
        if text:
            self._append_daily(f"- {self._now()} — ☀️ **briefing du matin** : {text}")

    def remember(self, fact: str) -> str:
        # "retiens que ..." : the fact lands in Mémoire.md AND leaves a trace in
        # the daily note, each side linking to the other.
        fact = " ".join(fact.split()).strip(" .")
        if not fact:
            return ""
        day = self.daily_name()
        with self._lock:
            self._ensure_daily()
            with self.memory_path.open("a", encoding="utf-8") as f:
                f.write(f"- {fact} — retenu le [[{day}]]\n")
        self._append_daily(f"- {self._now()} — 🧠 retenu : {fact} ([[Mémoire]])")
        return fact

    def facts(self, limit: int = 50) -> list[str]:
        try:
            lines = self.memory_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            if line.startswith("- "):
                # Strip the "— retenu le [[...]]" suffix: it's for the page, not
                # for the voice.
                out.append(re.sub(r"\s+— retenu le \[\[.*?\]\]\s*$", "", line[2:]).strip())
        return out[-limit:]

    def recall(self, query: str, limit: int = 3) -> list[str]:
        # Naive but sturdy: keep the facts sharing the most >=4-letter words
        # with the question, accents folded on both sides.
        words = {w for w in _fold(query).split() if len(w) >= 4}
        if not words:
            return []
        scored = []
        for fact in self.facts(limit=500):
            hits = sum(1 for w in words if w in _fold(fact))
            if hits:
                scored.append((hits, fact))
        scored.sort(key=lambda x: -x[0])
        return [fact for _, fact in scored[:limit]]

    def quick_note(self, content: str) -> Path:
        # Every dictated note becomes its own file, linked from the daily note.
        content = content.strip().strip(" .")
        self.ensure()
        base = slugify(content[:60])
        path = self.notes_dir / f"{base}.md"
        n = 1
        while path.exists():
            n += 1
            path = self.notes_dir / f"{base}-{n}.md"
        day = self.daily_name()
        stamp = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        path.write_text(
            f"# {content[:80]}\n\n{content}\n\n— note dictée à Ava le {stamp} · [[{day}]]\n",
            encoding="utf-8",
        )
        self._append_daily(f"- {self._now()} — 📝 note : [[{path.stem}]]")
        return path

    def context_for_llm(self, limit: int = 12) -> str:
        # The latest remembered facts, handed to the local chat engine so Ava
        # actually remembers things across sessions.
        facts = self.facts(limit=limit)
        if not facts:
            return ""
        return "Ce que tu sais sur Matheus :\n" + "\n".join(f"- {f}" for f in facts)

    def obsidian_uri(self) -> str:
        return "obsidian://open?path=" + urllib.parse.quote(str(self.vault))
