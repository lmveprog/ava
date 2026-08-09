"""l'actualite ia du briefing : fraiche, sourcee, et dite en francais.

l'ancienne version raclait deux pages html (mistral, openai) et prenait le
premier lien datable. trois defauts, tous audibles au reveil :

- **rien ne garantissait la fraicheur.** le 8 aout, ava annoncait une annonce du
  4 aout comme « l'actualite » — et sans nouvelle publication, elle aurait
  ressorti la meme pendant des semaines.
- **la traduction passait par ollama**, qui ne tourne pas. les titres anglais
  partaient donc tels quels dans une voix francaise : « Continuous voice
  interaction with GPT Live » lu par une voix fr, c'est du bruit.
- **la phrase se repetait** : « Mistral AI presente Shieldstral., selon
  Mistral AI. »

ici on lit des **flux rss** (vraies dates de publication, pas de scraping), on
refuse ce qui est trop vieux, on ecarte les billets de communication client, et
on traduit avec le meme petit modele que le routage d'intentions.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import email.utils
import json
import os
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ET

import requests

import net

HERE = Path(__file__).resolve().parent
CACHE_PATH = HERE / ".cache" / "ai_news.json"
TRANSLATIONS_PATH = HERE / ".cache" / "ai_news_titles.json"

USER_AGENT = "Mozilla/5.0 Ava/1.0"
ATOM = "{http://www.w3.org/2005/Atom}"

# les laboratoires d'abord (ils annoncent chez eux avant tout le monde), puis
# deux sources de contexte. tous verifies : ce sont de vrais flux rss/atom.
FEEDS = (
    ("Mistral AI", "https://mistral.ai/rss.xml"),
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google", "https://blog.google/technology/ai/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
)

# au-dela, ce n'est plus une actualite : mieux vaut le dire que de faire passer
# une annonce de la semaine derniere pour la nouvelle du jour.
MAX_AGE_DAYS = 6
FRESH_HOURS = 48          # en dessous, on considere que c'est vraiment chaud

# le cache n'est la que pour ne pas rappeler six flux a chaque reveil ; il doit
# perimer vite, sinon on retombe sur le probleme qu'on essaie de corriger.
CACHE_TTL_S = 3 * 3600


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime.datetime | None = None

    @property
    def age_hours(self) -> float:
        if self.published is None:
            return 1e9
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (now - self.published).total_seconds() / 3600)


def _parse_date(raw: str) -> datetime.datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    for parse in (email.utils.parsedate_to_datetime,
                  lambda v: datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))):
        try:
            moment = parse(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment
    return None


def _feed_items(source: str, url: str, timeout: float = 10.0) -> list[NewsItem]:
    response = requests.get(url, timeout=net.timeout(timeout),
                            headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items: list[NewsItem] = []
    # rss met <item>, atom met <entry> : on accepte les deux sans discuter.
    for node in (root.findall(".//item") or root.findall(f".//{ATOM}entry"))[:12]:
        title = (node.findtext("title") or node.findtext(f"{ATOM}title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not link:
            anchor = node.find(f"{ATOM}link")
            link = anchor.get("href", "") if anchor is not None else ""
        published = _parse_date(
            node.findtext("pubDate") or node.findtext("published")
            or node.findtext(f"{ATOM}published") or node.findtext(f"{ATOM}updated") or "")
        title = " ".join(title.split())
        if title and link:
            items.append(NewsItem(title[:220], source, link[:1200], published))
    return items


def fetch_items() -> list[NewsItem]:
    """Tous les flux, a plat, du plus recent au plus ancien.

    Les six flux partent **ensemble** : lus l'un apres l'autre, un seul serveur
    lent imposait son attente a tous les suivants, et le pire cas atteignait
    six fois le timeout — juste pour une phrase du briefing.
    """
    if not net.reachable("actu"):
        return []
    from concurrent.futures import ThreadPoolExecutor

    def one(entry) -> list[NewsItem]:
        source, url = entry
        try:
            return _feed_items(source, url)
        except Exception as exc:  # noqa: BLE001 - un flux mort ne prive pas du reste
            net.note_failure("actu", exc)
            return []

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        batches = list(pool.map(one, FEEDS))
    collected = [item for batch in batches for item in batch]
    if collected:
        net.note_success("actu")
    collected.sort(key=lambda item: item.age_hours)
    return collected


# les laboratoires publient aussi beaucoup de communication : temoignages
# clients, partenariats, billets rh. ce n'est pas ce qu'on veut entendre au
# reveil, meme si c'est publie du jour.
_FLUFF = (
    "how ", "working with", "from asking to doing", "putting ", " to work",
    "customer", "partnership", "webinar", "hiring", "we're joining",
    "case study", "testimonial", "builds ai capabilities", "expanding access",
)
_SUBSTANCE = (
    "introducing", "announcing", "launch", "release", "model", "gpt", "stral",
    "gemini", "claude", "research", "benchmark", "open source", "open-source",
    "agent", "reasoning", "multimodal", "breakthrough", "state of the art",
)


def relevance(title: str) -> int:
    """Plus c'est haut, plus ca merite d'etre la nouvelle du jour."""
    low = f" {title.lower()} "
    score = 0
    for marker in _SUBSTANCE:
        if marker in low:
            score += 2
    for marker in _FLUFF:
        if marker in low:
            score -= 3
    return score


def same_story(first: str, second: str) -> bool:
    ignored = {"with", "that", "this", "from", "your", "their", "intelligence",
               "artificielle", "avec", "pour", "dans"}
    def tokens(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
                if len(token) > 3 and token not in ignored}
    a, b = tokens(first), tokens(second)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.55


def pick(items: list[NewsItem], previous_title: str = "") -> NewsItem | None:
    """La meilleure nouvelle recente, en evitant celle deja racontee.

    On trie par pertinence *a l'interieur* de la fenetre de fraicheur, jamais
    l'inverse : une annonce majeure d'il y a cinq jours ne doit pas passer
    devant une vraie nouvelle d'hier.
    """
    recent = [item for item in items if item.age_hours <= MAX_AGE_DAYS * 24]
    if not recent:
        return None
    fresh = [item for item in recent if item.age_hours <= FRESH_HOURS] or recent
    unseen = [item for item in fresh if not same_story(item.title, previous_title)]
    pool = unseen or fresh
    return max(pool, key=lambda item: (relevance(item.title), -item.age_hours))


def freshness_phrase(published: datetime.datetime | None,
                     now: datetime.datetime | None = None) -> str:
    """« ce matin », « hier »… — sans quoi une annonce de mardi sonne comme du jour."""
    if published is None:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    local_now = now.astimezone()
    local = published.astimezone()
    days = (local_now.date() - local.date()).days
    if days <= 0:
        return "ce matin" if local.hour < 13 else "dans la journée"
    if days == 1:
        return "hier"
    if days == 2:
        return "avant-hier"
    return f"il y a {days} jours"


# --- traduction des titres ----------------------------------------------------

_translations_lock = threading.Lock()
_translations: dict[str, str] | None = None

TRANSLATE_MODEL = os.getenv("AVA_NLU_MODEL", "ministral-8b-latest").strip()
BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()

# ⚠️ un titre de presse n'est pas une phrase. « Réponse aux défis cyber
# stratégiques futurs avec les capacités avancées » est un groupe nominal sans
# verbe : ça se lit très bien des yeux et ça ne se dit pas. À l'oral, on attend
# un sujet et un verbe — d'où une reformulation, et pas seulement une traduction.
TRANSLATE_PROMPT = (
    "Réécris ce titre d'actualité en UNE phrase française, dite à l'oral, "
    "avec un sujet et un verbe conjugué, 20 mots maximum. "
    "Commence par l'entreprise ou le produit concerné quand c'est possible. "
    "N'ajoute, ne retire et n'invente aucune information. Garde les noms de "
    "produits et d'entreprises tels quels, et écris les accents. "
    "Réponds uniquement par la phrase, sans guillemets et sans point final."
)


# ⚠️ le modele repond en markdown des qu'il veut insister : « savent *quand*
# intervenir ». A l'ecran ca passe ; a la voix, la synthese prononce
# « asterisque ». On enleve toute la ponctuation d'emphase avant de garder la
# phrase — c'est du texte destine a etre **dit**, jamais affiche seul.
_EMPHASIS = str.maketrans({"*": None, "_": None, "`": None, "#": None})


def _plain_speech(value: str) -> str:
    cleaned = " ".join(str(value or "").translate(_EMPHASIS).split())
    return cleaned.strip(' "«»').rstrip(".")


def _load_translations() -> dict[str, str]:
    global _translations
    if _translations is None:
        try:
            data = json.loads(TRANSLATIONS_PATH.read_text(encoding="utf-8"))
            _translations = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _translations = {}
    return _translations


def _remember_translation(source_title: str, translated: str) -> None:
    table = _load_translations()
    table[source_title] = translated
    try:
        TRANSLATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TRANSLATIONS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(TRANSLATIONS_PATH)
    except OSError:
        pass


def looks_english(title: str) -> bool:
    low = f" {str(title or '').lower()} "
    english = sum(token in low for token in
                  (" the ", " and ", " for ", " with ", " to ", " of ", " on ",
                   " introducing ", " how ", " our ", " we ", " from "))
    french = sum(token in low for token in
                 (" le ", " la ", " les ", " et ", " pour ", " avec ", " une ",
                  " des ", " du ", " au "))
    return english > french


def translate_title(title: str, timeout: float = 8.0) -> str:
    """Rend le titre **dicible** : traduit s'il le faut, et surtout reformule.

    Passe par le meme petit modele que le routage d'intentions : il est deja
    joignable, il repond en une demi-seconde, et le resultat est garde sur le
    disque — un titre ne se retravaille jamais deux fois. Hors ligne, on garde
    le titre brut : approximatif, mais toujours mieux que rien.
    """
    value = " ".join(str(title or "").split())
    if not value:
        return value
    with _translations_lock:
        cached = _load_translations().get(value)
    if cached:
        return cached

    key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not key or not net.reachable("actu:traduction"):
        return value
    try:
        response = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": TRANSLATE_MODEL,
                "messages": [{"role": "system", "content": TRANSLATE_PROMPT},
                             {"role": "user", "content": value}],
                "temperature": 0, "max_tokens": 80,
            },
            timeout=net.timeout(timeout),
        )
        response.raise_for_status()
        translated = " ".join(
            str(response.json()["choices"][0]["message"]["content"]).split())
        translated = _plain_speech(translated)
        net.note_success("actu:traduction")
    except Exception as exc:  # noqa: BLE001 - sans traduction on garde l'original
        net.note_failure("actu:traduction", exc)
        return value
    if not translated or len(translated) > 260:
        return value
    with _translations_lock:
        _remember_translation(value, translated)
    return translated


# --- ce que le briefing consomme ----------------------------------------------

_cache_lock = threading.Lock()


def _read_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(payload: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


def current(force: bool = False) -> dict:
    """L'actualite du moment, sous la forme attendue par la scene de demarrage."""
    with _cache_lock:
        cached = _read_cache()
        fresh_enough = (
            not force and cached.get("fetched_at")
            and (datetime.datetime.now(datetime.timezone.utc).timestamp()
                 - float(cached["fetched_at"])) < CACHE_TTL_S
        )
        if fresh_enough and cached.get("title"):
            return {k: v for k, v in cached.items() if k != "fetched_at"}

        item = pick(fetch_items(), cached.get("source_title", ""))
        if item is None:
            return {k: v for k, v in cached.items() if k != "fetched_at"}

        payload = {
            "title": translate_title(item.title),
            "source_title": item.title,
            "source": item.source,
            "url": item.url,
            "published": item.published.astimezone().strftime("%d/%m/%Y")
            if item.published else "",
            "freshness": freshness_phrase(item.published),
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).timestamp(),
        }
        _write_cache(payload)
        return {k: v for k, v in payload.items() if k != "fetched_at"}


