---
name: etat-du-mac
description: Donne l'état de la machine à voix haute — batterie, place disque restante, mémoire et temps depuis le dernier démarrage. À utiliser quand on demande comment va le Mac, s'il reste de la batterie, s'il reste de la place, ou pourquoi il rame.
command: scripts/etat.py
---

# État du Mac

Cette compétence lit l'état de la machine et le résume en une phrase dite à voix
haute.

Le script `scripts/etat.py` fait tout le travail : il interroge `pmset` pour la
batterie, `df` pour le disque et `sysctl` pour la mémoire, puis rend une seule
ligne en français, déjà prononçable (pas de sigles, pas d'unités abrégées).

Ava lit cette ligne telle quelle. Si le script échoue, elle le dit simplement
plutôt que d'inventer des chiffres.
