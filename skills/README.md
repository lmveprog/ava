# Ava's skills

A skill teaches Ava something **without touching `app.py`**. Drop a folder in
here and she knows how.

The format is the open [Agent Skills](https://agentskills.io) standard, the same
one OpenJarvis and most recent agents use.

## writing one, in two minutes

```
skills/my-skill/
  SKILL.md              required
  scripts/do-it.py      optional
```

`SKILL.md`:

```markdown
---
name: my-skill
description: What it does, and above all WHEN to use it. This is the only thing
  Ava reads when deciding — write it the way you'd explain it to a person.
command: scripts/do-it.py
---

# My skill

The detailed instructions. Ava reads these **only** if the request matches.
```

## the two ways of answering

- **With a script** (`command:`) — Ava runs it, the request arrives as an
  argument, and she says out loud whatever the script writes to stdout. This is
  the reliable one: the script decides, nothing is made up.
- **Without a script** — the `SKILL.md` body becomes the brief for the
  conversation engine, which writes the answer.

## what to keep in mind

- **The description does all the work.** When choosing, Ava knows nothing but
  the name and the description — that's the standard's progressive disclosure,
  the body is only read on activation. A vague description means a skill that
  never fires.
- **Write the description in the language you speak to her.** It gets matched
  against what you actually said, so the two shipped skills are in French like
  the rest of what Ava hears and says.
- **Write for the ear.** The output gets spoken. No abbreviations, no `GB` or
  `%`: "gigaoctets", "pour cent". And keep the accents on, or the synthesiser
  mangles the words.
- **A script stays in its own folder.** A `command:` pointing anywhere else is
  refused, and execution is killed after 45 seconds.
- **Ava runs whatever you put here.** Only put scripts you've read. The whole
  mechanism switches off with `"skills": {"enabled": false}` in `config.json`.

A personal folder is read too: `~/Documents/ava-skills`. On a name clash it
wins — handy for replacing a bundled skill without editing the repo.

## checking

```bash
.venv/bin/ava-doctor | grep competence
```
