"""les competences d'ava, au format **Agent Skills**.

c'est le meme standard ouvert que celui d'openjarvis (agentskills.io, publie a
l'origine par anthropic) : une competence est un **dossier** qui contient un
`SKILL.md` — des metadonnees en frontmatter yaml, puis des instructions — et,
facultativement, des scripts et des fichiers de reference.

    ma-competence/
      SKILL.md          obligatoire : metadonnees + instructions
      scripts/          facultatif : code executable
      references/       facultatif : documentation

le standard prevoit une **divulgation progressive** en trois temps, et c'est ce
qui rend la chose viable ici : ava peut connaitre trente competences sans
alourdir quoi que ce soit.

1. **decouverte** — au demarrage on ne lit que `name` et `description`. juste de
   quoi savoir quand une competence *pourrait* servir.
2. **activation** — quand une demande correspond, on lit le `SKILL.md` en entier.
3. **execution** — on suit les instructions, en lancant le script fourni s'il y
   en a un.

interet pour ava : ajouter une capacite ne demande plus de toucher a `ava.py`.
on depose un dossier, elle sait faire.

⚠️ **une competence peut executer du code.** elles vivent donc dans des dossiers
que l'utilisateur controle, jamais telechargees toutes seules, et l'execution
est bornee (pas de shell, chemin verifie, delai maximum). c'est le meme niveau
de confiance qu'un script qu'on lance soi-meme depuis son terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent

# les competences livrees avec ava, puis celles de l'utilisateur. le dossier
# personnel gagne en cas de meme nom : on peut donc remplacer une competition
# fournie sans modifier le depot.
BUILTIN_DIR = HERE / "skills"
USER_DIR = Path.home() / "Documents" / "ava-skills"

MAX_INSTRUCTIONS_CHARS = 12000
SCRIPT_TIMEOUT_S = 45
MAX_SPOKEN_CHARS = 700


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    command: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def skill_file(self) -> Path:
        return self.path / "SKILL.md"

    def instructions(self) -> str:
        """Le corps du SKILL.md — l'etape « activation », lue seulement au besoin."""
        try:
            raw = self.skill_file.read_text(encoding="utf-8")
        except OSError:
            return ""
        return _split_frontmatter(raw)[1][:MAX_INSTRUCTIONS_CHARS]

    def script(self) -> Path | None:
        """Le script a lancer, s'il existe et s'il est bien dans la competence."""
        if not self.command:
            return None
        candidate = (self.path / self.command).resolve()
        # une competence n'a aucune raison de pointer hors de son propre dossier.
        # sans cette verification, un `command: ../../../bin/rm` sortirait du bac.
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError:
            print(f"[skills] « {self.name} » pointe hors de son dossier, ignorée")
            return None
        return candidate if candidate.is_file() else None


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Separe le frontmatter yaml du corps du document."""
    text = raw.lstrip("﻿")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    header, body = match.group(1), match.group(2)
    try:
        import yaml
        data = yaml.safe_load(header)
    except Exception:  # noqa: BLE001 - un yaml casse ne doit pas tuer la decouverte
        return {}, body
    return (data if isinstance(data, dict) else {}), body


def _read_skill(folder: Path) -> Skill | None:
    skill_file = folder / "SKILL.md"
    if not skill_file.is_file():
        return None
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, _body = _split_frontmatter(raw)
    name = str(meta.get("name", "") or folder.name).strip()[:80]
    description = str(meta.get("description", "") or "").strip()[:600]
    # le standard exige name + description : sans description, ava n'a aucun
    # moyen de savoir quand s'en servir, donc la competence serait morte.
    if not name or not description:
        print(f"[skills] « {folder.name} » sans nom ou sans description, ignorée")
        return None
    return Skill(name=name, description=description, path=folder,
                 command=str(meta.get("command", "") or "").strip()[:200],
                 metadata=meta)


def discover(directories=None) -> list[Skill]:
    """Etape 1 : nom et description seulement, pour tenir en memoire sans cout."""
    found: dict[str, Skill] = {}
    for directory in (directories if directories is not None else (BUILTIN_DIR, USER_DIR)):
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for folder in sorted(directory.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            skill = _read_skill(folder)
            if skill is not None:
                found[skill.name] = skill      # le dossier utilisateur passe apres
    return list(found.values())


def catalogue(skills: list[Skill]) -> str:
    """La liste que voit le routeur d'intentions : un nom, une description."""
    return "\n".join(f"- {skill.name} : {skill.description}" for skill in skills)


def find(name: str, skills: list[Skill] | None = None) -> Skill | None:
    wanted = str(name or "").strip().lower()
    for skill in (discover() if skills is None else skills):
        if skill.name.lower() == wanted:
            return skill
    return None


def run_script(skill: Skill, argument: str = "") -> tuple[bool, str]:
    """Etape 3 : lance le script de la competence. Rend (reussite, sortie).

    Pas de shell, la demande passe en argument (jamais concatenee dans une ligne
    de commande), et un delai maximum : une competence qui part en boucle ne doit
    pas figer ava.
    """
    script = skill.script()
    if script is None:
        return False, ""
    command = [str(script)]
    if not os.access(script, os.X_OK):
        # script non marque executable : on le lance avec son interprete plutot
        # que d'echouer sur un « permission denied » incomprehensible.
        interpreter = "python3" if script.suffix == ".py" else "/bin/sh"
        command = [interpreter, str(script)]
    if argument:
        command.append(argument[:500])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=SCRIPT_TIMEOUT_S, cwd=str(skill.path), check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"La compétence {skill.name} a mis trop de temps."
    except OSError as exc:
        return False, f"La compétence {skill.name} n'a pas pu démarrer : {exc}"
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return False, detail[-1][:200] if detail else f"La compétence {skill.name} a échoué."
    return True, output[:MAX_SPOKEN_CHARS]
