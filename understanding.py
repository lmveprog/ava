"""comprendre une commande quand les mots-cles n'ont pas suffi.

le routage d'ava est deterministe : une liste de verbes, de prefixes et de mots
declencheurs. c'est instantane et ca ne coute rien, donc ca reste le chemin
principal. mais tout ce qui sort du moule tombait jusqu'ici dans le dernier
filet — la recherche web — et ava repondait a cote :

    « baisse un peu le son »          -> recherche web
    « il me faudrait spotify »        -> recherche web
    « rappelle-moi dans un quart d'heure » -> recherche web

ce module ne remplace pas le routage : il s'intercale **juste avant** ce dernier
filet. on n'appelle donc le reseau que pour les phrases qui allaient de toute
facon etre mal traitees, et les commandes courantes gardent leur latence nulle.

modele : `ministral-8b-latest`. mesure du 08/08 : 0,39 s par classification,
contre 0,54 s pour le 3b qui rendait en plus du json plus brouillon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time

import requests

import net

MODEL = os.getenv("AVA_NLU_MODEL", "ministral-8b-latest").strip()
BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()
TIMEOUT_S = 6.0

# la liste est fermee : chaque entree correspond a une capacite qui existe
# vraiment dans `_dispatch_command`. un modele qui invente « commander_pizza »
# nous ferait promettre une action qu'ava ne sait pas faire.
INTENTS = {
    "ouvrir_app",        # cible = nom de l'application
    "ouvrir_site",       # cible = domaine ou url
    "mail",
    "musique_jouer",
    "musique_pause",
    "musique_suivant",
    "musique_precedent",
    "volume",            # valeur = 0..100, ou cible = "monter"/"baisser"
    "luminosite",
    "minuteur",          # valeur = duree en secondes
    "note",              # cible = texte a noter
    "heure",
    "date",
    "meteo",
    "actu",
    "agenda_lire",
    "agenda_creer",
    "capture_ecran",
    "verrouiller",
    "recherche_web",     # cible = la requete
    "discussion",        # question ouverte, pas d'action
    "competence",        # cible = le nom d'une competence installee
    "inconnu",
}

SYSTEM_PROMPT = """Tu es le routeur d'intentions d'Ava, une assistante vocale française sur Mac.
On te donne une phrase dictée par l'utilisateur. Tu réponds UNIQUEMENT par un objet JSON :
{"intent": "<une valeur de la liste>", "cible": "<texte ou chaîne vide>", "valeur": <nombre ou null>, "confiance": <0 à 1>}

Valeurs autorisées pour "intent" :
ouvrir_app, ouvrir_site, mail, musique_jouer, musique_pause, musique_suivant,
musique_precedent, volume, luminosite, minuteur, note, heure, date, meteo, actu,
agenda_lire, agenda_creer, capture_ecran, verrouiller, recherche_web, discussion, inconnu

Règles :
- "valeur" porte les nombres : secondes pour minuteur, 0-100 pour volume et luminosité. Sinon null.
- "cible" porte le complément utile : nom d'application, requête de recherche, texte d'une note.
- Pour monter/baisser sans chiffre, mets "cible" à "monter" ou "baisser" et "valeur" à null.
- Une question de culture générale sans besoin d'actualité => "discussion".
- Une question qui demande une information récente ou locale => "recherche_web".
- "coupe", "coupe tout", "silence", "chut" veulent dire couper le SON (musique_pause), jamais verrouiller.
- "verrouiller" seulement si l'utilisateur parle explicitement de verrouiller, de l'écran de verrouillage ou de fermer sa session.
- Si tu hésites vraiment, réponds "inconnu" avec une confiance basse. Ne devine pas une action.

Exemples :
"baisse un peu le son" -> {"intent":"volume","cible":"baisser","valeur":null,"confiance":0.93}
"il me faudrait spotify" -> {"intent":"ouvrir_app","cible":"Spotify","valeur":null,"confiance":0.9}
"rappelle-moi dans un quart d'heure" -> {"intent":"minuteur","cible":"","valeur":900,"confiance":0.88}
"pourquoi le ciel est bleu" -> {"intent":"discussion","cible":"pourquoi le ciel est bleu","valeur":null,"confiance":0.92}
"le cours du bitcoin" -> {"intent":"recherche_web","cible":"cours du bitcoin","valeur":null,"confiance":0.9}
"note qu'il faut rappeler Léa" -> {"intent":"note","cible":"rappeler Léa","valeur":null,"confiance":0.94}"""

# les competences s'ajoutent au vol : on ne donne au modele que leur nom et leur
# description (etape « decouverte » du standard Agent Skills), jamais leurs
# instructions completes. c'est ce qui permet d'en avoir trente sans alourdir
# chaque classification.
SKILLS_PROMPT = """

Compétences installées. Si la demande correspond clairement à l'une d'elles,
réponds {{"intent":"competence","cible":"<nom exact de la compétence>","valeur":null,"confiance":<0 à 1>}}.
Sinon, ignore cette liste et classe normalement.

