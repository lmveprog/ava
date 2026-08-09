# Ava

Ava est une assistante personnelle locale pour macOS : réveil vocal en français,
commandes clavier ou micro, actions sur le Mac et mini-interface toujours à portée
de main.

![Le briefing du matin d'Ava](docs/images/briefing.png)

## Ce qui rend Ava différente

- **Une extension de la barre de menus**, pas une fenêtre posée sur le bureau :
  icône en haut à droite à côté des autres, aucune icône dans le Dock, panneau
  qui tombe pile sous son icône et se referme d'un clic. L'icône change selon ce
  qu'Ava fait — au repos, elle écoute, elle réfléchit, elle parle.
- **« Mettre l'écoute en pause » dans le menu** : le micro reste ouvert mais plus
  rien ne peut la réveiller. Un clic pour la faire taire, un clic pour la
  reprendre — sans jamais avoir à la tuer.
- **Elle ne dépend de rien.** La voix et l'oreille tournent sur le GPU du Mac :
  aucune clé, aucun quota, aucune latence réseau, et rien de ce qui est dit ne
  quitte la machine. Ce n'est pas un repli dégradé — c'est le chemin **le plus
  rapide** : « Oui ? » part en **0,065 s** (contre 0,51 s par le réseau), un
  briefing de 31 s se fabrique en **1,7 s**, et une commande se transcrit en
  **0,23 s** — huit fois plus vite, et plus juste, que le Whisper local d'avant.
- **Un seul moteur, pas une pile de replis.** Quatre moteurs enchaînés, c'était
  quatre timbres possibles pour la même phrase et quatre délais à payer avant
  d'arriver au dernier. Il en reste **un**, et `say` comme filet.
- **On peut la couper.** « Stop », le bouton micro ou ⌘. arrêtent la phrase en
  cours ; à l'écran, ce qui n'a pas été prononcé se grise, pour qu'on ne lise
  pas une réponse qu'on n'a jamais entendue.
- **Une coupure réseau ne la fait plus attendre.** Un seul endroit sait si le
  Mac est en ligne : le premier appel qui échoue coupe court pour tous les
  autres, et chacun part directement sur son repli local. Wifi présent mais
  internet absent, la première réponse passe de **20 s à 3 s**, les suivantes à
  **zéro** — au lieu de payer un timeout complet par phrase, par flux d'actu et
  par relevé météo.
- **Elle ne prend pas la télévision pour toi.** Le micro entend la pièce
  entière : ce qui arrive en trois phrases sans verbe d'action ni question est
  reconnu comme une conversation d'ambiance, et Ava se tait au lieu d'aller
  chercher sur le web le nom d'un joueur entendu au passage.
- **Le grand rituel du matin ne se joue qu'une fois par jour.** Un « bonjour
  Ava » à 18 h ne relance plus la musique, les applications et 45 s de briefing :
  elle dit simplement bonsoir et écoute.
- **Elle apprend tes tournures.** Ce que le routage par mots-clés ne comprend pas
  part vers un petit modèle qui en déduit l'intention, puis la réponse est
  gardée sur le disque : la même phrase, la fois d'après, est instantanée et
  gratuite.
- **Elle est extensible sans toucher au code.** Ava lit le format ouvert
  [Agent Skills](https://agentskills.io) : on dépose un dossier avec un
  `SKILL.md` dans `skills/` (ou `~/Documents/ava-skills`), et elle sait faire.
  Deux compétences sont livrées — l'état du Mac, et « où j'en suis sur tel
  projet ». Voir [`skills/README.md`](skills/README.md).
- **Elle mesure où part son temps.** Chaque interaction laisse une trace — route
  empruntée, latence, réseau ou non — jamais ce qui a été dit.
  `doctor.py --traces` sort le tableau.
- Mini-plugin visible au démarrage, utilisable immédiatement au clavier.
- Bouton micro sans mot de réveil, plus `Bonjour Ava` et `OK Ava` hors ligne.
- Capture vocale adaptative au bruit ambiant, pré-roll anti-coupure et détection
  automatique de fin de phrase.
- Transcription partielle en direct avec Vosk, puis transcription finale sur le
  GPU du Mac (Whisper large-v3-turbo via MLX).
- Confirmations sensibles avec de vrais boutons **Oui / Non**.
- Réponses illustrées seulement lorsqu’un repère visuel est utile.
- Orbe fluide dessinée en local, branchée sur les états réels d'Ava.
- Conversation et exécution locales par défaut.
- **Voix locale par défaut** : Kokoro (Apache 2.0) sur le GPU du Mac, voix
  française `ff_siwis`. Mistral, Chatterbox et ElevenLabs restent sélectionnables
  dans les réglages, mais plus rien n'y retombe automatiquement.
- **Google Agenda connecté en deux clics** depuis les réglages (OAuth avec PKCE,
  jetons chiffrés sur le disque). Ava lit ton agenda du jour et **y écrit** :
  « ajoute un rendez-vous dentiste demain à 14 heures ».
- Agenda macOS en lecture seule comme repli hors ligne.
- **Session de focus Prométhée lancée automatiquement** au « bonjour Ava ».
- Recherche web intégrée : Ava lit la réponse et affiche ses sources sans ouvrir
  automatiquement un onglet.
- Calendrier officiel de l'OM pour les prochains matchs, sans réponse mémorisée.
- Index Spotlight de toutes les applications installées, avec tolérance aux noms
  mal prononcés et seuil anti-faux-positif.
- Diagnostic d'écran à la demande via Ollama + Gemma 3 Vision, entièrement local.
- Conversation vocale continue : après une réponse, Ava écoute directement la
  demande suivante pendant quelques secondes, sans répéter « OK Ava ».
- Scène de démarrage animée au centre de l'écran avec prénom, ville, actualité IA
  récente et sourcée, puis citation entrepreneuriale renouvelée à chaque lancement.

## Installation

Pré-requis : macOS et Python 3.11 ou plus récent. Python 3.12 est recommandé.

```bash
cd /chemin/vers/ava
python3.12 bootstrap.py
.venv/bin/python doctor.py
.venv/bin/python ava.py
```

Le bootstrap installe les dépendances dans `.venv` et récupère le modèle français
Vosk officiel. Il ne modifie pas le Python global.

Au premier lancement, macOS peut demander plusieurs permissions, uniquement au
moment où la fonction correspondante est utilisée :

- **Microphone** pour la voix ;
- **Accessibilité** pour cliquer, écrire ou piloter une fenêtre ;
- **Automatisation → Calendar** pour lire les rendez-vous ;
- **Automatisation → Promethee** pour ouvrir une session de focus ;
- **Enregistrement de l'écran et audio système** pour le diagnostic visuel.

Pour l'analyse d'écran locale, installe et lance
[Ollama](https://ollama.com/) avec un modèle vision. Ava détecte automatiquement
`gemma3`, `qwen3-vl`, `llama3.2-vision` ou `llava` :

```bash
ollama pull gemma3:12b
```

## Utilisation

- Écris directement dans le mini-plugin puis appuie sur Entrée.
- Clique sur le micro et parle naturellement.
- Dis `OK Ava, ouvre Spotify` pour une commande complète.
- Dis `Bonjour Ava` ou fais un double clap pour le rituel du matin.
- Le rituel reprend le dernier contexte Spotify en lecture aléatoire et passe à
  un nouveau morceau. Une URI Spotify peut aussi imposer une playlist précise.
- Demande `Ouvre mon agenda et dis-moi ce qui est prévu aujourd'hui`.
- Demande `Quand est le prochain match de l'OM ?`.
- Demande `Ouvre WhatsApp`, `ouvre Xcode` ou n'importe quelle app installée.
- Affiche une erreur puis demande `Quel est ce problème ?` : Ava masque son
  panneau, capture l'écran, montre la miniature capturée et l'analyse localement.
- Demande `Recherche les nouveautés de Python` pour une réponse avec sources.

Après chaque réponse vocale, le statut devient **Tu peux enchaîner** : parle tout de
suite. Un silence ferme naturellement la session ; dis `stop`, `c'est bon` ou
`merci Ava` pour la terminer explicitement. Le comportement et l'animation de départ
peuvent être désactivés dans les réglages du mini-plugin.

Les réglages de ville, voix, phrases de réveil, applications et préférences visuelles
sont accessibles via l’icône en haut à droite.

## Diagnostic et tests

```bash
.venv/bin/python doctor.py
.venv/bin/python -m unittest discover -s tests -v
```

`doctor.py` vérifie les bibliothèques, le modèle vocal, le micro, Ollama, le modèle
vision, l'état du réseau et les permissions macOS. La suite couvre aussi Calendar, la
source officielle OM, l'index d'applications, la vision locale et les cartes de sources.

`tests/test_smoke_phrases.py` fait passer une cinquantaine de tournures réelles
dans le routage et vérifie la **route** prise, pas la phrase rendue : c'est ainsi
qu'ont été trouvés les cas où « précédent », « salut Ava » ou « qu'est-ce que
j'ai aujourd'hui » finissaient en recherche web. Pour éprouver le comportement
hors ligne sans couper le wifi, on pointe l'API vers un trou noir
(`https://10.255.255.1`) : la connexion expire sans jamais être refusée, ce qui
reproduit le cas d'un réseau présent mais mort.

## Confidentialité

Le wake word Vosk, Faster Whisper, Ollama et l'analyse des captures restent locaux.
Ava ne prend une capture que lorsque la demande l'indique explicitement, ne déclenche
aucune action depuis le texte lu à l'écran et conserve l'image dans `~/Pictures/Ava`.
Une recherche envoie seulement la requête à DuckDuckGo ou à la source officielle
concernée. Une clé Mistral active Voxtral pour la transcription finale ; une clé
ElevenLabs envoie le texte de la réponse à ElevenLabs pour la voix Laura. Sans ces
clés, Ava garde ses fallbacks locaux et la voix système macOS.

## Principes d'architecture

Ava sépare les outils déterministes (Calendar, ouverture d'app, actions autorisées)
des réponses générées. Les actions sensibles demandent confirmation, les sources et
captures sont visibles dans le panneau, et une sortie de modèle visuel ne peut jamais
agir directement sur le Mac. Ces principes reprennent les meilleurs patterns observés
dans [Vibe Buddy](https://github.com/edouardfoussier/hackathon-mistral-vibe),
[Open Interpreter](https://github.com/OpenInterpreter/open-interpreter),
[Leon](https://github.com/leon-ai/leon) et [Cua](https://github.com/trycua/cua).

## Crédits

L’animation optionnelle est [AI Orb Mascot par aln.omrv](https://rive.app/community/files/28088-53050-ai-orb-mascot/),
adaptée sous licence CC BY 4.0. Les détails sont dans
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
