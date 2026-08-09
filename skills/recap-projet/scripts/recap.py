#!/usr/bin/env python3
"""ou en est un projet, d'apres son journal git.

le nom arrive tel qu'il a ete prononce (« lavalley », « le projet ava »), donc
on cherche de facon tolerante : sans accents, sans les mots de remplissage, et
en acceptant une correspondance partielle.
"""

from __future__ import annotations

import datetime
from pathlib import Path
import re
import subprocess
import sys
import unicodedata

SEARCH_DIRS = (
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "projects",
)

# mots qu'on prononce autour du nom sans qu'ils en fassent partie.
FILLERS = {
    "le", "la", "les", "mon", "ma", "mes", "projet", "projets", "dossier",
    "depot", "repo", "de", "du", "des", "sur", "ou", "j", "en", "suis", "ai",
    "est", "quoi", "avec", "pour", "dans", "c", "l", "d", "the",
}

# en dessous, un mot est trop court pour qu'une correspondance partielle veuille
# dire quelque chose.
MIN_PARTIAL = 5


def flatten(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def wanted_tokens(spoken: str) -> list[str]:
    return [token for token in flatten(spoken).split() if token not in FILLERS]


def find_project(spoken: str) -> Path | None:
    """Le dossier qui correspond au nom prononce.

    On compare **mot a mot**, jamais la phrase entiere recollee. Avec la phrase
    recollee, « ou j'en suis sur le projet lavalley » donnait
    « oujensuissurlavalley », qui *contient* « ava » (dans « l-ava-lley ») :
    n'importe quelle question finissait donc par designer le dossier `ava`.
    """
    tokens = wanted_tokens(spoken)
    if not tokens:
        return None
    joined = "".join(tokens)
    partial = None
    for directory in SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for folder in sorted(directory.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            name = "".join(flatten(folder.name).split())
            if not name:
                continue
            # le nom entier prononce, ou l'un des mots, tombe pile.
            if name == joined or name in tokens:
                return folder
            if partial is None:
                for token in tokens:
                    if len(token) >= MIN_PARTIAL and len(name) >= MIN_PARTIAL \
                            and (token in name or name in token):
                        partial = folder
                        break
    return partial


def git(project: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(project), *args],
                             capture_output=True, text=True, timeout=15, check=False)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def spoken_delay(when: datetime.datetime) -> str:
    days = (datetime.datetime.now(when.tzinfo).date() - when.date()).days
    if days <= 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    if days < 7:
        return f"il y a {days} jours"
    if days < 60:
        weeks = round(days / 7)
        return "il y a une semaine" if weeks == 1 else f"il y a {weeks} semaines"
    return f"il y a {round(days / 30)} mois"


def main() -> int:
    spoken = " ".join(sys.argv[1:]).strip()
    if not spoken:
        print("Sur quel projet ?")
        return 0

    project = find_project(spoken)
    if project is None:
        print(f"Je ne trouve pas de dossier qui ressemble à {spoken}.")
        return 0
    if not (project / ".git").is_dir():
        print(f"J'ai trouvé {project.name}, mais ce n'est pas un dépôt git.")
        return 0

    subject = git(project, "log", "-1", "--pretty=%s")
    stamp = git(project, "log", "-1", "--pretty=%cI")
    dirty = [line for line in git(project, "status", "--porcelain").splitlines() if line.strip()]

    if not subject:
        print(f"Le dépôt {project.name} n'a aucun commit pour le moment.")
        return 0

    when = ""
    try:
        when = spoken_delay(datetime.datetime.fromisoformat(stamp))
    except (TypeError, ValueError):
        pass

    phrase = f"Sur {project.name}, le dernier travail"
    phrase += f" remonte à {when}" if when else " enregistré"
    phrase += f" : {subject}."
    if dirty:
        count = len(dirty)
        phrase += (f" Il y a {count} fichier modifié qui n'est pas encore validé."
                   if count == 1
                   else f" Il y a {count} fichiers modifiés qui ne sont pas encore validés.")
    else:
        phrase += " Tout est validé."
    print(phrase)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