{catalogue}"""


def build_prompt(catalogue: str = "") -> str:
    """Le prompt du routeur, augmente des competences disponibles."""
    if not catalogue.strip():
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + SKILLS_PROMPT.format(catalogue=catalogue.strip())


DEFAULT_THRESHOLD = 0.55

# toutes les erreurs ne coutent pas pareil. se tromper sur « musique_suivant »
# fait sauter un morceau ; se tromper sur « verrouiller » jette Matheus dehors
# au milieu de son travail. les actions qu'on ne peut pas defaire d'un mot
# demandent donc une certitude nettement plus haute.
THRESHOLDS = {
    # une competence execute du code fourni par l'utilisateur : on ne la lance
    # pas sur une intuition tiede.
    "competence": 0.75,
    "verrouiller": 0.9,
    "agenda_creer": 0.85,
    "capture_ecran": 0.75,
    "luminosite": 0.75,
}


@dataclass(frozen=True)
class Understanding:
    intent: str = "inconnu"
    target: str = ""
    value: float | None = None
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        # en dessous, on prefere le comportement historique : mieux vaut une
        # recherche web qu'une action inventee sur le mac de quelqu'un.
        if self.intent not in INTENTS or self.intent == "inconnu":
            return False
        return self.confidence >= THRESHOLDS.get(self.intent, DEFAULT_THRESHOLD)


def api_key() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "intents.json"
CACHE_MAX_ENTRIES = 500


class IntentRouter:
    """Classifie une phrase, avec cache persistant et coupe-circuit.

    Le cache survit aux redemarrages : une tournure qu'Ava a deja comprise une
    fois ne repart plus sur le reseau. A l'usage, le vocabulaire de quelqu'un se
    stabilise vite, donc au bout de quelques jours l'essentiel des commandes
    « hors moule » repond instantanement et sans jeton.
    """

    def __init__(self, model: str = MODEL, timeout_s: float = TIMEOUT_S,
                 cache_path: Path | None = None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.cache_path = CACHE_PATH if cache_path is None else cache_path
        self._lock = threading.Lock()
        self._cache: dict[str, Understanding] = {}
        # apres un echec reseau, on arrete d'essayer un moment : sinon chaque
        # commande paie six secondes de timeout avant de retomber sur le filet.
        self._blocked_until = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            stored = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        for phrase, payload in stored.items():
            if not isinstance(phrase, str) or not isinstance(payload, dict):
                continue
            # on repasse par le meme controle que les reponses fraiches : un
            # fichier de cache modifie a la main ne doit pas pouvoir faire
            # executer une action qu'ava refuserait normalement.
            result = parse_understanding(json.dumps(payload))
            if result.usable:
                self._cache[phrase] = result

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                phrase: {"intent": item.intent, "cible": item.target,
                         "valeur": item.value, "confiance": item.confidence}
                for phrase, item in self._cache.items() if item.usable
            }
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            pass        # un cache qu'on n'arrive pas a ecrire n'empeche rien

    def available(self) -> bool:
        return (bool(api_key()) and time.monotonic() >= self._blocked_until
                and not net.is_offline())

    def knows(self, text: str) -> bool:
        """Cette tournure est-elle deja apprise ? (donc gratuite et instantanee)"""
        with self._lock:
            return str(text or "").strip().lower() in self._cache

    def understand(self, text: str, catalogue: str = "") -> Understanding:
        phrase = str(text or "").strip()
        if not phrase:
            return Understanding()
        # le cache passe **avant** la disponibilite : une tournure deja apprise
        # ne coute rien, elle doit donc marcher meme cle absente, coupe-circuit
        # ferme ou wifi coupe — c'etait tout l'interet de la garder sur disque,
        # et l'ordre inverse la rendait justement inutile quand elle servait.
        key = phrase.lower()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        if not self.available():
            return Understanding()
        try:
            result = self._classify(phrase, catalogue)
        except Exception as exc:  # noqa: BLE001
            print(f"[compréhension] indisponible : {exc}")
            net.note_failure("compréhension", exc)
            self._blocked_until = time.monotonic() + 60
            return Understanding()
        net.note_success("compréhension")
        with self._lock:
            if len(self._cache) > CACHE_MAX_ENTRIES:
                self._cache.clear()
            self._cache[key] = result
            if result.usable:
                self._save_cache()
        return result

    def _classify(self, phrase: str, catalogue: str = "") -> Understanding:
        response = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": build_prompt(catalogue)},
                    {"role": "user", "content": phrase},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 120,
            },
            timeout=net.timeout(self.timeout_s),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_understanding(content)


def parse_understanding(content: str) -> Understanding:
    """Lit la reponse du modele sans jamais faire confiance a sa forme."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return Understanding()
    if not isinstance(data, dict):
        return Understanding()

    intent = str(data.get("intent", "")).strip().lower()
    if intent not in INTENTS:
        intent = "inconnu"

    target = data.get("cible", "")
    # le modele glisse parfois un objet la ou on attend une chaine.
    target = str(target).strip()[:400] if isinstance(target, (str, int, float)) else ""

    value = data.get("valeur")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    else:
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            value = None

    try:
        confidence = float(data.get("confiance", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return Understanding(intent, target, value, confidence, data)


ROUTER = IntentRouter()
