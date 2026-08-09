"""the ai news in the briefing: fresh, sourced, and said in french.

the old version scraped two html pages (mistral, openai) and took the first link
it could date. three faults, all of them audible first thing in the morning:

- **nothing guaranteed freshness.** on 8 august ava announced a 4 august post as
  "the news" — and with nothing new published she'd have kept saying it for
  weeks.
- **translation went through ollama**, which isn't running. english headlines
  therefore went straight into a french voice: "Continuous voice interaction
  with GPT Live" read by a french voice is just noise.
- **the sentence repeated itself**: "Mistral AI presente Shieldstral., selon
  Mistral AI."

here we read **rss feeds** (real publication dates, no scraping), refuse
anything too old, drop the customer-story posts, and translate with the same
small model the intent router uses.
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

from ava import paths
from ava import net as net

CACHE_PATH = paths.cache_dir("ai_news.json")
TRANSLATIONS_PATH = paths.cache_dir("ai_news_titles.json")

USER_AGENT = "Mozilla/5.0 Ava/1.0"
ATOM = "{http://www.w3.org/2005/Atom}"

# the labs first (they announce at home before anyone else), then two context
# sources. all checked: these are real rss/atom feeds.
FEEDS = (
    ("Mistral AI", "https://mistral.ai/rss.xml"),
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google", "https://blog.google/technology/ai/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
)

# past this it isn't news any more: better to say so than to pass last week's
# announcement off as today's story.
MAX_AGE_DAYS = 6
FRESH_HOURS = 48          # en dessous, on considere que c'est vraiment chaud

# the cache is only there to avoid hitting six feeds on every wake-up; it has
# to expire fast, or we're back to the problem we're trying to fix.
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
    # rss says <item>, atom says <entry>: take both without arguing.
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
    """Every feed, flattened, newest first.

    The six feeds go out **together**: read one after another, a single slow
    server made everyone behind it wait, and the worst case hit six times the
    timeout — for one sentence of the briefing.
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


# the labs also publish a lot of marketing: customer stories, partnerships, hr
# posts. not what you want to hear first thing, however fresh it is.
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
    """The higher this is, the more it deserves to be the story of the day."""
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
    """The best recent story, avoiding the one already told.

    We sort by relevance *inside* the freshness window, never the other way
    round: a major announcement from five days ago must not jump ahead of a
    genuine piece of news from yesterday.
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
    """"ce matin", "hier"… — without it, a tuesday post sounds like today's."""
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


# --- turning headlines into sentences -----------------------------------------

_translations_lock = threading.Lock()
_translations: dict[str, str] | None = None

TRANSLATE_MODEL = os.getenv("AVA_NLU_MODEL", "ministral-8b-latest").strip()
BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()

# ⚠️ a headline is not a sentence. "Réponse aux défis cyber stratégiques futurs
# avec les capacités avancées" is a noun phrase with no verb: it reads fine with
# your eyes and cannot be said out loud. Spoken, you expect a subject and a verb
# — hence a rewrite, not just a translation.
TRANSLATE_PROMPT = (
    "Réécris ce titre d'actualité en UNE phrase française, dite à l'oral, "
    "avec un sujet et un verbe conjugué, 20 mots maximum. "
    "Commence par l'entreprise ou le produit concerné quand c'est possible. "
    "N'ajoute, ne retire et n'invente aucune information. Garde les noms de "
    "produits et d'entreprises tels quels, et écris les accents. "
    "Réponds uniquement par la phrase, sans guillemets et sans point final."
)


# ⚠️ the model answers in markdown the moment it wants to stress something:
# "savent *quand* intervenir". On screen that's fine; out loud, the synthesiser
# says "asterisk". We strip every emphasis mark before keeping the sentence —
# this is text meant to be **spoken**, never shown on its own.
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
    """Make the headline **sayable**: translate if needed, and above all rewrite.

    Goes through the same small model as the intent router: it's already
    reachable, it answers in half a second, and the result is kept on disk — a
    headline is never reworked twice. Offline we keep the raw title: rough, but
    still better than nothing.
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


# --- what the briefing consumes -----------------------------------------------

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
    """The current story, in the shape the startup scene expects."""
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
    """The briefing sentence, built to be *spoken*.

    Where and when it comes from first, then the headline as a sentence in its
    own right. Sticking the headline behind a colon produced strings like
    "… Hugging Face : TutorMoments : les tuteurs…" — two colons in one breath —
    and a "?." whenever the headline was a question.
    """
    data = current() if item is None else item
    title = str(data.get("title", "")).strip().rstrip(" .")
    if not title:
        return ""
    source = str(data.get("source", "")).strip()
    when = str(data.get("freshness", "")).strip()

    # ⚠️ the old version cut after the introduction: "Côté intelligence
    # artificielle, avant-hier chez OpenAI." and then the headline. The first
    # piece has no verb, so the voice put a full stop on an unfinished sentence
    # — it sounded like she'd been interrupted. It's all **one** sentence now,
    # where the freshness and the source are modifiers, not announcements.
    lead = "Côté intelligence artificielle"
    if when:
        lead += f", {when}"
    # repeating the source when it's already in the headline rings false:
    # "Mistral AI presente Shieldstral, selon Mistral AI".
    if source and source.lower() not in title.lower():
        # always "chez X": without the preposition, "avant-hier, Hugging Face,
        # TutorMoments se demande…" strings three comma-separated groups
        # together and you lose track of which one is the source.
        lead += f", chez {source}"
    body = _as_clause(title)
    if not title.endswith(("?", "!")):
        body += "."
    # ⚠️ definitely no colon here: headlines often carry one already
    # ("TutorMoments : les tuteurs IA"), and two in one breath is audible. A
    # comma carries on without calling a halt.
    return f"{lead}, {body}"


# words that, at the head of a headline, are only a sentence-initial capital.
# everything else is assumed to be a proper noun — "OpenAI", "Mistral AI", "GPT"
# — and **must not** be lowercased: it's the name of the company being talked
# about, as the test counting source repetitions made clear.
_SENTENCE_STARTERS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "ce", "cet", "cette",
    "ces", "il", "elle", "on", "ils", "elles", "cela", "leur", "leurs", "son",
    "sa", "ses", "avec", "pour", "dans", "apres", "après", "selon", "deux",
    "trois", "nouveau", "nouvelle", "nouveaux", "nouvelles",
}


def _as_clause(title: str) -> str:
    """Carry the headline on after a comma, without breaking proper nouns."""
    first = title.split(" ", 1)[0].strip(",;:")
    if first.lower() in _SENTENCE_STARTERS:
        return title[0].lower() + title[1:]
    return title
