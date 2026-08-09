"""les citations du briefing.

avant : une trentaine de formules anonymes du genre « crois en toi et tout
devient possible », piochees par `jour_de_l_annee % 30`. deux consequences —
elles sonnaient creux parce que personne ne les avait dites, et l'ordre etant
fixe, la meme revenait a date fixe.

ici, chaque citation a **un auteur**, et la rotation evite ce qui vient d'etre
dit. l'attribution est le point delicat : la moitie des citations qui circulent
sont mal attribuees. on s'en tient donc a des lignes dont l'origine est
documentee, quitte a avoir une liste plus courte. dans le doute, on retire.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
from pathlib import Path
import random
import threading

HERE = Path(__file__).resolve().parent
HISTORY_PATH = HERE / ".cache" / "quotes_seen.json"

# on garde en memoire les dernieres citations dites, pour ne pas tourner en
# rond sur trois formules quand ava est relancee plusieurs fois par jour.
HISTORY_SIZE = 12


@dataclass(frozen=True)
class Quote:
    text: str
    author: str

    def spoken(self) -> str:
        """Version parlee. Accents obligatoires : la synthese s'etrangle sans."""
        text = self.text.rstrip()
        if not text.endswith((".", "!", "?", "…")):
            text += "."
        return f"{text} {self.author}."


# ⚠️ « Et pour la route, cette phrase de X : … » revenait **mot pour mot** tous
# les matins. Une citation differente annoncee par la meme formule finit par
# s'entendre comme un jingle : on n'ecoute plus la phrase, on reconnait
# l'emballage. Les amorces tournent donc avec le jour, et l'auteur passe
# **apres** la citation — c'est la phrase qu'on veut entendre en premier, pas
# le nom propre, qui coupe l'elan quand il arrive en tete.
_QUOTE_INTROS = (
    "Et pour finir, {author} disait :",
    "Une phrase pour la route, de {author} :",
    "Je te laisse là-dessus, c'est de {author} :",
    "Et pour la route, cette phrase de {author} :",
    "Dernière chose, {author} :",
)


def spoken_intro(quote: "Quote", day: int | None = None) -> str:
    """La citation annoncee, avec une amorce qui change d'un jour a l'autre."""
    import datetime
    index = (datetime.date.today().toordinal() if day is None else int(day)) % len(_QUOTE_INTROS)
    text = quote.text.rstrip().rstrip(".")
    return f"{_QUOTE_INTROS[index].format(author=quote.author)} {text}."


# --- le fonds ----------------------------------------------------------------
# volontairement resserre sur des citations verifiables. celles dont
# l'attribution est contestee (le « un objectif sans plan » de Saint-Exupery, le
# « nous sommes ce que nous faisons de facon repetee » d'Aristote qui est en
# realite de Will Durant, la « chance = preparation + opportunite » de Seneque)
# ont ete ecartees plutot que reattribuees au petit bonheur.
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
    """Une citation qui n'a pas ete dite recemment."""
    seen = set(_load_history() if history is None else history)
    candidates = [quote for quote in pool if quote.text not in seen] or list(pool)
    choose = chooser or random.SystemRandom().choice
    return choose(candidates)


def daily() -> Quote:
    """Pioche une citation et la note comme dite. Reserve aux cas ponctuels."""
    quote = pick()
    _remember(quote.text)
    return quote


TODAY_PATH = HERE / ".cache" / "quote_today.json"

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
    """**La** citation du jour : la meme du matin au soir.

    Trois raisons de ne pas repiocher a chaque appel. Le briefing est refabrique
    toutes les 15 minutes (`keep_welcome_warm`), donc avec `daily()` la
    « citation du jour » changeait quatre fois par heure. La scene de demarrage
    et le texte parle piochaient chacun de leur cote, si bien que l'ecran
    affichait une citation et qu'Ava en prononcait une autre. Et le choix est
    garde sur le disque, sinon un simple redemarrage en changeait encore.
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
