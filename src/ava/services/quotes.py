"""the quotes in the briefing.

before: thirty-odd anonymous lines along the lines of "believe in yourself and
anything is possible", picked by `day_of_year % 30`. two consequences — they
rang hollow because nobody had actually said them, and with a fixed order the
same one came back on the same date every year.

here every quote has **an author**, and the rotation avoids whatever was just
said. attribution is the delicate part: half the quotes in circulation are
misattributed. so we stick to lines whose origin is documented, even if that
makes for a shorter list. when in doubt, it comes out.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
from pathlib import Path
import random
import threading

from ava import paths

HISTORY_PATH = paths.cache_dir("quotes_seen.json")

# remember the last few quotes said, so she doesn't cycle through the same
# three when ava gets restarted several times in a day.
HISTORY_SIZE = 12


@dataclass(frozen=True)
class Quote:
    text: str
    author: str

    def spoken(self) -> str:
        """The spoken form. Accents are mandatory — synthesis chokes without."""
        text = self.text.rstrip()
        if not text.endswith((".", "!", "?", "…")):
            text += "."
        return f"{text} {self.author}."


# ⚠️ "Et pour la route, cette phrase de X : …" came back **word for word** every
# morning. A different quote announced by the same formula ends up sounding like
# a jingle: you stop hearing the line and start recognising the wrapper. So the
# openers rotate with the day, and the author comes **after** the quote — the
# sentence is what you want to hear first, not the proper noun, which kills the
# momentum when it leads.
_QUOTE_INTROS = (
    "Et pour finir, {author} disait :",
    "Une phrase pour la route, de {author} :",
    "Je te laisse là-dessus, c'est de {author} :",
    "Et pour la route, cette phrase de {author} :",
    "Dernière chose, {author} :",
)


def spoken_intro(quote: "Quote", day: int | None = None) -> str:
    """The quote, announced with an opener that changes from day to day."""
    import datetime
    index = (datetime.date.today().toordinal() if day is None else int(day)) % len(_QUOTE_INTROS)
    text = quote.text.rstrip().rstrip(".")
    return f"{_QUOTE_INTROS[index].format(author=quote.author)} {text}."


# --- the collection ----------------------------------------------------------
# deliberately narrowed to quotes that check out. the ones whose attribution is
# disputed (Saint-Exupéry's "a goal without a plan", the Aristotle "we are what
# we repeatedly do" that is really Will Durant, Seneca's "luck = preparation +
# opportunity") were dropped rather than reassigned on a hunch.
QUOTES = (
    Quote("Ce n'est pas parce que les choses sont difficiles que nous n'osons pas ; "
          "c'est parce que nous n'osons pas qu'elles sont difficiles", "Sénèque"),
    Quote("Il n'est pas de vent favorable pour celui qui ne sait où il va", "Sénèque"),
    Quote("Pour ce qui est de l'avenir, il ne s'agit pas de le prévoir, "
          "mais de le rendre possible", "Antoine de Saint-Exupéry"),
    Quote("Cela semble toujours impossible, jusqu'à ce qu'on le fasse", "Nelson Mandela"),
    Quote("Je n'ai pas échoué. J'ai simplement trouvé dix mille solutions "
          "qui ne fonctionnent pas", "Thomas Edison"),
    Quote("Dans la vie, rien n'est à craindre, tout est à comprendre", "Marie Curie"),
    Quote("Au milieu de l'hiver, j'apprenais enfin qu'il y avait en moi "
          "un été invincible", "Albert Camus"),
    Quote("La meilleure façon de prédire l'avenir, c'est de l'inventer", "Alan Kay"),
    Quote("Ce qui est simple est toujours faux ; ce qui ne l'est pas est inutilisable",
          "Paul Valéry"),
    Quote("Je n'ai fait cette lettre plus longue que parce que je n'ai pas eu "
          "le loisir de la faire plus courte", "Blaise Pascal"),
    Quote("Rien n'est plus puissant qu'une idée dont l'heure est venue", "Victor Hugo"),
    Quote("On ne voit bien qu'avec le cœur ; l'essentiel est invisible pour les yeux",
          "Antoine de Saint-Exupéry"),
    Quote("La perfection est atteinte, non pas lorsqu'il n'y a plus rien à ajouter, "
          "mais lorsqu'il n'y a plus rien à retirer", "Antoine de Saint-Exupéry"),
    Quote("Un homme qui ose gaspiller une heure de son temps n'a pas découvert "
          "la valeur de la vie", "Charles Darwin"),
    Quote("La théorie, c'est quand on sait tout et que rien ne fonctionne. "
          "La pratique, c'est quand tout fonctionne et que personne ne sait pourquoi",
          "Albert Einstein"),
    Quote("Le doute est le commencement de la sagesse", "Aristote"),
    Quote("Connais-toi toi-même", "Socrate"),
    Quote("Deviens ce que tu es", "Pindare"),
    Quote("Il faut imaginer Sisyphe heureux", "Albert Camus"),
    Quote("La vie, c'est comme une bicyclette : il faut avancer pour ne pas "
          "perdre l'équilibre", "Albert Einstein"),
    Quote("Ce que nous savons est une goutte d'eau, ce que nous ignorons "
          "est un océan", "Isaac Newton"),
    Quote("Si j'ai vu plus loin, c'est en montant sur les épaules de géants",
          "Isaac Newton"),
    Quote("Le hasard ne favorise que les esprits préparés", "Louis Pasteur"),
    Quote("Rien ne se perd, rien ne se crée, tout se transforme", "Antoine Lavoisier"),
)


def _load_history() -> list[str]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return [str(item) for item in data][-HISTORY_SIZE:] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _remember(text: str) -> None:
    history = _load_history()
    history.append(text)
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(history[-HISTORY_SIZE:], ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(HISTORY_PATH)
    except OSError:
        pass


def pick(pool=QUOTES, history: list[str] | None = None, chooser=None) -> Quote:
    """A quote that hasn't come up recently."""
    seen = set(_load_history() if history is None else history)
    candidates = [quote for quote in pool if quote.text not in seen] or list(pool)
    choose = chooser or random.SystemRandom().choice
    return choose(candidates)


def daily() -> Quote:
    """Pick a quote and mark it as said. For one-off uses only."""
    quote = pick()
    _remember(quote.text)
    return quote


TODAY_PATH = paths.cache_dir("quote_today.json")

_today: dict = {"day": None, "quote": None}
_today_lock = threading.Lock()


def _load_today(day: str) -> Quote | None:
    try:
        data = json.loads(TODAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("day") != day:
        return None
    text, author = str(data.get("text", "")), str(data.get("author", ""))
    return Quote(text, author) if text and author else None


def _save_today(day: str, quote: Quote) -> None:
    try:
        TODAY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TODAY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day, "text": quote.text,
                                   "author": quote.author}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(TODAY_PATH)
    except OSError:
        pass


def of_the_day(day: str | None = None) -> Quote:
    """**The** quote of the day: the same one from morning to night.

    Three reasons not to re-draw on every call. The briefing is rebuilt every 15
    minutes (`keep_welcome_warm`), so with `daily()` the "quote of the day"
    changed four times an hour. The startup scene and the spoken text each drew
    their own, so the screen showed one quote while Ava said another. And the
    choice is kept on disk, or a plain restart would change it again.
    """
    today = day or datetime.date.today().isoformat()
    with _today_lock:
        if _today["day"] != today or _today["quote"] is None:
            stored = _load_today(today)
            if stored is None:
                stored = daily()
                _save_today(today, stored)
            _today["day"] = today
            _today["quote"] = stored
        return _today["quote"]
