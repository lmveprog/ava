"""ava's wake phrases, detected in a way that can actually be tested."""

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

# vosk-small rarely hears "ok ava" exactly, so near misses are allowed through.
TRIGGER_SYNONYMS = {
    "ok": {"ok", "okay", "okey", "okai", "oke", "o", "oh", "ho", "hey", "hei"},
    "bonjour": {"bonjour", "bonsoir", "salut", "coucou"},
    "hey": {"hey", "he", "hei"},
    "dis": {"dis"},
}
# the spellings vosk coughs up for "ava"
AVA_SPELLINGS = {"ava", "avas", "avat", "avah", "aya", "eva", "evas", "hava", "avac"}


def _lev(a: str, b: str) -> int:
    # a small edit distance, so one letter out of place doesn't matter
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
    # split each phrase into triggers (the start) and the name (last word), then
    # widen the triggers with their phonetic near-neighbours.
    # standalone = names allowed to wake her on their own (phrase is just "ava").
    triggers, names, standalone = set(), set(), set()
    for phrase in phrases:
        toks = normalize_speech(str(phrase)).split()
        if not toks:
            continue
        names.add(toks[-1])
        if len(toks) == 1:            # phrase is just the name -> "ava" alone wakes
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
    # waking on the name said ALONE ("ava"). only accepted when the whole
    # sentence is the name: "ava" on its own wakes her, "ava est un joli nom"
    # (or a command straight after) does not -> you say "ava", she answers, then
    # listens for the request. keeps her from firing whenever you mention her.
    if not standalone:
        return WakeMatch(False)
    if toks == ["a", "va"]:                 # vosk sometimes splits "ava" in two
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
        # the name right after (ok ava): one letter of slack.
        if i + 1 < n and _name_like(toks[i + 1], names):
            trailing = " ".join(toks[i + 2:]).strip()
            return WakeMatch(True, f"{tok} {toks[i + 1]}", trailing)
        # with a word in between (ok euh ava): here the name has to be EXACT, or
        # "ok on va" and "ok ca va" would fire mid-conversation.
        if i + 2 < n and (toks[i + 2] in names or toks[i + 2] in AVA_SPELLINGS):
            trailing = " ".join(toks[i + 3:]).strip()
            return WakeMatch(True, f"{tok} {toks[i + 2]}", trailing)
        # the case where "ava" got split in two: "a va"
        if i + 2 < n and toks[i + 1] == "a" and toks[i + 2] == "va":
            trailing = " ".join(toks[i + 3:]).strip()
            return WakeMatch(True, f"{tok} ava", trailing)
    # no trigger at all: the name said on its own is the last chance ("ava").
    if not allow_standalone:
        return WakeMatch(False)
    return _standalone_wake(toks, standalone)


def strip_wake_prefix(text: str, phrases=DEFAULT_WAKE_PHRASES):
    # strip a wake word off the front of a transcribed command
    # ("ok ava ouvre spotify" -> "ouvre spotify"). more generous than waking: we
    # clean every known prefix (ok/bonjour/hey + ava) even if the current wake
    # rule only fires on "ava" alone. returns None when there's nothing to cut.
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
        return None                       # no name after it -> not a wake prefix
    return " ".join(toks[j:]).strip()


def strip_wake_suffix(text: str, phrases=DEFAULT_WAKE_PHRASES) -> str:
    """Strip the name off the **end** of a sentence ("salut ava", "merci ava").

    People call their assistant by name at the end as often as at the start.
    `strip_wake_prefix` only cleaned the front, so "salut ava" matched none of
    the known greetings and went off to a web search — which makes no sense for
    a word ava herself just recognised as her own name.

    The text is left alone when the name is the only word: "ava" on its own is
    someone calling, not an empty command, and it's the caller's job to handle.

    ⚠️ Here we demand the **exact spelling**, not `_name_like`'s resemblance:
    "va" is one letter from "ava", so "ça va" turned into "ça" and stopped
    meaning anything. At the front of a sentence the fuzziness holds up because
    a wake word comes first; at the end there's no context to lean on, so we
    don't guess.
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
    """Fires on a partial wake word only once it has held still for a moment.

    Vosk only sends a final result after a fairly long pause. This little gate
    keeps "bonjour ava" responsive while still letting a command spoken in the
    same breath reach the final result.
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
        # never the bare name on a partial: vosk-small manufactures "ava" out of
        # just about any office noise, and it was waking her every two minutes.
        # "ava" alone therefore waits for the final result, where recognition has
        # settled.
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
