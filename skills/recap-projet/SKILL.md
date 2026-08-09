---
name: recap-projet
description: Raconte où en est un projet de code — dernier travail, date, fichiers en cours de modification. À utiliser quand on demande où on en est sur un projet, ce qu'on a fait la dernière fois, ou l'état d'un dépôt git. Le nom du projet est passé en argument.
command: scripts/recap.py
---

# Récap d'un projet

Cette compétence répond à « où j'en suis sur *tel projet* ».

Ava passe au script le nom prononcé, tel quel. `scripts/recap.py` cherche un
dossier qui correspond dans les emplacements habituels (`~/Documents`,
`~/Desktop`, `~/projects`), lit le journal git, et rend une phrase :

- le dernier commit et quand il a été fait,
- combien de fichiers sont modifiés mais pas encore validés.

Si aucun dossier ne correspond, le script le dit — il ne devine pas.

## Ajouter un emplacement

Les dossiers fouillés sont listés en haut de `scripts/recap.py`, dans
`SEARCH_DIRS`.
