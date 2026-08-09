"""detection testable des phrases de reveil d'ava."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
import unicodedata


@dataclass(frozen=True)
class WakeMatch:
    detected: bool
    phrase: str = ""
    trailing_command: str = ""


def normalize_speech(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


DEFAULT_WAKE_PHRASES = ("bonjour ava", "ok ava", "ava")

# vosk-small entend rarement "ok ava" pile : on tolere les variantes proches.
TRIGGER_SYNONYMS = {
    "ok": {"ok", "okay", "okey", "okai", "oke", "o", "oh", "ho", "hey", "hei"},
    "bonjour": {"bonjour", "bonsoir", "salut", "coucou"},
    "hey": {"hey", "he", "hei"},
    "dis": {"dis"},
}
# orthographes que vosk crache pour "ava"
AVA_SPELLINGS = {"ava", "avas", "avat", "avah", "aya", "eva", "evas", "hava", "avac"}


def _lev(a: str, b: str) -> int:
    # distance d'edition (petite), pour tolerer une lettre de travers
    if a == b:
        return 0
    if not a or not b:
        return len(a) + len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _phrase_parts(phrases):
    # separe chaque phrase en declencheurs (debut) + nom (dernier mot),
    # et enrichit les declencheurs avec leurs synonymes phonetiques.
    # standalone = noms qui peuvent reveiller seuls (phrase = juste "ava").
    triggers, names, standalone = set(), set(), set()
    for phrase in phrases:
        toks = normalize_speech(str(phrase)).split()
        if not toks:
            continue
        names.add(toks[-1])
        if len(toks) == 1:            # phrase reduite au nom -> reveil "ava" seul
            standalone.add(toks[-1])
        for tok in toks[:-1]:
            triggers.add(tok)
            for base, syns in TRIGGER_SYNONYMS.items():
                if tok == base or tok in syns:
                    triggers.update(syns)
    return triggers, names, standalone


def _name_like(tok: str, names) -> bool:
    if tok in AVA_SPELLINGS:
        return True
    return any(_lev(tok, n) <= 1 for n in names)


def _standalone_wake(toks, standalone) -> WakeMatch:
    # reveil sur le nom dit SEUL ("ava"). on n'accepte que si toute la phrase
    # est le nom : "ava" tout court reveille, mais "ava est un joli nom" (ou une
    # commande dans la foulee) non -> l'utilisateur dit "ava", ava repond, puis
    # ecoute la demande. evite les faux positifs des qu'on parle d'ava.
    if not standalone:
        return WakeMatch(False)
    if toks == ["a", "va"]:                 # vosk coupe parfois "ava" en deux
        return WakeMatch(True, "ava")
    if all(_name_like(tok, standalone) for tok in toks):
        return WakeMatch(True, toks[0])
    return WakeMatch(False)


def extract_wake(text: str, phrases=DEFAULT_WAKE_PHRASES, *,
                 allow_standalone: bool = True) -> WakeMatch:
    toks = normalize_speech(text).split()
    if not toks:
        return WakeMatch(False)
    triggers, names, standalone = _phrase_parts(phrases)
    n = len(toks)
    for i, tok in enumerate(toks):
        if tok not in triggers:
            continue
        # le nom juste apres (ok ava) : tolerance a 1 lettre pres.
        if i + 1 < n and _name_like(toks[i + 1], names):
            trailing = " ".join(toks[i + 2:]).strip()
            return WakeMatch(True, f"{tok} {toks[i + 1]}", trailing)
        # avec un mot d'ecart (ok euh ava) : la il faut le nom EXACT, sinon
        # "ok on va" ou "ok ca va" declencheraient en pleine conversation.
        if i + 2 < n and (toks[i + 2] in names or toks[i + 2] in AVA_SPELLINGS):
            trailing = " ".join(toks[i + 3:]).strip()
            return WakeMatch(True, f"{tok} {toks[i + 2]}", trailing)
        # cas ou "ava" est coupe en deux : "a va"
        if i + 2 < n and toks[i + 1] == "a" and toks[i + 2] == "va":
            trailing = " ".join(toks[i + 3:]).strip()
            return WakeMatch(True, f"{tok} ava", trailing)
    # aucun declencheur : reste le reveil sur le nom dit seul ("ava").
    if not allow_standalone:
        return WakeMatch(False)
    return _standalone_wake(toks, standalone)


def strip_wake_prefix(text: str, phrases=DEFAULT_WAKE_PHRASES):
    # enleve un eventuel mot de reveil en tete d'une commande transcrite
    # ("ok ava ouvre spotify" -> "ouvre spotify"). plus genereux que le reveil :
    # on nettoie tous les prefixes connus (ok/bonjour/hey + ava), meme si le
    # reveil courant ne se declenche que sur "ava" seul. renvoie None si rien a couper.
    toks = normalize_speech(text).split()
    if not toks:
        return None
    triggers, names, standalone = _phrase_parts(phrases)
    allnames = names | standalone | AVA_SPELLINGS
    i = 0
    while i < len(toks) and toks[i] in triggers:
        i += 1
    if i < len(toks) and _name_like(toks[i], allnames):
        j = i + 1
    elif i + 1 < len(toks) and toks[i] == "a" and toks[i + 1] == "va":
        j = i + 2
    else:
        return None                       # pas de nom apres -> pas un prefixe de reveil
    return " ".join(toks[j:]).strip()


def strip_wake_suffix(text: str, phrases=DEFAULT_WAKE_PHRASES) -> str:
    """Enleve le nom dit **en fin** de phrase (« salut ava », « merci ava »).

    On appelle son assistante par son nom aussi souvent a la fin qu'au debut.
    `strip_wake_prefix` ne nettoyait que la tete : « salut ava » n'etait donc
    egal a aucune politesse connue et partait en recherche web, ce qui n'a pas
    de sens pour un mot qu'ava vient elle-meme d'entendre comme son nom.

    On garde le texte tel quel si le nom est le seul mot : « ava » dit seul est
    un appel, pas une commande vide — c'est a l'appelant de le traiter.

    ⚠️ Ici on exige l'**orthographe exacte**, pas la ressemblance de
    `_name_like` : « va » n'est qu'a une lettre d'« ava », donc « ça va »
    devenait « ça » et ne ressemblait plus a rien. En tete de phrase le flou est
    tenable parce qu'un mot de reveil precede ; en fin de phrase il n'y a aucun
    contexte pour trancher, alors on ne devine pas.
    """
    toks = normalize_speech(text).split()
    if len(toks) < 2:
        return " ".join(toks)
    _triggers, names, standalone = _phrase_parts(phrases)
    allnames = (names | standalone | AVA_SPELLINGS) - {"va"}
    if toks[-1] in allnames:
        return " ".join(toks[:-1]).strip()
    if len(toks) > 2 and toks[-2] == "a" and toks[-1] == "va":
        return " ".join(toks[:-2]).strip()
    return " ".join(toks)


class PartialWakeGate:
    """declenche un mot de reveil partiel seulement s'il reste stable un instant.

    vosk n'envoie un resultat final qu'apres une pause assez longue. ce petit garde
    rend "bonjour ava" reactif, tout en laissant une commande prononcee dans la meme
    phrase arriver jusqu'au resultat final.
    """

    def __init__(self, hold_seconds: float = 0.20) -> None:
        self.hold_seconds = max(0.0, float(hold_seconds))
        self._candidate = ""
        self._since = 0.0

    def reset(self) -> None:
        self._candidate = ""
        self._since = 0.0

    def feed(self, text: str, phrases=DEFAULT_WAKE_PHRASES,
             now: float | None = None) -> WakeMatch:
        # jamais le nom tout seul sur un partiel : vosk-small fabrique « ava »
        # a partir d'a peu pres n'importe quel bruit de bureau, et ca reveillait
        # Ava toutes les deux minutes. « ava » seul attend donc le resultat
        # final, ou la reconnaissance est stabilisee.
        match = extract_wake(text, phrases, allow_standalone=False)
        candidate = normalize_speech(text)
        if not match.detected or match.trailing_command or not candidate:
            self.reset()
            return WakeMatch(False)

        instant = time.monotonic() if now is None else float(now)
        if candidate != self._candidate:
            self._candidate = candidate
            self._since = instant
            return WakeMatch(False)
        if instant - self._since < self.hold_seconds:
            return WakeMatch(False)

        self.reset()
        return match
