"""understanding a command when the keywords weren't enough.

ava's routing is deterministic: a list of verbs, prefixes and trigger words.
it's instant and costs nothing, so it stays the main path. but anything outside
the mould used to fall into the last net — the web search — and she answered
beside the point:

    "baisse un peu le son"                 -> web search
    "il me faudrait spotify"               -> web search
    "rappelle-moi dans un quart d'heure"   -> web search

this module doesn't replace the routing: it slots in **just before** that last
net. so the network is only called for sentences that were going to be handled
badly anyway, and everyday commands keep their zero latency.

model: `ministral-8b-latest`. measured on 8 august: 0.39 s per classification,
against 0.54 s for the 3b, which also produced messier json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time

import requests

from ava import paths
from ava import net as net

MODEL = os.getenv("AVA_NLU_MODEL", "ministral-8b-latest").strip()
BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()
TIMEOUT_S = 6.0

# the list is closed: every entry maps to a capability that really exists in
# `_dispatch_command`. a model inventing "commander_pizza" would have us promise
# an action ava has no idea how to perform.
INTENTS = {
    "ouvrir_app",        # cible = the application name
    "ouvrir_site",       # cible = a domain or url
    "mail",
    "lire_mails",      # read the unread out loud
    "musique_jouer",
    "musique_pause",
    "musique_suivant",
    "musique_precedent",
    "volume",            # valeur = 0..100, or cible = "monter"/"baisser"
    "luminosite",
    "minuteur",          # valeur = duration in seconds
    "note",              # cible = the text to jot down
    "retenir",           # cible = a fact to keep in long-term memory
    "heure",
    "date",
    "meteo",
    "actu",
    "agenda_lire",
    "agenda_creer",
    "capture_ecran",
    "verrouiller",
    "recherche_web",     # cible = the query
    "discussion",        # an open question, no action
    "competence",        # cible = the name of an installed skill
    "inconnu",
}

SYSTEM_PROMPT = """Tu es le routeur d'intentions d'Ava, une assistante vocale française sur Mac.
On te donne une phrase dictée par l'utilisateur. Tu réponds UNIQUEMENT par un objet JSON :
{"intent": "<une valeur de la liste>", "cible": "<texte ou chaîne vide>", "valeur": <nombre ou null>, "confiance": <0 à 1>}

Valeurs autorisées pour "intent" :
ouvrir_app, ouvrir_site, mail, lire_mails, musique_jouer, musique_pause, musique_suivant,
musique_precedent, volume, luminosite, minuteur, note, retenir, heure, date, meteo, actu,
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
"note qu'il faut rappeler Léa" -> {"intent":"note","cible":"rappeler Léa","valeur":null,"confiance":0.94}
"j'ai reçu des mails ce matin ?" -> {"intent":"lire_mails","cible":"","valeur":null,"confiance":0.9}
"retiens que la voiture est au niveau 2" -> {"intent":"retenir","cible":"la voiture est au niveau 2","valeur":null,"confiance":0.93}"""

# skills are appended on the fly: the model only ever gets their name and
# description (the standard's discovery step), never their full instructions.
# that's what makes thirty of them possible without weighing down every
# classification.
SKILLS_PROMPT = """

Compétences installées. Si la demande correspond clairement à l'une d'elles,
réponds {{"intent":"competence","cible":"<nom exact de la compétence>","valeur":null,"confiance":<0 à 1>}}.
Sinon, ignore cette liste et classe normalement.

{catalogue}"""


def build_prompt(catalogue: str = "") -> str:
    """The router prompt, widened with whatever skills are installed."""
    if not catalogue.strip():
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + SKILLS_PROMPT.format(catalogue=catalogue.strip())


DEFAULT_THRESHOLD = 0.55

# not every mistake costs the same. getting "musique_suivant" wrong skips a
# track; getting "verrouiller" wrong throws you out of your session mid-work.
# actions you can't undo with a word therefore demand much more certainty.
THRESHOLDS = {
    # a skill runs code the user supplied, so we don't fire one on a hunch.
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
        # below that we'd rather have the old behaviour: a web search beats an
        # invented action on somebody's mac.
        if self.intent not in INTENTS or self.intent == "inconnu":
            return False
        return self.confidence >= THRESHOLDS.get(self.intent, DEFAULT_THRESHOLD)


def api_key() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


CACHE_PATH = paths.cache_dir("intents.json")
CACHE_MAX_ENTRIES = 500


class IntentRouter:
    """Classify a sentence, with a persistent cache and a breaker.

    The cache survives restarts: a phrasing Ava has understood once never goes
    back to the network. In practice someone's vocabulary settles fast, so after
    a few days most of the off-pattern commands answer instantly and for free.
    """

    def __init__(self, model: str = MODEL, timeout_s: float = TIMEOUT_S,
                 cache_path: Path | None = None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.cache_path = CACHE_PATH if cache_path is None else cache_path
        self._lock = threading.Lock()
        self._cache: dict[str, Understanding] = {}
        # after a network failure we stop trying for a while, or every command
        # pays six seconds of timeout before falling back to the net.
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
            # run it through the same checks as a fresh answer: a cache file
            # edited by hand must not be able to trigger an action ava would
            # normally refuse.
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
            pass        # a cache we can't write stops nothing

    def available(self) -> bool:
        return (bool(api_key()) and time.monotonic() >= self._blocked_until
                and not net.is_offline())

    def knows(self, text: str) -> bool:
        """Has this phrasing been learned already? (so free and instant)"""
        with self._lock:
            return str(text or "").strip().lower() in self._cache

    def understand(self, text: str, catalogue: str = "") -> Understanding:
        phrase = str(text or "").strip()
        if not phrase:
            return Understanding()
        # the cache comes **before** the availability check: a learned phrasing
        # costs nothing, so it has to work with no key, with the breaker open or
        # with the wifi down — that was the whole point of keeping it on disk,
        # and the other order made it useless exactly when it mattered.
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
    """Read the model's answer without ever trusting its shape."""
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
    # the model sometimes slips an object in where a string is expected.
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
