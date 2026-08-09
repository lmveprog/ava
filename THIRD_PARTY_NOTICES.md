# Third-party notices

## Agent Skills (format des compétences)

Le dossier `skills/` suit le standard ouvert **Agent Skills**
(<https://agentskills.io>) : un dossier par compétence, un `SKILL.md` avec un
frontmatter YAML (`name`, `description`), et une divulgation progressive en trois
temps (découverte, activation, exécution).

Le format a été publié à l'origine par [Anthropic](https://www.anthropic.com/)
et ouvert à l'écosystème. Ava en implémente sa propre lecture — aucun code n'est
repris — avec un champ supplémentaire, `command`, qui désigne le script à lancer.

L'idée de l'appliquer à un assistant vocal, et celle de traiter la latence et le
coût comme des contraintes de premier ordre (voir `traces.py`), viennent de
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) (Apache 2.0,
« Copyright 2025 The OpenJarvis Authors »). Aucune ligne de son code n'a été
copiée ; seules les idées d'architecture ont été reprises.

## Modèle vocal français Vosk

Le réveil hors ligne utilise `vosk-model-small-fr-0.22`, distribué par
[Alphacephei](https://alphacephei.com/vosk/models) sous licence Apache 2.0.

---

*Note du 8 août 2026 : les mentions de la mascotte « AI Orb Mascot » et du
runtime Rive ont été retirées — l'orbe est désormais dessinée en local sur un
canvas, et plus aucun asset distant n'est chargé.*
