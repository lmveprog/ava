"""ava's skills, in the **Agent Skills** format.

same open standard openjarvis uses (agentskills.io, originally published by
anthropic): a skill is a **folder** holding a `SKILL.md` — yaml frontmatter,
then instructions — and, optionally, scripts and reference files.

    my-skill/
      SKILL.md          required: metadata + instructions
      scripts/          optional: executable code
      references/       optional: documentation

the standard's **progressive disclosure** in three steps is what makes this
workable here: ava can know thirty skills without carrying any weight for it.

1. **discovery** — at startup we read `name` and `description` and nothing else.
   just enough to know when a skill *might* be useful.
2. **activation** — when a request matches, we read the whole `SKILL.md`.
3. **execution** — we follow the instructions, running the script if there is one.

what ava gets out of it: adding a capability no longer means touching `app.py`.
drop a folder in, she knows how.

⚠️ **a skill can run code.** so they live in folders the user controls, are never
downloaded on their own, and execution is fenced in — no shell, path checked,
hard timeout. same level of trust as a script you run yourself from a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess

from ava import paths


# the skills shipped with ava first, then the user's. the personal folder wins
# on a name clash, so a bundled skill can be replaced without touching the repo.
BUILTIN_DIR = paths.BUILTIN_SKILLS_DIR
USER_DIR = paths.USER_SKILLS_DIR

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
        """The body of SKILL.md — the activation step, read only when needed."""
        try:
            raw = self.skill_file.read_text(encoding="utf-8")
        except OSError:
            return ""
        return _split_frontmatter(raw)[1][:MAX_INSTRUCTIONS_CHARS]

    def script(self) -> Path | None:
        """The script to run, if it exists and really lives inside the skill."""
        if not self.command:
            return None
        candidate = (self.path / self.command).resolve()
        # a skill has no business pointing outside its own folder. without this
        # check, a `command: ../../../bin/rm` would walk straight out of the box.
        try:
            candidate.relative_to(self.path.resolve())
        except ValueError:
            print(f"[skills] « {self.name} » pointe hors de son dossier, ignorée")
            return None
        return candidate if candidate.is_file() else None


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Split the yaml frontmatter from the body of the document."""
    text = raw.lstrip("﻿")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    header, body = match.group(1), match.group(2)
    try:
        import yaml
        data = yaml.safe_load(header)
    except Exception:  # noqa: BLE001 - broken yaml must not kill discovery
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
    # the standard requires name + description: without a description ava has no
    # way of knowing when to reach for it, so the skill would be dead weight.
    if not name or not description:
        print(f"[skills] « {folder.name} » sans nom ou sans description, ignorée")
        return None
    return Skill(name=name, description=description, path=folder,
                 command=str(meta.get("command", "") or "").strip()[:200],
                 metadata=meta)


def discover(directories=None) -> list[Skill]:
    """Step 1: name and description only — cheap enough to hold in memory."""
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
                found[skill.name] = skill      # the user folder is walked last, so it wins
    return list(found.values())


def catalogue(skills: list[Skill]) -> str:
    """What the intent router gets to see: a name and a description."""
    return "\n".join(f"- {skill.name} : {skill.description}" for skill in skills)


def find(name: str, skills: list[Skill] | None = None) -> Skill | None:
    wanted = str(name or "").strip().lower()
    for skill in (discover() if skills is None else skills):
        if skill.name.lower() == wanted:
            return skill
    return None


def run_script(skill: Skill, argument: str = "") -> tuple[bool, str]:
    """Step 3: run the skill's script. Returns (succeeded, output).

    No shell, the request goes in as an argument (never concatenated into a
    command line), and a hard timeout: a skill that spins must not freeze ava.
    """
    script = skill.script()
    if script is None:
        return False, ""
    command = [str(script)]
    if not os.access(script, os.X_OK):
        # script not marked executable: run it through its interpreter rather
        # than failing with a baffling "permission denied".
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