def sentence(item: dict | None = None) -> str:
    """La phrase du briefing, faite pour etre *dite*.

    On annonce d'abord d'ou et de quand ca vient, puis le titre comme une phrase
    a part entiere. Coller le titre derriere un deux-points donnait des enfilades
    du genre « … Hugging Face : TutorMoments : les tuteurs… » — deux deux-points
    dans une seule respiration — et un « ?. » quand le titre etait une question.
    """
    data = current() if item is None else item
    title = str(data.get("title", "")).strip().rstrip(" .")
    if not title:
        return ""
    source = str(data.get("source", "")).strip()
    when = str(data.get("freshness", "")).strip()

    # ⚠️ l'ancienne version coupait apres l'introduction : « Côté intelligence
    # artificielle, avant-hier chez OpenAI. » puis le titre. Le premier morceau
    # n'a pas de verbe, donc la voix marquait un point sur une phrase inachevee
    # — a l'oreille, on croyait qu'elle s'etait interrompue. Tout tient
    # maintenant dans **une seule** phrase, ou la fraicheur et la source sont
    # des complements, pas des annonces.
    lead = "Côté intelligence artificielle"
    if when:
        lead += f", {when}"
    # repeter la source quand elle est deja dans le titre sonne faux :
    # « Mistral AI presente Shieldstral, selon Mistral AI ».
    if source and source.lower() not in title.lower():
        # toujours « chez X » : sans la preposition, « avant-hier, Hugging Face,
        # TutorMoments se demande… » enfile trois groupes separes par des
        # virgules et on ne sait plus lequel est la source.
        lead += f", chez {source}"
    body = _as_clause(title)
    if not title.endswith(("?", "!")):
        body += "."
    # ⚠️ surtout pas de deux-points ici : les titres en portent souvent un
    # (« TutorMoments : les tuteurs IA »), et deux dans une respiration, ca
    # s'entend. Une virgule enchaine sans marquer d'arret.
    return f"{lead}, {body}"


# les mots qui, en tete de titre, ne sont qu'une majuscule de debut de phrase.
# tout le reste est presume nom propre — « OpenAI », « Mistral AI », « GPT » —
# et **ne doit pas** etre mis en minuscule : c'est le nom de l'entreprise dont
# on parle, et le test qui comptait les repetitions de source l'a montre.
_SENTENCE_STARTERS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "ce", "cet", "cette",
    "ces", "il", "elle", "on", "ils", "elles", "cela", "leur", "leurs", "son",
    "sa", "ses", "avec", "pour", "dans", "apres", "après", "selon", "deux",
    "trois", "nouveau", "nouvelle", "nouveaux", "nouvelles",
}


def _as_clause(title: str) -> str:
    """Enchaine le titre apres une virgule, sans casser les noms propres."""
    first = title.split(" ", 1)[0].strip(",;:")
    if first.lower() in _SENTENCE_STARTERS:
        return title[0].lower() + title[1:]
    return title
