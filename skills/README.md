# Les compétences d'Ava

Une compétence apprend quelque chose à Ava **sans toucher à `ava.py`**. On dépose
un dossier ici, elle sait faire.

Le format est le standard ouvert [Agent Skills](https://agentskills.io), le même
que celui utilisé par OpenJarvis et par la plupart des agents récents.

## Écrire une compétence en deux minutes

```
skills/ma-competence/
  SKILL.md              obligatoire
  scripts/faire.py      facultatif
```

`SKILL.md` :

```markdown
---
name: ma-competence
description: Ce que ça fait, et surtout QUAND s'en servir. C'est la seule chose
  qu'Ava lit pour décider — écris-la comme tu l'expliquerais à quelqu'un.
command: scripts/faire.py
---

# Ma compétence

Les instructions détaillées. Ava ne les lit **que** si la demande correspond.
```

## Les deux façons de répondre

- **Avec un script** (`command:`) — Ava le lance, la demande arrive en argument,
  et elle dit à voix haute ce que le script écrit sur la sortie standard. C'est
  le plus fiable : le script décide, rien n'est inventé.
- **Sans script** — les instructions du `SKILL.md` servent de consigne au moteur
  de discussion, qui rédige la réponse.

## Ce qu'il faut savoir

- **La description fait tout.** Ava ne connaît que le nom et la description au
  moment de choisir (c'est la « divulgation progressive » du standard : le corps
  n'est lu qu'à l'activation). Une description vague = une compétence qui ne se
  déclenche jamais.
- **Écris pour l'oreille.** La sortie est prononcée. Pas de sigles, pas de `Go`
  ni de `%` : « gigaoctets », « pour cent ». Et des accents partout, sinon la
  synthèse écorche les mots.
- **Un script reste dans son dossier.** Un `command:` qui pointe ailleurs est
  refusé, et l'exécution est coupée au bout de 45 secondes.
- **Ava exécute ce que tu déposes ici.** N'y mets que des scripts que tu as lus.
  Le tout se coupe d'un coup avec `skills.enabled: false` dans `config.json`.

Un dossier personnel est également lu : `~/Documents/ava-skills`. En cas de même
nom, c'est lui qui gagne — pratique pour remplacer une compétence livrée sans
modifier le dépôt.

## Vérifier

```bash
.venv/bin/python doctor.py | grep competence
```
