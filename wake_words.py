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


DEFAULT_WAKE_PHRASES = ("bonjour ava", "ok ava")

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
    triggers, names = set(), set()
    for phrase in phrases:
        toks = normalize_speech(str(phrase)).split()
        if not toks:
            continue
        names.add(toks[-1])
        for tok in toks[:-1]:
            triggers.add(tok)
            for base, syns in TRIGGER_SYNONYMS.items():
                if tok == base or tok in syns:
                    triggers.update(syns)
    return triggers, names


def _name_like(tok: str, names) -> bool:
    if tok in AVA_SPELLINGS:
        return True
    return any(_lev(tok, n) <= 1 for n in names)


def extract_wake(text: str, phrases=DEFAULT_WAKE_PHRASES) -> WakeMatch:
    toks = normalize_speech(text).split()
    if not toks:
        return WakeMatch(False)
    triggers, names = _phrase_parts(phrases)
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
    return WakeMatch(False)


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
        match = extract_wake(text, phrases)
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
