#!/usr/bin/env python3
# ava - a voice assistant for the mac: "bonjour ava" or a double clap wakes the
# desk up, "ok ava" runs a command.
# we listen to the mic, spot a double clap, and set off:
#   1. the music on spotify
#   2. the welcome line (cached so it starts instantly)
#   3. the apps laid out in four quadrants
#
# start:  .venv/bin/ava
# stop:   ctrl+c

from __future__ import annotations

import datetime
import json
import os
import queue
import re
import subprocess
import unicodedata
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import requests
import sounddevice as sd

from ava import paths
from ava.state import AssistantStateMachine, AvaState, InvalidTransition
from ava.mac.app_catalog import AppCatalog
from ava.config import STORE as CONFIG, clap_min_rms
from ava.services.calendar_tools import MacCalendar
from ava.mac.computer_use import ComputerUseEngine, parse_computer_intent
from ava.brain.conversation import LocalConversationEngine
from ava.services import google_calendar as google_calendar
from ava.instance_lock import SingleInstanceLock
from ava import net as net
from ava.mac import promethee as promethee
from ava.services import ai_news as ai_news
from ava.services import quotes as quotes
from ava.mac.screen_agent import ScreenAgent
from ava.mac.screen_vision import ScreenVision
from ava.services import mailbox as mailbox
from ava.services.obsidian import ObsidianMemory
from ava.brain import skills as skills
from ava import traces as traces
from ava.brain.understanding import ROUTER as INTENT_ROUTER
from ava.audio import voice_tts as voice_tts
from ava.audio.wake_words import (PartialWakeGate, extract_wake, normalize_speech,
                        strip_wake_prefix, strip_wake_suffix)
from ava.services.web_research import ResearchReply, WebResearch

load_dotenv(paths.ENV_FILE)
SETTINGS = CONFIG.snapshot()

# --- settings ---------------------------------------------------------------

# mic: macbooks sample at 48000 hz, not 44100.
# the wrong sample rate is the number one cause of missed claps.
SAMPLE_RATE = 48000
BLOCK_MS = 10                      # taille d'un bloc d'analyse (ms)

# clap detection by the SHAPE of the sound, not by volume alone.
# a voice reaches the same level as a clap (clap 0.42-0.66, voice ~0.54), so you
# can't tell them apart on volume. but a clap collapses in under 100 ms while a
# voice stays loud. so when a peak crosses the threshold we wait, then check the
# level really did fall back.
MIN_RMS = CONFIG.clap_min_rms()    # derive du curseur de sensibilite (0..100)
DECAY_MS = 100                     # delai max pour que le son "s'effondre"
DECAY_RATIO = 0.35                 # doit retomber sous 35% du pic => c'est un clap
MAX_EVENT_MS = 300                 # si ca reste fort plus longtemps => voix/musique

# double clap: the gap between the two claps
MIN_GAP_S = SETTINGS["wake"]["clap"]["min_gap_ms"] / 1000
MAX_GAP_S = SETTINGS["wake"]["clap"]["max_gap_ms"] / 1000
CLAP_ENABLED = SETTINGS["wake"]["clap"]["enabled"]
REFRACTORY_S = 0.10                # petit temps mort apres un clap (echo)
# afplay takes a moment to put out its first sample: we shift the transcript by
# the same amount, or the text speaks before the voice does.
AFPLAY_LEAD_S = 0.22
CLAP_PEAK_RATIO = 2.5              # les deux coups d'une paire doivent se ressembler
FLOW_COOLDOWN_S = 8.0              # apres un declenchement complet, on se tait

# debug mode: print the measured level and the threshold on every peak
DEBUG = os.getenv("AVA_DEBUG", os.getenv("JARVIS_DEBUG", "")).strip().lower() in (
    "1", "true", "yes", "on")

# spotify: a URI forces an album or playlist; empty means shuffle inside the last
# context used (playlist, liked songs, whatever).
SPOTIFY_URI = SETTINGS["morning"]["spotify_uri"]

# apps to open and their quadrant: "tl" top-left, "tr" top-right,
# "bl" bottom-left, "br" bottom-right.
APPS = [(item["name"], item["position"], item.get("url", ""))
        for item in SETTINGS["morning"]["apps"]]

# the morning briefing is built on the fly (weather + ai news + a quote), so the
# text changes every day. the city for the weather:
CITY = SETTINGS["identity"]["city"]
USER_NAME = SETTINGS["identity"]["name"]
WAKE_PHRASES = tuple(SETTINGS["wake"]["phrases"])
SYSTEM_VOICE = SETTINGS["voice"]["system_fallback"]
COMPUTER_USE_ENABLED = SETTINGS["computer_use"]["enabled"]
SKILLS_ENABLED = SETTINGS["skills"]["enabled"]
CONTINUOUS_LISTENING = SETTINGS["conversation"]["continuous_listening"]
FOLLOWUP_TIMEOUT_S = SETTINGS["conversation"]["followup_timeout_seconds"]
MAX_CONTINUOUS_TURNS = SETTINGS["conversation"]["max_continuous_turns"]

# the quote collection lives in `services/quotes.py`: they're attributed, and the
# rotation avoids whatever was just said.
# accents are mandatory: this is read by a speech synthesiser, and "espere" is
# not pronounced anything like "espère".

def daily_quote() -> str:
    """The quote said in the briefing, author included."""
    return quotes.of_the_day().spoken()


def startup_quote() -> str:
    """The quote shown on the startup scene."""
    return quotes.of_the_day().spoken()


# --- weather (open-meteo, no key) -------------------------------------------

WMO = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine dense",
    56: "bruine verglaçante", 57: "bruine verglaçante",
    61: "pluie légère", 63: "pluie", 65: "forte pluie",
    66: "pluie verglaçante", 67: "pluie verglaçante",
    71: "neige légère", 73: "neige", 75: "forte neige", 77: "grains de neige",
    80: "averses légères", 81: "averses", 82: "fortes averses",
    85: "averses de neige", 86: "fortes averses de neige",
    95: "orage", 96: "orage avec grêle", 99: "orage avec grêle",
}

_geo_cache: dict = {}


def _geocode(city: str):
    if city in _geo_cache:
        return _geo_cache[city]
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": city, "count": 1, "language": "fr"},
                     timeout=net.timeout(10))
    res = r.json().get("results")
    if not res:
        return None
    latlon = (res[0]["latitude"], res[0]["longitude"])
    _geo_cache[city] = latlon
    return latlon


# one weather family (glyph) per wmo code, for the scene illustration.
def weather_glyph(code: int) -> str:
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    return "partly"


_weather_cache: dict = {"ts": 0.0, "city": None, "info": None}
_weather_lock = threading.Lock()


def weather_info() -> dict | None:
    # raw weather (for the voice AND the visual scene), cached for 20 min so we
    # don't call the api again on every refresh of the scene.
    with _weather_lock:
        age = time.time() - _weather_cache["ts"]
        usable = (_weather_cache["info"] is not None
                  and _weather_cache["city"] == CITY)
        if usable and age < 1200:
            return dict(_weather_cache["info"])
        # offline, two-hour-old weather beats an amputated briefing: the
        # temperature moves a couple of degrees, the season doesn't change.
        if usable and age < 10800 and not net.reachable("meteo"):
            return dict(_weather_cache["info"])
    if not net.reachable("meteo"):
        return None
    try:
        latlon = _geocode(CITY)
        if not latlon:
            return None
        lat, lon = latlon
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 1}, timeout=net.timeout(10)).json()
        code = int(w["current"]["weather_code"])
        info = {
            "city": CITY,
            "temp": round(w["current"]["temperature_2m"]),
            "code": code,
            "desc": WMO.get(code, "temps variable"),
            "tmax": round(w["daily"]["temperature_2m_max"][0]),
            "tmin": round(w["daily"]["temperature_2m_min"][0]),
            "glyph": weather_glyph(code),
        }
        with _weather_lock:
            _weather_cache.update(ts=time.time(), city=CITY, info=info)
        net.note_success("meteo")
        return dict(info)
    except Exception as exc:  # noqa: BLE001
        net.note_failure("meteo", exc)
        if DEBUG:
            print(f"[meteo] indisponible : {exc}")
        return None


def weather_sentence() -> str:
    """The weather in one sentence, without saying the same number twice.

    The old version said "il fait actuellement 33 degres […] aujourd'hui jusqu'a
    33 degres": when the current temperature is already the day's high,
    announcing the high teaches you nothing and makes ava sound like she's
    padding.
    """
    info = weather_info()
    if not info:
        return ""
    temp, tmax, tmin = info["temp"], info["tmax"], info["tmin"]
    opening = f"Côté ciel, {temp} degrés à {info['city']}, {info['desc']}"
    if temp >= tmax:
        return f"{opening}. On ne montera pas plus haut, et {tmin} degrés au plus bas."
    return f"{opening}. On ira jusqu'à {tmax} degrés, avec {tmin} au plus bas."


# --- ai news (rss feeds, no key) --------------------------------------------

# the news is frozen for the whole session: the morning briefing and the startup
# scene have to tell the same story. `ai_news` handles expiry on disk (3 h).
_AI_NEWS_LOCK = threading.Lock()
_AI_NEWS_RUNTIME: dict = {}


def ai_news_item() -> dict:
    """The current ai story. The detail lives in `ai_news` (dated rss feeds)."""
    global _AI_NEWS_RUNTIME
    with _AI_NEWS_LOCK:
        if _AI_NEWS_RUNTIME:
            return dict(_AI_NEWS_RUNTIME)
        try:
            _AI_NEWS_RUNTIME = ai_news.current()
        except Exception as exc:  # noqa: BLE001 - un briefing sans actu vaut mieux que pas de briefing
            if DEBUG:
                print(f"[actu ia] indisponible : {exc}")
            return {}
        return dict(_AI_NEWS_RUNTIME)


def localized_ai_title(title: str, source: str = "") -> str:
    """A headline a french voice can actually pronounce.

    Translation used to go through Ollama, which isn't running, so english
    headlines arrived intact at a french synthesiser. `ai_news` handles it now
    with the same small model as the intent router, and keeps the result on
    disk.
    """
    return ai_news.translate_title(title)


def ai_news_sentence() -> str:
    item = ai_news_item()
    if not item:
        # better to skip the section than to pad: a story that isn't there
        # doesn't deserve airtime in the briefing.
        return ""
    return ai_news.sentence(item)


# --- today's calendar (calendar.app, so google calendar if it syncs) --------

CALENDAR = MacCalendar()
GOOGLE_CALENDAR = google_calendar.CALENDAR

WEEKDAYS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS_FR = ("", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre")


def spoken_date(day: datetime.date | None = None) -> str:
    """\"samedi 8 août\", the way you'd say it out loud."""
    day = day or datetime.date.today()
    number = "premier" if day.day == 1 else str(day.day)
    return f"{WEEKDAYS_FR[day.weekday()]} {number} {MONTHS_FR[day.month]}"


def spoken_title(title: str) -> str:
    """Make a calendar title sayable.

    Calendar titles are full of emoji ("💻 Exo code #1"), and the synthesiser
    either pronounces them or chokes on them. We also drop the hash, which comes
    out as "croisillon" instead of "numero".
    """
    value = str(title or "").strip()
    # emoji, pictographs, flags, variation selectors and joiners
    value = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
        "\U00002190-\U000021FF\U00002B00-\U00002BFF️‍•]",
        " ", value)
    value = re.sub(r"#\s*(\d+)", r"numéro \1", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -–—:;,")
    return value or "Sans titre"


def _spoken_hour(moment: datetime.datetime) -> str:
    # "14h30" reads badly: "14 heures 30" is better, and "midi pile" stays "12 heures".
    if moment.minute == 0:
        return f"{moment.hour} heures"
    return f"{moment.hour} heures {moment.minute:02d}"


def agenda_events(day_offset: int = 0):
    """A day's events, whatever the source.

    Google Calendar wins as soon as it's connected (it's the one that's actually
    kept up to date); Calendar.app stays the offline fallback. Both return
    objects with the same attributes, so nothing downstream cares.
    """
    if GOOGLE_CALENDAR.connected():
        try:
            return GOOGLE_CALENDAR.events_for_day(day_offset), "google"
        except Exception as exc:  # noqa: BLE001 - on retombe sur Calendar.app
            if DEBUG:
                print(f"[agenda] google indisponible : {exc}")
    return CALENDAR.events_for_day(day_offset), "calendar"


def calendar_sentence(limit: int = 3) -> str:
    """Today's calendar, at the top of the briefing. Silent if the calendar can't
    be read: a morning briefing is no time to bring up a permission dialog.

    We say **the count first**, then the next three events, then the rest as a
    number. Listing all ten events of a busy day pushed the briefing past a
    minute, and nobody remembers the tenth; the count tells you what the day
    looks like straight away. Events already past are never mentioned — at 2 pm,
    this morning's teach you nothing.
    """
    try:
        events, _source = agenda_events(0)
    except Exception:  # noqa: BLE001 - agenda indisponible, on n'en parle pas
        return ""
    now = datetime.datetime.now()
    # what has already happened is of no interest at 9 in the morning.
    upcoming = [event for event in events if event.all_day or event.end >= now]
    if not events:
        return "Ton agenda est vide aujourd'hui, la journée t'appartient."
    if not upcoming:
        return "Tout ton agenda du jour est déjà passé."

    details = []
    for event in upcoming[:max(1, limit)]:
        if event.all_day:
            details.append(f"toute la journée, {spoken_title(event.title)}")
        else:
            details.append(f"à {_spoken_hour(event.start)}, {spoken_title(event.title)}")
    # each event gets its own sentence: strung together with semicolons they came
    # out in one breath, with no pause to separate them by ear.
    body = ". ".join(detail[0].upper() + detail[1:] for detail in details) + "."
    if len(upcoming) == 1:
        return "Tu as un seul rendez-vous aujourd'hui, " + details[0] + "."
    rest = len(upcoming) - len(details)
    head = f"Tu as {len(upcoming)} rendez-vous aujourd'hui. "
    tail = (" Et un autre ensuite." if rest == 1
            else f" Et {rest} autres ensuite." if rest else "")
    return head + body + tail


def build_startup_payload(*, fetch_news: bool = True, briefing: str | None = None) -> dict:
    now = datetime.datetime.now()
    weekdays = ("LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI", "SAMEDI", "DIMANCHE")
    months = ("", "JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOÛT", "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE")
    news = ai_news_item() if fetch_news else {}
    # weather (illustration) and the written briefing ("what ava says") only on
    # the enriched pass: the first pass stays instant (state "loading").
    weather = weather_info() if fetch_news else None
    # `briefing` can be forced by the caller: the wake-up already built that text
    # AND the matching audio. Recomputing it here produced a transcript different
    # from what Ava says (the calendar and the time moved in between), and redid
    # the calendar and Promethee lookups for nothing.
    if briefing is None:
        briefing = build_welcome_text() if fetch_news else ""
    # one draw only: showing one quote while saying another would make it look
    # like the scene and the voice aren't talking about the same day.
    scene_quote = quotes.of_the_day() if fetch_news else quotes.Quote("", "")
    return {
        "name": USER_NAME,
        "city": CITY,
        "date": f"{weekdays[now.weekday()]} {now.day} {months[now.month]}",
        # `ai_news` already hands back a translated headline: putting it through
        # translation again would return it unchanged, for one network round trip.
        "news": news.get("title", "") if news else "Recherche d'une actualité IA récente…",
        "source": news.get("source", "Vérification en cours"),
        "source_url": news.get("url", ""),
        "published": news.get("published", ""),
        # "hier", "ce matin": freshness reads on screen too.
        "freshness": news.get("freshness", "") if news else "",
        # the greeting follows the clock, like the spoken briefing does: a huge
        # "BONJOUR" at 10 pm next to text saying "Bonsoir" was impossible to miss.
        "hello": greeting_word(now),
        "quote": scene_quote.text if fetch_news else "Ton prochain niveau commence par ce que tu fais maintenant.",
        "quote_author": scene_quote.author if fetch_news else "",
        "weather": weather,
        "briefing": briefing,
        "loading": not fetch_news,
        "duration": SETTINGS.get("ui", {}).get("startup_duration_seconds", 9),
    }


def greeting_word(moment: datetime.datetime | None = None) -> str:
    """« Bonjour » le matin, « bonsoir » le soir — pas « bonjour » a 20 h."""
    hour = (moment or datetime.datetime.now()).hour
    return "Bonjour" if hour < 18 else "Bonsoir"


def build_welcome_text() -> str:
    """The briefing, in the order it's useful.

    The order isn't decorative. We open with the bearings (who's talking, when),
    give **immediately** what commits the day — the calendar — then the scenery,
    and close on the action. Before, the five blocks arrived flat, in the same
    tone, glued end to end: the one genuinely useful line ("tu as un rendez-vous
    a 20 h") was buried between the weather and an anonymous quote, and it
    opened on "c'est Ava, j'espere que tu vas bien !", identical every morning.

    Each block now carries its own way in, so you hear the subject change
    without having to be told.
    """
    now = datetime.datetime.now()
    parts = [f"{greeting_word(now)} {USER_NAME}. "
             f"Il est {_spoken_hour(now)}, on est {spoken_date()}."]

    agenda = calendar_sentence()
    if agenda:
        parts.append(agenda)

    meteo = weather_sentence()
    if meteo:
        parts.append(meteo)

    actu = ai_news_sentence()
    if actu:
        parts.append(actu)

    quote = quotes.of_the_day()
    parts.append(quotes.spoken_intro(quote))

    # the workspace goes up while she talks: by the time she gets here the
    # windows are already in place. Announcing "je t'ouvre tes applications"
    # afterwards would ring false.
    if promethee.active_session():
        parts.append("Ta session Prométhée tourne déjà, ton espace est en place. "
                     "Bon travail !")
    else:
        parts.append("Je te lance une session Prométhée, ton espace est en place. "
                     "Bon travail !")
    return " ".join(parts)

CACHE_DIR = paths.cache_dir("ava_welcome")
INSTANCE_LOCK = SingleInstanceLock(paths.cache_dir("ava.lock"))

# --- the visual overlay (orb window, top right) ------------------------------
OVERLAY_ENABLED = os.getenv("AVA_OVERLAY", os.getenv("JARVIS_OVERLAY", "1")).strip().lower() not in (
    "0", "false", "no", "off")
try:
    from ava.ui import overlay as _overlay
except Exception:                       # pywebview absent ?
    _overlay = None
    OVERLAY_ENABLED = False

try:
    from ava.ui.menubar import MENU_BAR, quit_ava
except Exception:                       # hors macos : pas de barre de menus
    MENU_BAR = None
    quit_ava = None


def ui(fn: str, *args) -> None:
    # pass a state on to the overlay if it is up, otherwise do nothing
    if OVERLAY_ENABLED and _overlay is not None:
        try:
            getattr(_overlay, fn)(*args)
        except Exception:
            pass


def _render_assistant_state(snapshot) -> None:
    # the functional state and the visible state never drift apart.
    if snapshot.state == AvaState.DORMANT:
        ui("dormant")
    elif snapshot.state == AvaState.IDLE:
        ui("idle")
    elif snapshot.state != AvaState.BOOTING:
        ui("set_state", snapshot.state.value, snapshot.label or None)
    # the menu bar icon is the only visual feedback left once the panel is
    # closed, so it has to follow the same state.
    if MENU_BAR is not None:
        try:
            MENU_BAR.set_state(snapshot.state.value)
        except Exception:  # noqa: BLE001
            pass


ASSISTANT_STATE = AssistantStateMachine(on_change=_render_assistant_state)


def set_assistant_state(state: AvaState | str, label: str = "", *, force=False) -> None:
    try:
        ASSISTANT_STATE.transition(state, label, force=force)
    except InvalidTransition:
        # a stream that ends must always be able to put ava back to sleep.
        if state == AvaState.DORMANT or state == AvaState.DORMANT.value:
            ASSISTANT_STATE.dormant()
        elif DEBUG:
            print(f"[etat] transition ignoree : {ASSISTANT_STATE.snapshot.state} -> {state}")


def return_to_idle() -> None:
    """Go back to the panel, or hide it if that's what was asked for."""
    if SETTINGS.get("ui", {}).get("start_hidden", False):
        ASSISTANT_STATE.dormant()
    else:
        ASSISTANT_STATE.idle()


# --- what the wake-up does --------------------------------------------------

def _osascript(script: str) -> None:
    # small helper: run a bit of applescript without dying if it fails
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[applescript] souci : {exc}")


def play_spotify() -> None:
    print("  -> spotify")
    if SPOTIFY_URI:
        # An explicit target wins. The configuration expects a Spotify URI
        # (spotify:playlist:...), not a web link.
        _osascript(f'tell application "Spotify" to play track "{SPOTIFY_URI}"')
        return
    # With no target, Spotify picks its last listening context back up. We turn
    # shuffle on and skip ahead so the ritual gets a different track.
    _osascript('''
    tell application "Spotify"
        activate
        play
        set shuffling to true
        next track
        play
    end tell
    ''')


def screen_bounds() -> tuple[int, int, int, int]:
    # the screen size in "points" (not retina pixels)
    out = subprocess.run(
        ["osascript", "-e",
         'tell application "Finder" to get bounds of window of desktop'],
        capture_output=True, text=True, timeout=10,
    )
    try:
        x1, y1, x2, y2 = [int(v.strip()) for v in out.stdout.split(",")]
        return x1, y1, x2, y2
    except Exception:
        return 0, 0, 1440, 900  # secours


def quadrant_rect(where: str, sb: tuple[int, int, int, int]):
    x1, y1, x2, y2 = sb
    menubar = 25                       # on evite la barre de menu du haut
    top = y1 + menubar
    w = (x2 - x1) // 2
    h = (y2 - top) // 2
    left = x1
    right = x1 + w
    mid = top + h
    # a small window tucked bottom right, with a margin: enough to see what's
    # playing without eating a quarter of the screen.
    small_w, small_h, margin = 380, 220, 24
    return {
        "tl": (left,  top, w, h),
        "tr": (right, top, w, h),
        "bl": (left,  mid, w, h),
        "br": (right, mid, w, h),
        "small": (x2 - small_w - margin, y2 - small_h - margin, small_w, small_h),
    }[where]


def open_and_place(app: str, where: str, sb, url: str = "") -> None:
    # open the app, then put it in its corner (through system events).
    # macos may ask for the "accessibility" permission the first time.
    print(f"  -> {app} ({where}){' ' + url if url else ''}")
    launch_name = app
    resolved = APP_CATALOG.resolve(app) if "APP_CATALOG" in globals() else None
    if url:
        # `open -a <app> <url>` starts the app *on* the page we want, in one go.
        target = resolved[1] if resolved else app
        flag = "-a" if not resolved else "-a"
        subprocess.run(["open", flag, target, url], check=False)
        if resolved:
            launch_name = resolved[0]
    elif resolved:
        launch_name, app_path = resolved
        subprocess.run(["open", app_path], check=False)
    else:
        subprocess.run(["open", "-a", app], check=False)
    x, y, w, h = quadrant_rect(where, sb)
    # ⚠️ waiting for the *process* isn't enough: it exists before it has painted
    # a window, and a browser opened on an address makes a new one afterwards. So
    # we were applying the geometry to a window that wasn't there yet — Dia ended
    # up spread over half the screen instead of sitting in its quadrant. We wait
    # for the window, then **check** the size actually took, and start again if
    # it didn't.
    script = f'''
    tell application "System Events"
        set tries to 0
        repeat until (exists (process "{launch_name}")) or tries > 25
            delay 0.2
            set tries to tries + 1
        end repeat
        try
            tell process "{launch_name}"
                set waited to 0
                repeat until (count of windows) > 0 or waited > 30
                    delay 0.2
                    set waited to waited + 1
                end repeat
                set frontmost to true
                repeat 4 times
                    delay 0.35
                    try
                        set position of window 1 to {{{x}, {y}}}
                        set size of window 1 to {{{w}, {h}}}
                        set got to size of window 1
                        if (item 1 of got) - {w} < 40 and {w} - (item 1 of got) < 40 then exit repeat
                    end try
                end repeat
            end tell
        end try
    end tell
    '''
    _osascript(script)


def open_startup_apps() -> None:
    """Ouvre uniquement la liste personnelle configuree, sans musique ni briefing."""
    bounds = screen_bounds()
    for app, where, url in APPS:
        open_and_place(app, where, bounds, url)


def start_workspace() -> threading.Thread:
    """Bring the workspace up **while** Ava talks.

    The windows used to open once the briefing was over: the briefing played out
    in front of an empty desktop, then, in silence, the applications appeared.
    You were watching two unrelated scenes. Run together, you see what she
    announces happening as she says it.
    """
    job = threading.Thread(target=open_startup_apps, daemon=True,
                           name="ava-espace-de-travail")
    job.start()
    return job


def close_startup_apps() -> list[str]:
    """Close what the morning ritual opened. Returns the names closed.

    The counterpart was missing: ava lays four applications out in quadrants at
    wake-up, and "ferme tout" went off to a web search. We only close the
    configured list — never everything running, there's work in the others.
    """
    closed: list[str] = []
    for app, _where, _url in APPS:
        _osascript(f'tell application "{app}" to quit')
        closed.append(app)
    return closed


def ensure_welcome_audio(text: str, mood: str = "") -> Path | None:
    """A sentence's audio, cached. None means falling through to `say`.

    Choosing the engine (mistral, local chatterbox, elevenlabs, system voice)
    lives in voice_tts: ava no longer knows who's speaking, only where the file
    is. `mood` colours the timbre when the engine can do that.
    """
    return voice_tts.synthesize(text, mood)


# the briefing is built ahead of time so it can start the moment you clap.
# we refresh it in the morning (the day changed) or once it's over 6 h old, not
# on every clap -> instant voice, and no quota burned.
_welcome: dict = {"day": None, "ts": 0.0, "text": "", "path": None}
_welcome_lock = threading.Lock()
REFRESH_AFTER_S = 6 * 3600


def get_welcome(force: bool = False):
    with _welcome_lock:
        today = datetime.date.today().isoformat()
        fresh = (_welcome["path"] is not None
                 and _welcome["day"] == today
                 and (time.time() - _welcome["ts"]) < REFRESH_AFTER_S)
        if fresh and not force:
            return _welcome["text"], _welcome["path"]
        text = build_welcome_text()
        path = ensure_welcome_audio(text)
        _welcome.update(day=today, ts=time.time(), text=text, path=path)
        return text, path


# shared guard: the clap AND the "ok ava" wake word both come through here, so
# two wake-ups can't fire back to back. _flow_active blocks any new trigger while
# a wake-up is running (otherwise ava, who says her own name during the briefing,
# would keep re-triggering herself through the mic).
_last_trigger = {"ts": 0.0}
_trigger_lock = threading.Lock()
_flow_active = threading.Event()


# listening paused: the mic stays open (the watchdog keeps an eye on the stream,
# and reopening coreaudio is expensive) but nothing can wake ava any more. this
# is the answer to "ava is in the way": one click in the menu bar and she goes
# quiet, without anyone having to kill her.
#
# AVA_PAUSED=1: ava starts without listening (handy while tuning her, or to work
# next to her without waking her up).
if os.getenv("AVA_PAUSED", "").strip().lower() in ("1", "true", "yes", "on"):
    _listening_paused.set()


def listening_paused() -> bool:
    return _listening_paused.is_set()


def set_listening_paused(paused: bool) -> bool:
    if paused:
        _listening_paused.set()
    else:
        _listening_paused.clear()
        _drain_wake_queue()     # on jette ce qui a pu s'accumuler pendant la pause
    if MENU_BAR is not None:
        MENU_BAR.set_paused(paused)
    print(f"[ava] écoute {'en pause' if paused else 'reprise'}")
    return paused


def toggle_listening_paused() -> bool:
    return set_listening_paused(not _listening_paused.is_set())


def _drain_wake_queue() -> None:
    # throw away the audio piled up during the wake-up (the mic heard ava talking
    # through the speakers) so she doesn't re-trigger right after.
    while True:
        try:
            _wake_q.get_nowait()
        except queue.Empty:
            break


def _audio_ms(path) -> int:
    # an audio file's duration via afinfo (macos), to pin the transcript to the voice.
    try:
        out = subprocess.run(["afinfo", str(path)], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"estimated duration:\s*([\d.]+)\s*sec", out)
        if m:
            return int(float(m.group(1)) * 1000)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _spoken_ms(text: str) -> int:
    # an estimate when there is no file (the "say" voice): ~165 words per minute.
    words = len(str(text or "").split())
    return max(3200, round(words / 165 * 60 * 1000))


def start_promethee_session() -> None:
    # the big wake-up opens the working day too: a focus session in promethee,
    # with nobody having to click.
    try:
        reply = promethee.start_session()
    except Exception as exc:  # noqa: BLE001 - jamais bloquer le reveil pour ca
        print(f"  -> promethee : {exc}")
        return
    print(f"  -> promethee : {reply.text}")


WELCOME_WARM_INTERVAL_S = 900


def keep_welcome_warm(interval_s: int = WELCOME_WARM_INTERVAL_S) -> None:
    """Keep today's briefing ready to go, text AND audio.

    Without this, the first "bonjour ava" after the date rolls over or the
    calendar changes lands on an empty cache: the startup scene freezes for
    about a minute while the local voice synthesises 45 s of speech. Here we
    rebuild the text regularly; if nothing moved, the audio is already cached
    and it costs nothing.
    """
    while True:
        time.sleep(interval_s)
        if _flow_active.is_set():
            continue          # ava parle : on ne lui prend pas le gpu
        try:
            get_welcome(force=True)
        except Exception as exc:  # noqa: BLE001 - un briefing rate n'arrete rien
            if DEBUG:
                print(f"[briefing] rafraichissement impossible : {exc}")


def run_welcome_flow() -> None:
    try:
        set_assistant_state(AvaState.BOOTING, "Preparation de ton espace", force=True)
        print("*** reveil du bureau ***")
        # The ritual reuses the big launch scene. It stays centred until the
        # briefing is over and the applications are up.
        startup_payload = build_startup_payload(fetch_news=False)
        startup_payload["auto_finish"] = False
        ui("startup", startup_payload)
        play_spotify()
        # Promethee takes a few seconds to open and paint its button, so we set
        # it off alongside the briefing and the session is already running by the
        # time ava stops talking.
        promethee_job = threading.Thread(target=start_promethee_session, daemon=True)
        promethee_job.start()
        start_workspace()

        # Keep the scene up while the briefing is prepared; on a first launch
        # the network or ElevenLabs can take a few seconds.
        text, path = get_welcome()
        _remember_safely("morning_briefing", text)
        startup_payload = build_startup_payload(fetch_news=True, briefing=text)
        startup_payload["auto_finish"] = False
        ui("startup", startup_payload)
        if path:
            print("  -> voix")
            set_assistant_state(AvaState.SPEAKING, "Ton briefing du matin")
            # transcript pinned to the real duration of the voice (karaoke): we
            # start afplay BEFORE the text, then give the player time to get
            # going. Before, the text went first and ran half a second ahead of
            # the voice for the whole briefing.
            total = _audio_ms(path) or _spoken_ms(text)
            delays = voice_tts.word_delays(path, text, total)
            player = subprocess.Popen(["afplay", str(path)])
            time.sleep(AFPLAY_LEAD_S)
            ui("startup_brief", text, total, delays)
            player.wait()
        elif text:
            set_assistant_state(AvaState.SPEAKING, "Ton briefing du matin")
            ui("startup_brief", text, _spoken_ms(text))
            subprocess.run(["say", "-v", SYSTEM_VOICE, text], check=False)

        print("*** pret. ***\n")
    finally:
        ui("finish_startup")
        _drain_wake_queue()
        # the cooldown only starts once the wake-up is over
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        return_to_idle()


# the big ritual only makes sense once: it starts the music, lays four
# applications out on screen and talks for 45 seconds. replaying it because
# somebody said "bonjour ava" at 6 pm — or because a noise passed for a double
# clap — is exactly what made ava unbearable during the day.
_ritual = {"day": None}


def ritual_done_today(day: str | None = None) -> bool:
    return _ritual["day"] == (day or datetime.date.today().isoformat())


def mark_ritual_done(day: str | None = None) -> None:
    _ritual["day"] = day or datetime.date.today().isoformat()


def run_short_greeting() -> None:
    """The later hello: ava greets you and listens, without replaying the ritual."""
    try:
        hour = datetime.datetime.now().hour
        moment = "Bonjour" if hour < 18 else "Bonsoir"
        speak(f"{moment} {USER_NAME}. Je t'écoute.")
    finally:
        _drain_wake_queue()
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        return_to_idle()


def trigger_welcome(source: str) -> None:
    with _trigger_lock:
        now = time.time()
        if _flow_active.is_set() or now - _last_trigger["ts"] < FLOW_COOLDOWN_S:
            return
        _last_trigger["ts"] = now
        # claim the flow BEFORE starting the thread: without that, the wake word
        # can win the race and start a second interaction alongside.
        _flow_active.set()
        already = ritual_done_today()
        if not already:
            mark_ritual_done()
    print(f"\n[declencheur : {source}{' (deja fait aujourd hui)' if already else ''}]")
    try:
        job = run_short_greeting if already else run_welcome_flow
        threading.Thread(target=job, daemon=True).start()
    except Exception:
        _flow_active.clear()
        raise


# --- the "ok ava" wake word (vosk, offline) ---------------------------------
# on a mac, opening two mic streams at once is unstable (coreaudio errors). so we
# keep ONE stream (the clap one, at 48000 hz) and tap its audio: resample it to
# 16000 hz for vosk and push it into this queue. the clap and "ok ava" therefore
# share the same microphone.
WAKE_MODEL_DIR = paths.MODELS_DIR / "vosk-model-small-fr-0.22"
WAKE_SR = 16000
WAKE_ENABLED = WAKE_MODEL_DIR.exists()
_wake_q: queue.Queue = queue.Queue(maxsize=800)
_command_q: queue.Queue = queue.Queue(maxsize=1800)
_capture_audio = threading.Event()


# gain applied to the voice before vosk: without it you have to shout to wake
# her. a tanh soft limiter boosts a normal or quiet voice without clipping peaks.
# tunable with AVA_WAKE_GAIN (0 or 1 disables it).
try:
    WAKE_GAIN = float(os.getenv("AVA_WAKE_GAIN", "4.0"))
except ValueError:
    WAKE_GAIN = 4.0


def resample_for_wake(mono: np.ndarray, rate: int) -> bytes:
    if rate <= 0 or mono.size == 0:
        return b""
    out_size = max(1, round(len(mono) * WAKE_SR / rate))
    if out_size == len(mono):
        downsampled = mono
    else:
        source_x = np.arange(len(mono), dtype=np.float64)
        target_x = np.linspace(0, len(mono) - 1, out_size)
        downsampled = np.interp(target_x, source_x, mono)
    if WAKE_GAIN > 1.0:
        # tanh: ~linear (x*gain) on a soft voice, saturating gently on peaks
        downsampled = np.tanh(downsampled * WAKE_GAIN)
    return (np.clip(downsampled, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _put_drop_oldest(q: queue.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def push_wake_audio(mono: np.ndarray, rate: int) -> None:
    # really resample to 16000 hz, including on the 44100 hz fallback.
    # the mic callback must never block: if vosk falls behind we drop the oldest
    # block instead of letting the ram grow.
    pcm = resample_for_wake(mono, rate)
    if not pcm:
        return
    if WAKE_ENABLED:
        _put_drop_oldest(_wake_q, pcm)
    if _capture_audio.is_set():
        _put_drop_oldest(_command_q, pcm)


# --- ava's voice (short answers) --------------------------------------------

def _illustration_for(text: str) -> str:
    """Choisit une petite illustration locale, jamais une image decorative lourde."""
    value = _norm(text)
    if any(word in value for word in ("meteo", "pluie", "nuage", "degres", "soleil")):
        return "weather"
    if any(word in value for word in ("heure", "minuteur", "chrono")):
        return "time"
    if any(word in value for word in ("musique", "spotify", "morceau", "chanson")):
        return "music"
    if any(word in value for word in ("recherche", "resultats", "internet", "youtube")):
        return "search"
    if any(word in value for word in ("ouvre", "application", "premier plan")):
        return "app"
    if any(word in value for word in ("capture", "ecran", "fenetre")):
        return "screen"
    return "spark"

# the player currently running, so it can be stopped dead. this is the core idea
# of real-time pipelines (pipecat propagates an "interruption" that flushes every
# stage's buffers) cut down to what ava needs: while we waited on `player.wait()`
# a thirty-second briefing was **indivisible**, and the only way to shut her up
# was to kill her.
_player_lock = threading.Lock()
_player: subprocess.Popen | None = None
_interrupted = threading.Event()


def stop_speaking() -> bool:
    """Coupe la parole en cours. Rend True si Ava parlait vraiment."""
    with _player_lock:
        player = _player
    if player is None or player.poll() is not None:
        return False
    _interrupted.set()
    try:
        player.terminate()
    except OSError:
        return False
    print("[ava] parole interrompue")
    return True


def speaking() -> bool:
    with _player_lock:
        return _player is not None and _player.poll() is None


_last_spoken = {"text": ""}


def speak(text: str, state: str = "speaking") -> None:
    # put the orb in the right state, show the text, then speak.
    # the voice is made locally and cached by text; if the model won't load we
    # fall through to the macos system voice, `say`.
    global _player
    _last_spoken["text"] = text     # kept for the vault journal
    set_assistant_state(state, text)
    # we measure making the voice, not playing it: the wait before ava opens her
    # mouth is the part you feel.
    with traces.span("voix", route=voice_tts.engine_name()) as trace:
        cached = voice_tts.is_cached(text)
        path = ensure_welcome_audio(text)
        # a sentence already said comes off the disk: no network, no waiting.
        trace["route"] = "cache" if cached else voice_tts.engine_name()
        trace["network"] = not cached and voice_tts.engine_name() == "mistral"
    if path:
        # same alignment as the briefing: the voice starts, then the bubble
        # fills at the pace measured on the audio.
        delays = voice_tts.word_delays(path, text, _audio_ms(path) or _spoken_ms(text))
        _interrupted.clear()
        player = subprocess.Popen([voice_tts.tool_path("afplay"), str(path)])
        with _player_lock:
            _player = player
        try:
            time.sleep(AFPLAY_LEAD_S)
            ui("message", "assistant", text, _illustration_for(text), delays)
            player.wait()
        finally:
            with _player_lock:
                _player = None
        if _interrupted.is_set():
            ui("interrupted")
    else:
        ui("message", "assistant", text, _illustration_for(text))
        subprocess.run([voice_tts.tool_path("say"), "-v", SYSTEM_VOICE, text], check=False)


def start_timer(seconds: float, text: str) -> threading.Timer:
    """A timer that doesn't hold Ava back when you quit.

    ⚠️ `threading.Timer` makes a **non-daemon** thread, so a thirty-minute timer
    kept the process from exiting for thirty minutes. Since the launch agent
    only restarts after a real exit, "quit" from the menu bar simply looked like
    it did nothing.
    """
    timer = threading.Timer(seconds, notify, args=(text,))
    timer.daemon = True
    timer.name = "ava-minuteur"
    timer.start()
    return timer


def notify(text: str) -> None:
    """parle hors interaction, sans chevaucher une conversation en cours."""
    with _trigger_lock:
        if _flow_active.is_set():
            retry = threading.Timer(2.0, notify, args=(text,))
            retry.daemon = True
            retry.start()
            return
        _flow_active.set()
    try:
        set_assistant_state(AvaState.SPEAKING, text, force=True)
        speak(text)
    finally:
        _drain_wake_queue()
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        return_to_idle()


# --- understanding and running a command ------------------------------------

def _norm(s: str) -> str:
    # lowercase and unaccented, so comparisons are easy
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


# a few spoken shortcuts -> the real app (the rest is found automatically)
_APP_ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "navigateur": "Safari", "safari": "Safari",
    "terminal": "Terminal", "spotify": "Spotify", "notion": "Notion",
    "dia": "Dia", "promethee": "Promethee", "promethe": "Promethee",
    "promethee 2": "Promethee 2",
    "code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "visual studio code": "Visual Studio Code", "cursor": "Cursor",
    "finder": "Finder", "messages": "Messages", "photos": "Photos",
    "reglages": "System Settings", "parametres": "System Settings",
    "calendrier": "Calendar", "agenda": "Calendar", "notes": "Notes",
    "rappels": "Reminders", "musique": "Music", "obsidian": "Obsidian",
    "whatsapp": "WhatsApp", "discord": "Discord",
}

APP_CATALOG = AppCatalog()


def _installed_apps() -> dict:
    return {
        name: app[0]
        for name, app in APP_CATALOG.refresh().items()
    }


def resolve_app(spoken: str):
    spoken = _norm(spoken)
    if not spoken:
        return None
    if spoken in _APP_ALIASES:
        return _APP_ALIASES[spoken]
    resolved = APP_CATALOG.resolve(spoken)
    if resolved:
        return resolved[0]
    for alias, real in _APP_ALIASES.items():
        if alias in spoken:
            return real
    return None


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)


def _open_target(target: str) -> None:
    if any(w in target for w in ("mail", "gmail", "courriel")):
        open_url("https://mail.google.com/mail/u/0/")
        speak("J'ouvre ta boite mail.")
        return
    if "youtube" in target:
        open_url("https://www.youtube.com")
        speak("J'ouvre YouTube.")
        return
    app = resolve_app(target)
    if app:
        subprocess.run(["open", "-a", app], check=False)
        speak(f"J'ouvre {app}.")
        return
    speak(f"Désolée, je ne trouve pas l'application {target}.")


# small numbers spelled out (whisper usually writes digits, but just in case)
_NUM_WORDS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
    "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20,
    "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60,
}


def _parse_number(text: str):
    m = re.search(r"\d+", text)
    if m:
        return int(m.group())
    toks = text.split()
    total = 0
    found = False
    for t in toks:
        if t in _NUM_WORDS:
            total += _NUM_WORDS[t]
            found = True
    return total if found else None


# in french the quarter hour is counted before the minute. without these three
# cases, "rappelle-moi dans un quart d'heure" understood "un" then "heure" and
# started a ONE HOUR timer.
_FRACTION_DURATIONS = (
    ("trois quarts d heure", 45 * 60, "trois quarts d'heure"),
    ("trois quarts d'heure", 45 * 60, "trois quarts d'heure"),
    ("quart d heure", 15 * 60, "un quart d'heure"),
    ("quart d'heure", 15 * 60, "un quart d'heure"),
    ("demi heure", 30 * 60, "une demi-heure"),
    ("demi-heure", 30 * 60, "une demi-heure"),
    ("une heure et demi", 90 * 60, "une heure et demie"),
)


def _parse_duration_s(text: str):
    value = str(text or "")
    for marker, seconds, label in _FRACTION_DURATIONS:
        if marker in value:
            return seconds, label
    n = _parse_number(text)
    if not n:
        return None, None
    if "heure" in text:
        return n * 3600, f"{n} heure" + ("s" if n > 1 else "")
    if "second" in text:
        return n, f"{n} seconde" + ("s" if n > 1 else "")
    return n * 60, f"{n} minute" + ("s" if n > 1 else "")   # minutes par defaut


def _volume_now() -> int:
    try:
        out = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, timeout=8)
        return int(out.stdout.strip())
    except Exception:
        return 50


def _set_volume(n: int) -> None:
    n = max(0, min(100, int(n)))
    _osascript(f"set volume output volume {n}")


def _spotify(cmd: str) -> None:
    _osascript(f'tell application "Spotify" to {cmd}')


def _flat(command: str) -> str:
    # "rendez-vous", "rendez vous", "apres-demain"…: the hyphens are an accident
    # of transcription, so we wipe them before looking for keywords.
    return re.sub(r"[-']", " ", str(command or ""))


# people almost never say the word "agenda" when asking about their calendar:
# they say "qu'est-ce que j'ai aujourd'hui", "c'est quoi mon programme demain",
# "a quelle heure est mon rendez-vous". requiring the word sent those three
# phrasings — the most common ones — off to a web search.
_AGENDA_NOUNS = ("rendez vous", "rdv", "mes reunions", "ma reunion", "planning",
                 "emploi du temps", "mon programme", "ma journee", "mes evenements")
_AGENDA_ASKS = ("qu est ce que j ai", "qu est ce qu il y a", "j ai quoi",
                "je fais quoi", "il y a quoi", "c est quoi mon", "c est quoi ma",
                "quel est mon", "quelle est ma", "dis moi ce")
_AGENDA_WHEN = ("aujourd", "demain", "ce matin", "cet apres midi", "ce soir",
                "cette semaine", "de prevu", "au programme")


def _is_calendar_summary(command: str) -> bool:
    value = _flat(command)
    if any(word in value for word in ("agenda", "calendrier", "calendar")):
        return True
    if any(noun in value for noun in _AGENDA_NOUNS):
        return True
    return (any(ask in value for ask in _AGENDA_ASKS)
            and any(when in value for when in _AGENDA_WHEN))


def _calendar_summary(command: str) -> None:
    set_assistant_state(AvaState.THINKING, "Lecture de ton agenda")
    if any(word in command for word in ("ouvre", "affiche", "montre", "lance")):
        CALENDAR.open()
    day_offset = 2 if "apres demain" in command else 1 if "demain" in command else 0
    label = ("après-demain" if day_offset == 2
             else "demain" if day_offset == 1 else "aujourd'hui")
    try:
        events, _source = agenda_events(day_offset)
    except google_calendar.NotConnected:
        speak("Je ne suis pas connectée à Google. Ouvre mes réglages pour brancher ton agenda.")
        return
    except Exception:  # noqa: BLE001
        speak("Je n'arrive pas à lire ton agenda pour le moment.")
        return
    if not events:
        speak(f"Tu n'as rien de prévu {label}.")
        return
    details = []
    for event in events[:6]:
        if event.all_day:
            details.append(f"toute la journée, {spoken_title(event.title)}")
        else:
            details.append(f"à {_spoken_hour(event.start)}, {spoken_title(event.title)}")
    head = (f"Tu as {len(events)} rendez-vous {label}" if len(events) > 1
            else f"Tu as un rendez-vous {label}")
    speak(head + " : " + " ; ".join(details) + ".")


# --- writing to the calendar ------------------------------------------------

_EVENT_VERBS = ("ajoute", "ajouter", "cree", "creer", "note", "planifie", "programme",
                "mets", "met", "bloque", "reserve", "cale")
_EVENT_NOUNS = ("agenda", "calendrier", "rendez vous", "rdv", "reunion", "evenement",
                "creneau", "un appel")


def _is_calendar_create(command: str) -> bool:
    value = _flat(command)
    if not any(verb in value for verb in _EVENT_VERBS):
        return False
    return any(noun in value for noun in _EVENT_NOUNS)


def _event_title(raw: str) -> str:
    """Pull the event's label out of a dictated sentence."""
    value = str(raw or "").strip()
    # "… appelé X", "… pour X", "… : X": whatever follows is the title.
    for marker in (" intitule ", " intitulé ", " appele ", " appelé ", " nomme ", " nommé "):
        if marker in value.lower():
            return value[value.lower().index(marker) + len(marker):].strip(" .")[:120] or "Rendez-vous"
    # otherwise strip the filler words and the time markers.
    cleaned = re.sub(
        r"\b(ava|s'?il te pla[iî]t|ajoute|ajouter|cr[ée]{1,2}|cr[ée]er|note|planifie|programme|"
        r"mets|met|bloque|r[ée]serve|cale|un|une|le|la|les|des|du|de|dans|sur|à|a|mon|ma|mes|"
        r"agenda|calendrier|google|rendez[- ]vous|rdv|r[ée]union|[ée]v[ée]nement|cr[ée]neau)\b",
        " ", value, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(aujourd'?hui|demain|apr[èe]s[- ]demain|ce soir|lundi|mardi|mercredi|"
                     r"jeudi|vendredi|samedi|dimanche)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d{1,2}\s*(?:h|heures?|:)\s*\d{0,2}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpendant\s+\d+\s*(?:min|minutes?|h|heures?)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,")
    return cleaned[:120] or "Rendez-vous"


def _calendar_create(raw_command: str) -> None:
    set_assistant_state(AvaState.THINKING, "Écriture dans ton agenda")
    if not GOOGLE_CALENDAR.connected():
        speak("Je ne peux pas écrire dans Calendar tout seul. "
              "Connecte ton compte Google dans mes réglages et je m'en occupe.")
        return
    start = google_calendar.parse_french_datetime(raw_command)
    if start is None:
        speak("Il me manque l'heure. Dis-moi par exemple : ajoute un rendez-vous demain à 14 heures.")
        return
    minutes = google_calendar.parse_duration_minutes(raw_command)
    title = _event_title(raw_command)
    try:
        event = GOOGLE_CALENDAR.create_event(title, start, minutes=minutes)
    except google_calendar.NotConnected:
        speak("Ma connexion Google a expiré, reconnecte-moi dans les réglages.")
        return
    except Exception as exc:  # noqa: BLE001
        speak(f"Google a refusé : {exc}")
        return
    when = spoken_date(event.start.date())
    speak(f"C'est noté : {spoken_title(event.title)}, {when} à {_spoken_hour(event.start)}.")


def _is_screen_diagnosis(command: str) -> bool:
    direct = (
        "quel est ce probleme", "c est quoi ce probleme", "analyse mon ecran",
        "analyse l ecran", "regarde mon ecran", "qu est ce qui ne va pas",
        "explique cette erreur", "aide moi avec cette erreur", "diagnostic ecran",
    )
    return any(phrase in command for phrase in direct)


def _diagnose_screen(question: str) -> None:
    set_assistant_state(AvaState.THINKING, "Analyse locale de ton ecran")
    # Ava's panel must not cover the message the user is pointing at.
    ui("hide")
    time.sleep(0.22)
    reply = SCREEN_VISION.capture_and_analyze(question)
    ui("show")
    if reply.screenshot is not None:
        ui("preview", str(reply.screenshot), "Capture analysee localement — aucune action automatique")
    speak(reply.text)


def _research_synthesizer(prompt: str) -> str | None:
    reply = CONVERSATION.ask_once(prompt, max_tokens=260)
    return reply.text if reply.available else None


def _present_research(reply: ResearchReply) -> None:
    speak(reply.answer)
    if reply.sources:
        ui("sources", [
            {"title": source.title, "url": source.url}
            for source in reply.sources
        ])


def _research(query: str, *, om_match: bool = False) -> None:
    # offline, Ava used to answer "je n'ai pas trouve de source suffisamment
    # claire": she sounded like she'd searched and failed, when she hadn't even
    # left the mac. saying the real reason saves you asking the same question ten
    # times thinking you phrased it badly.
    if net.is_offline():
        _mark_route("hors-ligne")
        speak("Je n'ai pas de réseau pour le moment, je ne peux pas aller chercher ça.")
        return
    set_assistant_state(AvaState.THINKING, "Recherche et vérification des sources")
    try:
        reply = WEB_RESEARCH.next_om_match() if om_match else WEB_RESEARCH.answer(
            query, synthesizer=_research_synthesizer,
        )
    except Exception as exc:  # noqa: BLE001 - reseau coupe, page illisible...
        # the web search is the routing's last resort: if it blows up, it used
        # to take the whole command down with it.
        if DEBUG:
            print(f"[recherche] echec : {exc}")
        speak("Je n'arrive pas à chercher sur le web pour le moment.")
        return
    _present_research(reply)


# a duration containing the word "heure" is not a question about the time.
_DURATION_HINTS = ("quart d heure", "quart d'heure", "demi heure", "demi-heure",
                   "minuteur", "chrono", "timer", "rappelle", "reveille",
                   "previens", "pendant")
_APPOINTMENT_HINTS = ("rendez vous", "rendez-vous", "rdv", "reunion", "reunions",
                      "cours", "train", "avion", "match")


def _is_time_question(command: str) -> bool:
    """\"Quelle heure il est ?\" — and nothing else, above all.

    Testing `"heure" in c` caught everything containing the word: "rappelle-moi
    dans un quart d'heure" got told the time instead of starting a timer, and "a
    quelle heure est mon rendez-vous" never looked at the calendar.
    """
    value = str(command or "")
    if "heure" not in value:
        return False
    if any(hint in value for hint in _DURATION_HINTS):
        return False
    if any(hint in value for hint in _APPOINTMENT_HINTS):
        return False
    # "dans deux heures", "dans une heure": that's a delay, not a question.
    if re.search(r"\bdans\s+\S+\s*heure", value):
        return False
    return bool(re.search(r"\b(?:quelle heure|l heure|l'heure|heure qu il est|"
                          r"heure qu'il est)\b", value) or value.strip() == "heure")


def _looks_current_or_web(command: str) -> bool:
    return any(marker in command for marker in (
        "prochain match", "prochaine rencontre", "en ce moment", "aujourd hui sur internet",
        "actualite", "dernieres nouvelles", "resultat du match", "classement",
        "prix actuel", "qui est le president", "qui est la presidente",
    ))


# "merci" is handled higher up in the routing: no need to double it here.
SMALL_TALK = {
    "super": "Content que ça t'aille.",
    "cool": "Content que ça t'aille.",
    "parfait": "Content que ça t'aille.",
    "bonjour": "Bonjour Mathieu.",
    "salut": "Salut !",
    "bonsoir": "Bonsoir Mathieu.",
    "ca va": "Ça va, et toi ?",
    "comment ca va": "Ça va, et toi ?",
    "tu vas bien": "Ça va, et toi ?",
    "tu fais quoi": "Je t'écoute, c'est tout.",
    "tu es la": "Je suis là.",
    "tu m entends": "Je t'entends.",
    "rien": "D'accord.",
    "laisse tomber": "D'accord.",
    "annule": "D'accord, j'annule.",
    "non": "D'accord.",
    "oui": "D'accord.",
    "ok": "D'accord.",
    "au revoir": "À tout à l'heure.",
    "bonne nuit": "Bonne nuit.",
}


def _small_talk(command: str) -> str:
    reply = SMALL_TALK.get(_flat(command).strip())
    if reply and "Mathieu" in reply:
        return reply.replace("Mathieu", USER_NAME)
    return reply or ""


def execute_command(cmd: str) -> None:
    """The single way in, and the safety net: whatever happens below, Ava has to
    answer something rather than die quietly.

    It's also the measurement point: we note the route taken and the time it
    took, never what was said (see the header of `traces`).
    """
    with traces.span("commande", route="integre", network=False) as trace:
        _ROUTE.route = "integre"
        _ROUTE.network = False
        try:
            _dispatch_command(cmd)
        except Exception as exc:  # noqa: BLE001
            print(f"[ava] commande en echec : {exc}")
            _ROUTE.route = "echec"
            speak("Je n'ai pas réussi à aller au bout de cette demande.")
        finally:
            trace["route"] = _ROUTE.route
            trace["network"] = _ROUTE.network
            # every exchange leaves a line in the day's Obsidian note. traces
            # stay content-free by design; the vault is the opposite: it IS the
            # user's memory, on his own disk.
            _remember_safely("log_interaction", cmd, _last_spoken["text"])


# which route was taken is decided deep in the routing: we note it in passing
# rather than threading a return value back up through twenty branches.
_ROUTE = threading.local()

# the name said on its own after waking: that's somebody calling, not a command.
_CALLED_BY_NAME = frozenset({"ava", "hey ava", "ok ava", "eva", "avah"})
# what transcription gives back when you hesitate: neither command nor question.
_FILLERS = frozenset({"euh", "heu", "hein", "hm", "hmm", "mmh", "bah", "ben",
                      "voila", "donc", "alors"})
# a sentence cut short: ask for the missing half instead of googling the verb on
# its own.
_TRUNCATED = {
    "ouvre": "Ouvrir quoi ?",
    "ouvrir": "Ouvrir quoi ?",
    "lance": "Lancer quoi ?",
    "ferme": "Fermer quoi ?",
    "rappelle moi": "Te rappeler quoi, et quand ?",
    "previens moi": "Te prévenir de quoi, et quand ?",
    "cherche": "Chercher quoi ?",
    "mets": "Mettre quoi ?",
    "envoie": "Envoyer quoi, et à qui ?",
    "minuteur": "Un minuteur de combien de temps ?",
}


def _mark_route(route: str, network: bool = False) -> None:
    _ROUTE.route = route
    if network:
        _ROUTE.network = True


def _dispatch_command(cmd: str) -> None:
    raw = cmd.strip()
    c = _norm(raw)
    if not c:
        speak("Je n'ai pas compris, tu peux répéter ?")
        return
    # "salut ava", "merci ava": people call their assistant by name at the end of
    # a sentence as often as at the start. without this, those phrasings matched
    # no known command and ended up in a web search.
    if c in _CALLED_BY_NAME:
        speak("Oui ?")
        return
    c = _norm(strip_wake_suffix(c)) or c
    # hesitation: don't go searching the web because somebody said "euh".
    if c in _FILLERS:
        speak("Je t'écoute.")
        return

    # capabilities that combine several tools come before the generic verbs:
    # "ouvre mon agenda et dis-moi…" must not be taken for an application name,
    # and "quel est ce probleme" triggers the screen vision.
    # writing before reading: "ajoute un rdv demain" also contains "demain".
    if _is_calendar_create(c):
        _calendar_create(raw)
        return
    if _is_calendar_summary(c):
        _calendar_summary(c)
        return
    if _is_screen_diagnosis(c):
        _diagnose_screen(raw)
        return
    om_match = bool(
        any(phrase in c for phrase in ("prochain match", "prochaine rencontre"))
        and re.search(r"\b(?:om|marseille)\b", c)
    )
    if om_match:
        _research(raw, om_match=True)
        return

    # computer use: only the actions the deterministic parser recognises get run.
    # sending, pasting, closing and clicking a sensitive button all go through a
    # separate spoken confirmation.
    computer_candidate = parse_computer_intent(raw)
    pending_reply = (
        COMPUTER_USE.pending is not None
        and c in (COMPUTER_USE.CONFIRM_WORDS | COMPUTER_USE.CANCEL_WORDS)
    )
    if COMPUTER_USE_ENABLED and (computer_candidate is not None or pending_reply):
        set_assistant_state(AvaState.ACTING, "Action sur ton Mac")
        outcome = COMPUTER_USE.handle(raw, resolve_app)
        if outcome.needs_confirmation:
            prompt = outcome.intent.summary if outcome.intent else "Tu confirmes cette action ?"
            ui("choices", prompt, "Oui, confirmer", "Non")
            speak(f"{prompt} Tu confirmes ?")
        else:
            ui("clear_choices")
            speak(outcome.message or "Action terminée.")
        return

    # close what the ritual opened: the counterpart to open_startup_apps.
    if c in ("ferme tout", "ferme moi tout", "quitte tout", "range tout",
             "ferme mes applications", "ferme les applications"):
        closed = close_startup_apps()
        speak("Je ferme tout." if closed else "Il n'y a rien à fermer.")
        return

    # close or quit one application by name ("ferme la fenêtre"/"ferme l'onglet"
    # are exact computer-use matches and never reach this point; "arrête" stays
    # with the music so "arrête la musique" keeps pausing instead of quitting).
    for verb in ("ferme ", "fermer ", "quitte ", "quitter "):
        if c.startswith(verb):
            target = c.split(verb, 1)[1].strip()
            app = resolve_app(target)
            if app:
                _osascript(f'tell application "{app}" to quit')
                speak(f"Je ferme {app}.")
                return
            break   # not a known app: let the other intents have a look

    # Apostrophes and hyphens vary by transcription engine ("qu'est-ce" vs
    # "qu est ce"): memory intents match on a flattened copy.
    flat = re.sub(r"\s+", " ", re.sub(r"[-']", " ", c)).strip()

    # memory: "retiens que ..." goes to the Obsidian vault
    memory_match = re.match(
        r"(?:retiens|retiens bien|souviens toi|memorise|note dans ta memoire)"
        r"\s+(?:que\s+|qu\s+|de\s+)?(.+)", flat)
    if memory_match:
        fact = MEMORY.remember(memory_match.group(1))
        if fact:
            speak("C'est retenu, je l'ai noté dans ma mémoire.")
        else:
            speak("Qu'est-ce que je dois retenir ?")
        return

    # memory: "qu'est-ce que tu sais sur ..." / "tu te souviens de ..."
    recall_match = re.match(
        r"(?:que sais tu (?:sur|de)|qu est ce que tu sais (?:sur|de)|"
        r"tu te souviens (?:de|que)?|rappelle moi ce que tu sais (?:sur|de))\s*(.*)",
        flat)
    if recall_match:
        found = MEMORY.recall(recall_match.group(1) or c)
        if found:
            speak("Voilà ce que j'ai retenu : " + " Aussi : ".join(found))
        else:
            speak("Je n'ai encore rien retenu là-dessus. Dis-moi « retiens que » "
                  "suivi de l'information.")
        return

    # open the memory vault in Obsidian (or Finder when Obsidian is missing)
    if any(p in c for p in ("ta memoire", "ma memoire", "mon journal",
                            "le vault", "mon vault")):
        MEMORY.ensure()
        speak("J'ouvre ta mémoire dans Obsidian.", state="action")
        if resolve_app("obsidian"):
            open_url(MEMORY.obsidian_uri())
        else:
            subprocess.run(["open", str(MEMORY.vault)], check=False)
        return

    # agent mode: ava drives the screen herself (capture -> vision model ->
    # click/type), announcing every action. Bounded and cautious (screen_agent).
    for starter in ("prends la main", "pilote mon mac", "pilote l ecran",
                    "mode agent", "debrouille toi pour", "occupe toi de"):
        if c.startswith(starter):
            goal = c[len(starter):].strip(" ,.")
            goal = re.sub(r"^(et|pour|de)\s+", "", goal)
            if not goal:
                speak("Dis-moi quoi faire, par exemple : prends la main et "
                      "ouvre mes téléchargements.")
                return
            if not COMPUTER_USE.controller.accessibility_enabled():
                speak("Pour piloter l'écran il me faut l'accessibilité : Réglages, "
                      "Confidentialité et sécurité, Accessibilité.")
                return
            _mark_route("agent", network=True)
            set_assistant_state(AvaState.ACTING, "Je prends la main")
            speak("Je prends la main, je te dis tout ce que je fais.", state="action")
            # the capture covers the PRIMARY display only: scale to it, not to
            # the union of every screen (wrong as soon as there are two).
            from ava.mac.screen_agent import main_screen_size
            result = SCREEN_AGENT.run(
                goal, main_screen_size(),
                say=lambda msg: speak(msg, state="action"),
            )
            speak(result.message)
            _remember_safely(
                "log_event",
                f"🤖 mode agent « {goal} » : {'ok' if result.ok else 'stoppé'} "
                f"en {result.steps} tour(s) — {result.message}",
            )
            return

    # open an app or a site ("votre"/"offre" = voxtral mishearing "ouvre")
    for verb in ("ouvre moi ", "ouvre-moi ", "ouvre ", "ouvrir ", "lance ",
                 "lancer ", "demarre ", "affiche ", "votre ", "offre ",
                 "montre ", "va sur "):
        if c.startswith(verb):
            return _open_target(c.split(verb, 1)[1].strip())

    # a verb with no object: ask what, rather than searching the web for the verb
    # on its own ("ouvre" returned results about the word ouvre).
    if c in _TRUNCATED:
        speak(_TRUNCATED[c])
        return

    # mail: read the unread out loud when IMAP credentials are there, and when
    # the phrasing asks for content ("lis", "combien", "résume"...); a plain
    # "ouvre mes mails" still opens Gmail in the browser.
    if any(w in c for w in ("mail", "mails", "gmail", "courriel", "boite mail")):
        wants_reading = any(w in c for w in (
            "lis", "lire", "resume", "combien", "nouveau", "nouveaux", "non lus",
            "recus", "check", "verifie", "regarde",
        ))
        if wants_reading and mailbox.credentials()[0]:
            _mark_route("mails", network=True)
            summary = mailbox.fetch_unread()
            if summary.available:
                speak(mailbox.spoken_summary(summary))
                _remember_safely("log_event", f"📬 mails : {summary.unread} non lus")
                return
            speak("Je n'arrive pas à joindre Gmail pour le moment, j'ouvre ta boîte.")
            open_url("https://mail.google.com/mail/u/0/")
            return
        open_url("https://mail.google.com/mail/u/0/")
        if wants_reading:
            speak("J'ouvre Gmail. Pour que je lise tes mails à la voix, ajoute un "
                  "mot de passe d'application Google dans mon fichier point env.")
        else:
            speak("J'ouvre ta boîte mail.")
        return

    # in-app search: Ava reads the results and shows her sources, without pushing
    # the user out to a browser unless they click on purpose.
    search_match = re.match(
        r"^\s*(?:recherche(?:-moi| moi)?|cherche(?:-moi| moi)?|google)\s+(.+)$",
        raw, re.IGNORECASE,
    )
    if search_match:
        _research(search_match.group(1).strip())
        return

    # what time it is
    if _is_time_question(c):
        now = datetime.datetime.now()
        speak(f"Il est {now.hour} heures {now.minute:02d}.")
        return

    # today's date
    if "quel jour" in c or "quelle date" in c:
        speak("Nous sommes le " + datetime.date.today().strftime("%d/%m/%Y") + ".")
        return

    # weather
    if "meteo" in c or "quel temps" in c or "il fait combien" in c:
        speak(weather_sentence() or "Météo indisponible pour le moment.")
        return

    # ai news. "quoi de neuf en IA" contained none of the expected words and went
    # off to a web search; "ia" is matched as a whole word, or it gets caught
    # inside "diaporama", "italie", "bavarois"…
    if ("intelligence artificielle" in c or "actu" in c or "nouveaute" in c
            or ("quoi de neuf" in c and not _is_calendar_summary(c))
            or re.search(r"\b(?:ia|ai)\b", c)):
        speak(ai_news_sentence() or "Rien de neuf côté intelligence artificielle.")
        return

    # transport words said on their own: "pause", "stop", "reprends".
    # they used to fall into the web search net and Ava went off to look up the
    # word "pause" on the internet.
    if c in ("pause", "stop", "silence", "chut", "coupe", "coupe la musique", "stop la musique"):
        _spotify("pause"); speak("Je coupe."); return
    if c in ("play", "reprends", "reprend", "continue", "relance"):
        _spotify("play"); speak("Je reprends."); return
    # "suivant" / "precedent" said alone: without the word "morceau" they fell
    # into the net and Ava searched the web for "precedent".
    if c in ("suivant", "suivante", "next", "la suite", "passe", "zappe"):
        _spotify("next track"); speak("Morceau suivant."); return
    if c in ("precedent", "precedente", "previous", "retour", "reviens", "davant"):
        _spotify("previous track"); speak("Morceau précédent."); return

    # spotify transport: next track, previous track, resume
    if any(w in c for w in ("musique", "chanson", "morceau", "titre", "spotify", "son morceau")):
        if any(w in c for w in ("suivant", "suivante", "prochain", "prochaine", "next", "apres")):
            _spotify("next track"); speak("Morceau suivant."); return
        if any(w in c for w in ("precedent", "precedente", "reviens", "davant", "previous", "retour")):
            _spotify("previous track"); speak("Morceau précédent."); return
        if any(w in c for w in ("reprend", "continue", "relance", "remets", "remet")):
            _spotify("play"); speak("Je reprends la musique."); return

    # lock the screen
    if "verrouille" in c or "verrouiller" in c or ("lock" in c and "ecran" in c):
        _osascript('tell application "System Events" to keystroke "q" using {control down, command down}')
        speak("Je verrouille l'écran."); return

    # screenshot (before brightness, or "ecran" is ambiguous)
    if "capture" in c or "screenshot" in c or "screen shot" in c:
        dest = str(Path.home() / "Desktop" /
                   ("capture-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"))
        subprocess.run(["screencapture", "-x", dest], check=False)
        speak("Capture d'écran enregistrée sur le bureau."); return

    # volume
    if re.search(r"\bson\b", c) or "volume" in c or "sonore" in c:
        if any(w in c for w in ("coupe", "muet", "mute", "silence", "eteins")):
            _osascript("set volume output muted true"); speak("Son coupe."); return
        if any(w in c for w in ("remets", "retablis", "active", "remet")):
            _osascript("set volume output muted false"); speak("Son retabli."); return
        n = _parse_number(c)
        if n is not None and ("a " in c or "sur " in c or "%" in c or "pour cent" in c):
            _set_volume(n); speak(f"Volume a {max(0, min(100, n))} pour cent."); return
        if any(w in c for w in ("monte", "augmente", "plus fort", "plus haut", "plus")):
            _set_volume(_volume_now() + 15); speak("Je monte le son."); return
        if any(w in c for w in ("baisse", "diminue", "moins fort", "reduis", "moins", "bas")):
            _set_volume(_volume_now() - 15); speak("Je baisse le son."); return
        if n is not None:
            _set_volume(n); speak(f"Volume a {max(0, min(100, n))} pour cent."); return

    # brightness (through the system keys; may not work on every mac)
    if "luminosite" in c or "lumiere" in c or ("ecran" in c and any(
            w in c for w in ("clair", "sombre", "lumineux", "fonce"))):
        up = any(w in c for w in ("monte", "augmente", "plus", "clair", "lumineux"))
        code = 144 if up else 145
        for _ in range(4):
            _osascript(f'tell application "System Events" to key code {code}')
        speak("Voila." if up else "C'est fait."); return

    # timer
    if any(w in c for w in ("minuteur", "chrono", "timer")) or \
            ("dans" in c and any(w in c for w in ("rappelle", "reveille", "previens"))):
        secs, label = _parse_duration_s(c)
        if secs:
            start_timer(secs, "C'est l'heure ! Ton minuteur est terminé.")
            speak(f"Minuteur lancé pour {label}."); return
        speak("Pour combien de temps ?"); return

    # a quick note (appended to ~/Documents/ava-notes.md)
    if re.search(r"\bnote", c) or "prends note" in c or "rappelle moi de" in c or "ajoute une note" in c:
        m = re.search(r"(?:noter?|note[rz]?|prends? note|rappelle[- ]?moi de|ajoute une note)"
                      r"\s+(?:que\s+|de\s+|d'\s*)?(.*)", raw, re.I)
        content = (m.group(1).strip() if m else "").strip(" .")
        if content:
            _append_note(content)
            speak("C'est noté, dans ton vault."); return
        speak("Qu'est-ce que je dois noter ?"); return

    # music (spotify): play your favourite track, or pause
    if ("musique" in c or "chanson" in c or "spotify" in c
            or c.startswith("joue") or c.startswith("mets") or "play" in c):
        if any(w in c for w in ("pause", "arrete", "coupe", "stop", "eteins")):
            _osascript('tell application "Spotify" to pause')
            speak("Je mets la musique en pause.")
        else:
            play_spotify()
            speak("Je lance ta musique.")
        return

    if "merci" in c:
        speak("Avec plaisir !")
        return

    if _looks_current_or_web(c):
        _research(raw)
        return

    # open conversation through LM Studio (or any local openai-compatible server).
    # nothing leaves the Mac by default.
    discussion_starters = (
        "discute avec moi de ", "parle moi de ", "explique moi ",
        "qu est ce que ", "qu'est ce que ", "que penses tu de ",
        "aide moi a reflechir a ",
    )
    for starter in discussion_starters:
        if c.startswith(starter):
            question = raw[len(starter):].strip() or raw
            answer = CONVERSATION.ask(question, context=_memory_context())
            if answer.available:
                speak(answer.text)
            else:
                speak("Je n'arrive à joindre aucun moteur de discussion pour le moment.")
            return

    # a sentence with no verb but carrying a known app ("l'application discord",
    # "votre discord"…): open the app rather than searching the web.
    # (last, so it doesn't short-circuit music, mail and the rest.)
    for token in c.replace("'", " ").split():
        if token in _APP_ALIASES:
            return _open_target(token)

    # politeness and filler: answer briefly, and above all don't go off to a web
    # search because somebody said "merci".
    courtesy = _small_talk(c)
    if courtesy:
        speak(courtesy)
        return

    # last resort before the net: ask a small model what was actually meant.
    # everything reaching this point was going to end up in a web search anyway,
    # so no command that already worked gets any slower.
    if _dispatch_understood(raw):
        return

    # conversation by default. If no engine answers, Ava runs a sourced in-app
    # search instead of opening an opaque tab.
    answer = CONVERSATION.ask(raw, context=_memory_context())
    if answer.available:
        speak(answer.text)
    else:
        _research(raw)


def _dispatch_understood(raw: str) -> bool:
    """Run the intent the model guessed. False means we understood nothing.

    Every branch reuses exactly the same actions as the keyword routing: this
    module decides *what* to do, never *how*.
    """
    installed = skills.discover() if SKILLS_ENABLED else []
    cached = INTENT_ROUTER.knows(raw)
    result = INTENT_ROUTER.understand(raw, skills.catalogue(installed))
    if not result.usable:
        return False
    # a phrasing already learned costs nothing now: you can see it in the traces.
    _mark_route("comprehension" if cached else "comprehension-reseau",
                network=not cached)
    if result.intent == "competence":
        _mark_route("competence", network=not cached)
        return _run_skill(result.target, raw, installed)
    if DEBUG:
        print(f"[compréhension] {result.intent} cible={result.target!r} "
              f"valeur={result.value} confiance={result.confidence:.2f}")

    intent, target, value = result.intent, result.target, result.value

    if intent == "ouvrir_app":
        if not target:
            return False
        _open_target(_norm(target))
        return True
    if intent == "ouvrir_site":
        if not target:
            return False
        url = target if target.startswith(("http://", "https://")) else f"https://{target}"
        open_url(url)
        speak(f"J'ouvre {target}.")
        return True
    if intent == "mail":
        open_url("https://mail.google.com/mail/u/0/")
        speak("J'ouvre ta boîte mail.")
        return True
    if intent == "lire_mails":
        if mailbox.credentials()[0]:
            _mark_route("mails", network=True)
            summary = mailbox.fetch_unread()
            if summary.available:
                speak(mailbox.spoken_summary(summary))
                _remember_safely("log_event", f"📬 mails : {summary.unread} non lus")
                return True
        open_url("https://mail.google.com/mail/u/0/")
        speak("J'ouvre ta boîte mail.")
        return True
    if intent == "retenir":
        if not target:
            speak("Qu'est-ce que je dois retenir ?"); return True
        MEMORY.remember(target)
        speak("C'est retenu, je l'ai noté dans ma mémoire.")
        return True

    if intent == "musique_jouer":
        play_spotify(); speak("Je lance ta musique."); return True
    if intent == "musique_pause":
        _spotify("pause"); speak("Je coupe."); return True
    if intent == "musique_suivant":
        _spotify("next track"); speak("Morceau suivant."); return True
    if intent == "musique_precedent":
        _spotify("previous track"); speak("Morceau précédent."); return True

    if intent == "volume":
        if value is not None:
            level = max(0, min(100, int(value)))
            _set_volume(level); speak(f"Volume à {level} pour cent.")
            return True
        if "bais" in target or "moins" in target or "down" in target:
            _set_volume(_volume_now() - 15); speak("Je baisse le son."); return True
        if "mont" in target or "plus" in target or "up" in target:
            _set_volume(_volume_now() + 15); speak("Je monte le son."); return True
        return False
    if intent == "luminosite":
        up = "mont" in target or "plus" in target or "clair" in target
        for _ in range(4):
            _osascript(f'tell application "System Events" to key code {144 if up else 145}')
        speak("Voilà."); return True

    if intent == "minuteur":
        if not value or value <= 0:
            speak("Pour combien de temps ?"); return True
        seconds = int(min(value, 12 * 3600))
        start_timer(seconds, "C'est l'heure ! Ton minuteur est terminé.")
        speak(f"Minuteur lancé pour {_spoken_duration(seconds)}.")
        return True

    if intent == "note":
        if not target:
            speak("Qu'est-ce que je dois noter ?"); return True
        _append_note(target); speak("C'est noté."); return True

    if intent == "heure":
        now = datetime.datetime.now()
        speak(f"Il est {now.hour} heures {now.minute:02d}."); return True
    if intent == "date":
        speak("Nous sommes le " + datetime.date.today().strftime("%d/%m/%Y") + ".")
        return True
    if intent == "meteo":
        speak(weather_sentence() or "Météo indisponible pour le moment."); return True
    if intent == "actu":
        speak(ai_news_sentence() or "Rien de neuf côté intelligence artificielle.")
        return True

    if intent == "agenda_lire":
        _calendar_summary(_norm(raw)); return True
    if intent == "agenda_creer":
        _calendar_create(raw); return True

    if intent == "capture_ecran":
        destination = str(Path.home() / "Desktop" / (
            "capture-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"))
        subprocess.run(["screencapture", "-x", destination], check=False)
        speak("Capture d'écran enregistrée sur le bureau."); return True
    if intent == "verrouiller":
        _osascript('tell application "System Events" to keystroke "q" '
                   'using {control down, command down}')
        speak("Je verrouille l'écran."); return True

    if intent == "recherche_web":
        _research(target or raw); return True
    if intent == "discussion":
        answer = CONVERSATION.ask(target or raw, context=_memory_context())
        if answer.available:
            speak(answer.text)
            return True
        return False        # pas de moteur : on laisse la recherche web prendre

    return False


def _run_skill(name: str, raw: str, installed: list) -> bool:
    """The activation and execution steps of a skill.

    Two ways for a skill to answer: a script we run and whose output we read,
    or — if there isn't one — its instructions handed to the conversation
    engine. Either way Ava says what comes back; she invents nothing.
    """
    skill = skills.find(name, installed)
    if skill is None:
        if DEBUG:
            print(f"[skills] « {name} » n'existe pas (cache périmé ?)")
        return False
    set_assistant_state(AvaState.ACTING, skill.name)
    if DEBUG:
        print(f"[skills] activation de « {skill.name} »")

    if skill.script() is not None:
        ok, output = skills.run_script(skill, raw)
        if ok and output:
            speak(output)
            return True
        speak(output or f"La compétence {skill.name} n'a rien renvoyé.")
        return True

    # no script: the SKILL.md instructions become the brief.
    instructions = skill.instructions()
    if not instructions.strip():
        return False
    answer = CONVERSATION.ask_once(
        f"{instructions}\n\nDemande de l'utilisateur : {raw}\n"
        "Réponds en français, en moins de 60 mots, pour être lu à voix haute.")
    if answer.available:
        speak(answer.text)
        return True
    return False


def _append_note(content: str) -> None:
    # Notes used to pile up in a flat ~/Documents/ava-notes.md; they now become
    # real linked notes in the Obsidian vault. The flat file stays as a fallback
    # so a disk hiccup never loses a dictated note.
    try:
        MEMORY.quick_note(content)
        return
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[memoire] note vers le vault impossible : {exc}")
    path = Path.home() / "Documents" / "ava-notes.md"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"- {stamp} — {content.strip(' .')}\n")


def _spoken_duration(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} heure" + ("s" if hours > 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes > 1 else ""))
    if secs and not hours:
        parts.append(f"{secs} seconde" + ("s" if secs > 1 else ""))
    return " et ".join(parts) if parts else "quelques secondes"


# --- the assistant loop: "ok ava …" plus a command ---------------------------

# --- transcribing the command with whisper (local, accurate in french) -------
WHISPER_SIZE = os.getenv("AVA_WHISPER_SIZE", "small").strip()  # small ou medium
_whisper = None
_whisper_lock = threading.Lock()
_vosk_model = None
_vosk_model_lock = threading.Lock()


def get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
        return _whisper


def get_vosk_model():
    """Charge une fois le modele partage par le wake word et le texte en direct."""
    global _vosk_model
    if not WAKE_ENABLED:
        return None
    with _vosk_model_lock:
        if _vosk_model is None:
            from vosk import Model
            _vosk_model = Model(str(WAKE_MODEL_DIR))
        return _vosk_model


class AdaptiveSpeechGate:
    """A light VAD that adapts to the room's noise before every command."""

    def __init__(self, silence_s: float = 0.68, min_speech_s: float = 0.18) -> None:
        self.silence_s = max(0.2, float(silence_s))
        self.min_speech_s = max(0.05, float(min_speech_s))
        self.noise_floor = 0.003
        self.started = False
        self.elapsed = 0.0
        self.voiced = 0.0
        self.silence = 0.0

    @property
    def start_threshold(self) -> float:
        return min(0.04, max(0.0065, self.noise_floor * 2.35))

    @property
    def stop_threshold(self) -> float:
        return min(0.028, max(0.0045, self.noise_floor * 1.55))

    def feed(self, rms: float, duration: float) -> bool:
        """Take in one level, returning ``True`` when the utterance is over."""
        level = max(0.0, float(rms))
        block_s = max(0.0, float(duration))
        self.elapsed += block_s
        if not self.started:
            threshold = self.start_threshold
            if level >= threshold:
                self.started = True
                self.voiced += block_s
                return False
            # The floor follows the fans and the music slowly, without being
            # dragged down by the brief start of a word.
            self.noise_floor = 0.94 * self.noise_floor + 0.06 * min(level, 0.04)
            return False

        if level > self.stop_threshold:
            self.voiced += block_s
            self.silence = 0.0
        else:
            self.silence += block_s
        return self.voiced >= self.min_speech_s and self.silence >= self.silence_s


def _drain_command_queue() -> None:
    while True:
        try:
            _command_q.get_nowait()
        except queue.Empty:
            return


def _live_recognizer():
    try:
        from vosk import KaldiRecognizer
        model = get_vosk_model()
        return KaldiRecognizer(model, WAKE_SR) if model is not None else None
    except Exception:
        return None


def _record_utterance(max_s: float = 14.0, silence_s: float = 0.68,
                      start_timeout_s: float = 4.5) -> bytes:
    """Capture one utterance off a dedicated queue, publishing a live draft."""
    frames: list[bytes] = []
    preroll: deque[bytes] = deque(maxlen=36)  # 360 ms, protege la premiere syllabe
    gate = AdaptiveSpeechGate(silence_s=silence_s)
    live = _live_recognizer()
    live_text = ""
    started_at = time.monotonic()
    _drain_command_queue()
    _capture_audio.set()
    try:
        while True:
            try:
                data = _command_q.get(timeout=0.25)
            except queue.Empty:
                if gate.started and time.monotonic() - started_at > max_s:
                    break
                if not gate.started and time.monotonic() - started_at > start_timeout_s:
                    break
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size == 0:
                continue
            duration = arr.size / WAKE_SR
            rms = float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))
            was_started = gate.started
            complete = gate.feed(rms, duration)
            if not was_started:
                preroll.append(data)
                if gate.started:
                    frames.extend(preroll)
                    if live is not None:
                        for block in preroll:
                            live.AcceptWaveform(block)
                    preroll.clear()
            else:
                frames.append(data)
                if live is not None:
                    try:
                        accepted = live.AcceptWaveform(data)
                        payload = json.loads(live.Result() if accepted else live.PartialResult())
                        candidate = (payload.get("text") if accepted else payload.get("partial")) or ""
                        candidate = candidate.strip()
                        if candidate and candidate != live_text:
                            live_text = candidate
                            ui("transcript", candidate, False)
                    except Exception:
                        live = None
            if complete or (gate.started and gate.elapsed >= max_s):
                break
    finally:
        _capture_audio.clear()
    return b"".join(frames)


# --- voxtral (mistral api): the main transcription when a key is present -----
# a wav goes multipart to /v1/audio/transcriptions, model voxtral-mini-latest,
# with the local fallback (whisper) if there's no key, no network or an error.
# nothing ever breaks.
MISTRAL_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
VOXTRAL_MODEL = os.getenv("VOXTRAL_MODEL", "voxtral-mini-latest").strip()
MISTRAL_BASE = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1").strip()


def _pcm_to_wav(pcm: bytes) -> bytes:
    import io
    import wave as _wave
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(WAKE_SR)
        w.writeframes(pcm)
    return buf.getvalue()


def voxtral_transcribe(pcm: bytes):
    # returns the text, or None if unavailable (the whisper fallback takes over)
    if not MISTRAL_KEY or not pcm:
        return None
    try:
        r = requests.post(
            f"{MISTRAL_BASE}/audio/transcriptions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
            files={"file": ("audio.wav", _pcm_to_wav(pcm), "audio/wav")},
            data={"model": VOXTRAL_MODEL, "language": "fr"},
            timeout=8,
        )
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
        if DEBUG:
            print(f"[voxtral] '{text}'")
        return text
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[voxtral] indisponible ({exc}) -> whisper local")
        return None


_ASR_HALLUCINATIONS = {
    "merci d avoir regarde cette video",
    "sous titres realises par la communaute d amara org",
    "a suivre",
}


def clean_transcript(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n.,!?;:")
    normalized = re.sub(r"[^a-z0-9]+", " ", _norm(value)).strip()
    if not value or normalized in _ASR_HALLUCINATIONS:
        return ""
    wake = extract_wake(value, WAKE_PHRASES)
    if wake.detected:
        return wake.trailing_command.strip()
    # even though the current wake rule only fires on "ava" alone, we clean off
    # any "ok ava"/"bonjour ava" said at the head of a command (all one breath).
    stripped = strip_wake_prefix(value)
    if stripped is not None:
        return stripped
    return value


# whisper large-v3-turbo compiled for the mac's gpu. measured on this mac, same
# 11 s clip of french:
#
#     faster-whisper small (cpu)   1.85 s   "Bonjour Mathieu, c'est **Hava**"
#     voxtral (network)            ~1-2 s plus latency, and a key that runs out
#     whisper-large-v3-turbo (mlx) **0.32 s**  "Mathieu, c'est **Ava**"
#
# so local is no longer the degraded fallback: it is both the fastest and the
# most accurate. a 3.5 s command transcribes in 0.23 s, with no network at all.
MLX_WHISPER_MODEL = os.getenv("AVA_STT_MODEL", "mlx-community/whisper-large-v3-turbo").strip()
_mlx_whisper_ok = True


def mlx_transcribe(pcm: bytes) -> str | None:
    """Local transcription on the gpu. None if mlx isn't available."""
    global _mlx_whisper_ok
    if not _mlx_whisper_ok or not pcm:
        return None
    try:
        import mlx_whisper
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=MLX_WHISPER_MODEL, language="fr",
            condition_on_previous_text=False, temperature=0.0,
            # steer decoding towards command vocabulary: without this, "ava"
            # comes back as "hava", "à va", "ava ?"…
            initial_prompt="Commande vocale en français adressée à Ava, "
                           "assistante sur Mac : ouvre une application, mets la "
                           "musique, quelle heure est-il, la météo, mon agenda.",
        )
        text = str(result.get("text", "")).strip()
        if DEBUG:
            print(f"[whisper-mlx] '{text}'")
        return text
    except ImportError:
        # a machine without apple silicon: don't retry on every sentence.
        _mlx_whisper_ok = False
        return None
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[whisper-mlx] échec : {exc}")
        return None


def transcribe(pcm: bytes) -> str:
    if not pcm:
        return ""
    # 1) the mac's gpu: fastest, most accurate, and it depends on nothing.
    text = mlx_transcribe(pcm)
    if text:
        return clean_transcript(text)
    # 2) voxtral, only if explicitly asked for (it leaves the mac).
    if os.getenv("AVA_USE_VOXTRAL") == "1":
        text = voxtral_transcribe(pcm)
        if text:
            return clean_transcript(text)
    # 3) safety net: whisper on the cpu, if mlx doesn't exist on this machine.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # vad_filter strips silence and noise (which keeps whisper from
    # hallucinating), initial_prompt steers towards french command vocabulary.
    segments, _ = get_whisper().transcribe(
        audio, language="fr", beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 420, "speech_pad_ms": 180},
        condition_on_previous_text=False,
        temperature=0.0,
        no_speech_threshold=0.5,
        initial_prompt="Conversation naturelle avec Ava, assistante Mac. "
                       "Commande vocale en francais : ouvre une application, "
                       "cherche sur internet, mets la musique, quelle heure est-il, "
                       "la meteo, mes mails.",
    )
    return clean_transcript(" ".join(s.text for s in segments))


COMPUTER_USE = ComputerUseEngine(
    ttl_s=SETTINGS["computer_use"]["confirmation_ttl_seconds"],
)
WEB_RESEARCH = WebResearch()
SCREEN_VISION = ScreenVision()
SCREEN_AGENT = ScreenAgent()
# Ava's long-term memory lives in an Obsidian vault of plain markdown.
# NOT ~/Documents/Ava: that's this repository (macOS is case-insensitive).
MEMORY = ObsidianMemory(
    Path(os.getenv("AVA_VAULT_DIR", str(Path.home() / "Documents" / "AvaVault"))),
)


def _remember_safely(fn: str, *args) -> None:
    # The vault must never take the voice down with it: disk errors are
    # logged in debug and otherwise swallowed on purpose.
    try:
        getattr(MEMORY, fn)(*args)
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[memoire] {fn} impossible : {exc}")


def _memory_context() -> str:
    # Remembered facts, slipped into the local discussion (never blocking).
    try:
        return MEMORY.context_for_llm()
    except Exception:  # noqa: BLE001
        return ""
CONVERSATION = LocalConversationEngine(
    base_url=SETTINGS["conversation"]["base_url"],
    model=SETTINGS["conversation"]["model"],
)


def apply_runtime_settings(settings: dict) -> None:
    global SETTINGS, MIN_RMS, MIN_GAP_S, MAX_GAP_S, CLAP_ENABLED
    global SPOTIFY_URI, APPS, CITY, USER_NAME, WAKE_PHRASES, SYSTEM_VOICE
    global COMPUTER_USE_ENABLED, SKILLS_ENABLED
    global CONTINUOUS_LISTENING, FOLLOWUP_TIMEOUT_S, MAX_CONTINUOUS_TURNS
    SETTINGS = settings
    clap = settings["wake"]["clap"]
    MIN_RMS = clap_min_rms(clap["sensitivity"])
    MIN_GAP_S = clap["min_gap_ms"] / 1000
    MAX_GAP_S = clap["max_gap_ms"] / 1000
    CLAP_ENABLED = clap["enabled"]
    SPOTIFY_URI = settings["morning"]["spotify_uri"]
    APPS = [(item["name"], item["position"], item.get("url", ""))
            for item in settings["morning"]["apps"]]
    CITY = settings["identity"]["city"]
    USER_NAME = settings["identity"]["name"]
    WAKE_PHRASES = tuple(settings["wake"]["phrases"])
    SYSTEM_VOICE = settings["voice"]["system_fallback"]
    COMPUTER_USE_ENABLED = settings["computer_use"]["enabled"]
    SKILLS_ENABLED = settings["skills"]["enabled"]
    CONTINUOUS_LISTENING = settings["conversation"]["continuous_listening"]
    FOLLOWUP_TIMEOUT_S = settings["conversation"]["followup_timeout_seconds"]
    MAX_CONTINUOUS_TURNS = settings["conversation"]["max_continuous_turns"]
    COMPUTER_USE.ttl_s = settings["computer_use"]["confirmation_ttl_seconds"]
    CONVERSATION.configure(
        settings["conversation"]["base_url"], settings["conversation"]["model"],
    )
    with _welcome_lock:
        _welcome.update(day=None, ts=0.0, text="", path=None)


CONFIG.subscribe(apply_runtime_settings)


def _reserve_interaction() -> bool:
    with _trigger_lock:
        if _flow_active.is_set():
            return False
        _flow_active.set()
        return True


# what gives away a sentence aimed at ava: an action verb, a question, a bit of
# politeness. a conversation on television needs none of those.
_ADDRESSED_MARKERS = (
    "ouvre", "ouvrir", "lance", "ferme", "mets", "met ", "coupe", "monte",
    "baisse", "rappelle", "ajoute", "cherche", "dis moi", "explique", "montre",
    "envoie", "note", "joue", "arrete", "verrouille", "affiche", "trouve",
    "quel", "quelle", "qui ", "quoi", "comment", "pourquoi", "quand", "combien",
    "est ce que", "peux tu", "tu peux", "merci", "stop", "pause", "s il te plait",
)


def looks_ambient(text: str) -> bool:
    """Is this a stream of speech that wasn't aimed at Ava?

    Straight out of the real logs: a "salut ava" opened the mic while a match was
    on television, and Ava went off to search the web for "Thibaut Delphis et
    les Anéciens", then followed up twice more on the commentary. The mic hears
    the whole room; the only clue available is the **shape** of what got
    transcribed.

    A command is short and fits in one sentence. Sports commentary, a meeting or
    the radio arrive as several sentences with no action verb and no question —
    that contrast is what we read here, never the subject.
    """
    raw = str(text or "").strip()
    words = _norm(raw).split()
    sentences = [part for part in re.split(r"[.!?…]+", raw) if part.strip()]
    # the number of sentences decides before the length does: "Thibaut Delphis.
    # Même s'il n'arrive plus a se relancer. Desormais, les Aneciens" is only
    # twelve words, and it's pure sports commentary all the same.
    if len(sentences) >= 3 and len(words) >= 8:
        return True
    # ⚠️ a question word doesn't prove you're talking to Ava: "ce que j'adore
    # c'est **quand** on est dans la baignoire" came from a video playing in the
    # room, and "quand" made it look like a question. Past twenty-five words over
    # several sentences it's a story, not a request — nobody commands their
    # assistant in forty words.
    if len(words) > 25 and len(sentences) >= 2:
        return True
    if len(words) <= 12:            # une commande tient en une respiration
        return False
    value = _norm(raw)
    return not any(marker in value for marker in _ADDRESSED_MARKERS)


def _followup_should_stop(command: str) -> bool:
    value = _norm(command)
    return value in {
        "stop", "arrete", "arrete la conversation", "c est bon", "cest bon",
        "termine", "fin", "merci ava", "non merci", "je n ai plus besoin",
    }


def _run_continuous_conversation(command: str, *, display_initial: bool) -> None:
    """Run several turns without asking for the wake word between answers."""
    current = command.strip()
    show_user = display_initial
    for turn in range(MAX_CONTINUOUS_TURNS):
        if _followup_should_stop(current):
            speak("D'accord, je reste disponible.")
            return
        if show_user:
            ui("transcript", current, True)
            ui("message", "user", current, "")
        set_assistant_state(AvaState.THINKING, "Je m'en occupe")
        execute_command(current)
        if not CONTINUOUS_LISTENING or turn + 1 >= MAX_CONTINUOUS_TURNS:
            return

        # Ava's audio has just finished: flush her voice and open a follow-up
        # window straight away. Silence simply closes the session; it doesn't
        # trigger another spoken answer.
        _capture_audio.clear()
        _drain_command_queue()
        _drain_wake_queue()
        time.sleep(0.16)
        set_assistant_state(AvaState.LISTENING, "Tu peux enchainer")
        ui("transcript", "", False)
        followup = transcribe(_record_utterance(
            max_s=14.0,
            silence_s=0.62,
            start_timeout_s=FOLLOWUP_TIMEOUT_S,
        )).strip()
        if len(followup) < 3:
            ui("transcript", "", False)
            return
        # in a follow-up no wake word was said: if what comes in sounds like the
        # room rather than a request, close without answering.
        if looks_ambient(followup):
            print(f"[ava] suivi {turn + 1} ignoré (ambiance) : '{followup[:60]}'")
            ui("transcript", "", False)
            return
        print(f"[ava] suivi {turn + 1} : '{followup}'")
        current = followup
        show_user = True


def handle_wake(prefilled_command: str = "", *, reserved: bool = False) -> None:
    if not reserved and not _reserve_interaction():
        return
    try:
        set_assistant_state(AvaState.LISTENING, "Je t'ecoute", force=True)
        ui("clear_choices")
        ui("transcript", "", False)
        command = prefilled_command.strip()
        if not command:
            command = transcribe(_record_utterance()).strip()
        if len(command) < 3:
            # A silent second chance: Ava no longer talks over the start of the
            # command, and the mic stays immediately available.
            set_assistant_state(AvaState.LISTENING, "Je n'ai rien entendu — reessaie")
            command = transcribe(_record_utterance()).strip()
        print(f"[ava] commande : '{command}'")
        if len(command) < 3:
            speak("Je n'ai pas compris. Tu peux parler plus près du micro ou écrire ici.")
            return
        # the wake word was said, but what followed may have come from the room
        # (television, a meeting). say so, rather than going off to search the
        # web for the name of a footballer overheard in passing.
        if looks_ambient(command):
            print(f"[ava] commande ignorée (ambiance) : '{command[:60]}'")
            speak("Je n'ai pas bien saisi, tu peux répéter ?")
            return
        _run_continuous_conversation(command, display_initial=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ava] interaction interrompue : {exc}")
        set_assistant_state(AvaState.ERROR, "Une erreur est survenue", force=True)
        ui("error", "Je n'ai pas pu terminer cette demande.")
    finally:
        _capture_audio.clear()
        _drain_command_queue()
        _drain_wake_queue()
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        return_to_idle()


def submit_text_command(text: str) -> dict:
    """The non-blocking way in from the text field and the Oui / Non buttons."""
    command = str(text or "").strip()[:4000]
    if not command:
        return {"accepted": False, "error": "Écris une demande avant de l'envoyer."}
    # "stop" while she's talking means "be quiet", not "run a stop command": it
    # has to cut in without waiting for the end of the sentence.
    if _norm(command) in _HUSH and stop_speaking():
        return {"accepted": True, "interrupted": True}
    if _norm(command) in {"bonjour ava", "bonsoir ava", "coucou ava"}:
        trigger_welcome("bonjour ava (texte)")
        return {"accepted": True}
    if not _reserve_interaction():
        return {"accepted": False, "error": "Ava termine déjà une demande."}

    def worker() -> None:
        try:
            set_assistant_state(AvaState.THINKING, "Je m'en occupe", force=True)
            _run_continuous_conversation(command, display_initial=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[ava] commande texte interrompue : {exc}")
            set_assistant_state(AvaState.ERROR, "Une erreur est survenue", force=True)
            ui("error", "Je n'ai pas pu terminer cette demande.")
        finally:
            _drain_wake_queue()
            _last_trigger["ts"] = time.time()
            _flow_active.clear()
            return_to_idle()

    threading.Thread(target=worker, daemon=True, name="ava-text-command").start()
    return {"accepted": True}


# what you say or type to shut her up, without asking her for anything else.
_HUSH = frozenset({"stop", "chut", "tais toi", "silence", "arrete", "arrete toi",
                   "ca suffit", "stop ava", "c est bon"})


def start_voice_interaction() -> dict:
    """Start listening from the mic button, with no wake word required."""
    # clicking the mic while she's talking means you want to cut her off and
    # speak: we don't ask anyone to wait out the end of a briefing.
    stop_speaking()
    if not _reserve_interaction():
        return {"accepted": False, "error": "Ava termine déjà une demande."}
    threading.Thread(
        target=handle_wake,
        kwargs={"reserved": True},
        daemon=True,
        name="ava-manual-listen",
    ).start()
    return {"accepted": True}


# --- the menu bar gestures --------------------------------------------------

def _menu_toggle_panel() -> None:
    if _overlay is None:
        return
    MENU_BAR.set_panel_open(_overlay.toggle_panel())


def _menu_listen() -> None:
    # talking to ava from the menu implies watching her answer.
    if _overlay is not None and not _overlay.panel_visible():
        _overlay.set_panel_visible(True)
        MENU_BAR.set_panel_open(True)
    start_voice_interaction()


def _menu_settings() -> None:
    if _overlay is not None:
        _overlay.open_settings()
        MENU_BAR.set_panel_open(True)


def install_menu_bar() -> None:
    """Install the menu bar icon once the window is ready."""
    if MENU_BAR is None:
        return
    MENU_BAR.install({
        "toggle": _menu_toggle_panel,
        "menu": MENU_BAR.show_menu,
        "listen": _menu_listen,
        "hush": stop_speaking,
        "pause": toggle_listening_paused,
        "settings": _menu_settings,
        "quit": quit_ava,
    })
    MENU_BAR.set_state(ASSISTANT_STATE.snapshot.state.value)


# a far more reliable wake-up: instead of listening to ALL of french (where
# "bonjour ava" drowns in the vocabulary), we hold vosk to a small grammar — the
# wake phrases plus "[unk]", which absorbs everything else. the model only has to
# decide "wake or not", and it gets that wrong far less often. tunable with
# AVA_WAKE_GRAMMAR=0 to go back to the full vocabulary.
_WAKE_GRAMMAR = os.getenv("AVA_WAKE_GRAMMAR", "1").strip().lower() not in ("0", "false", "no", "off")


def _wake_grammar(phrases) -> str:
    # build the list of allowed phrases from the config, plus a few common
    # openers (ok/okay/hey/salut…), always followed by the name.
    entries: list[str] = []
    seen = set()

    def add(entry: str) -> None:
        entry = " ".join(entry.split())
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)

    prefixes = {"bonjour", "bonsoir", "salut", "coucou", "ok", "okay", "hey"}
    for phrase in phrases:
        toks = normalize_speech(str(phrase)).split()
        if not toks:
            continue
        add(" ".join(toks))
        name = toks[-1]
        if len(toks) >= 2:
            prefixes.add(toks[0])
        # keep the bare name wakeable and decline the usual openers.
        add(name)
        for pfx in list(prefixes):
            add(f"{pfx} {name}")
    add("[unk]")
    return json.dumps(entries)


def _strip_unk(text: str) -> str:
    # in grammar mode vosk spits out "[unk]" for anything that isn't a wake
    # phrase: strip it before parsing, or it pollutes the captured command.
    toks = [t for t in text.split() if t not in ("[unk]", "unk")]
    return " ".join(toks)


def _build_wake_recognizer(KaldiRecognizer, model, phrases):
    if _WAKE_GRAMMAR:
        try:
            rec = KaldiRecognizer(model, WAKE_SR, _wake_grammar(phrases))
            if DEBUG:
                print(f"[ava] reveil en grammaire restreinte : {_wake_grammar(phrases)}")
            return rec
        except Exception as exc:  # noqa: BLE001
            print(f"[ava] grammaire de reveil indispo ({exc}) -> vocabulaire complet")
    return KaldiRecognizer(model, WAKE_SR)


def assistant_loop() -> None:
    if not WAKE_ENABLED:
        print("[ava] modele vocal absent -> commandes vocales desactivees")
        return
    try:
        from vosk import KaldiRecognizer, SetLogLevel
    except Exception:
        print("[ava] vosk absent -> commandes vocales desactivees")
        return
    SetLogLevel(-1)  # on coupe les logs bavards de vosk
    model = get_vosk_model()
    if model is None:
        return
    rec = _build_wake_recognizer(KaldiRecognizer, model, WAKE_PHRASES)
    partial_gate = PartialWakeGate()
    _last_partial_dbg: dict = {}       # pour ne pas spammer le meme partiel
    print('[ava] "bonjour ava" = grand reveil | "ava" = commande vocale')
    was_flow = False
    while True:
        # while ava is listening or acting we do NOT drain the queue:
        # _record_utterance needs ALL of the command's audio. reading here at the
        # same time splits the audio between the two -> an empty or garbled
        # command.
        if _flow_active.is_set():
            was_flow = True
            time.sleep(0.05)
            continue
        if was_flow:
            rec.Reset()
            partial_gate.reset()
            _drain_wake_queue()      # on jette ce qui s'est accumule pendant le flow
            was_flow = False
        try:
            data = _wake_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if rec.AcceptWaveform(data):
            raw = json.loads(rec.Result()).get("text", "")
            txt = _strip_unk(raw)
            if DEBUG and raw.strip():
                print(f"[vosk final]   '{raw}'")
            wake = extract_wake(txt, WAKE_PHRASES)
            partial_gate.reset()
        else:
            raw = json.loads(rec.PartialResult()).get("partial", "")
            txt = _strip_unk(raw)
            if DEBUG and raw.strip() and raw != _last_partial_dbg.get("t"):
                _last_partial_dbg["t"] = raw
                print(f"[vosk partiel] '{raw}'")
            wake = partial_gate.feed(txt, WAKE_PHRASES)
        if wake.detected:
            if DEBUG:
                print(f"[ava] entendu : '{txt}'")
            rec.Reset()
            partial_gate.reset()
            try:
                # "bonjour ava" = the big morning wake-up (like the double clap).
                # "ok ava" (and the rest) = command mode.
                # only a real "bonjour" starts the full ritual (music + apps +
                # a 45 s briefing). "salut ava" and "coucou ava" go to command
                # mode: setting those off by accident in the middle of a working
                # day was far too expensive.
                greeting = wake.phrase.split()[0] in ("bonjour", "bonsoir")
                if greeting and not wake.trailing_command:
                    trigger_welcome("bonjour ava")
                else:
                    handle_wake(wake.trailing_command)
            except Exception as exc:  # noqa: BLE001
                print(f"[ava] interaction interrompue : {exc}")
                _flow_active.clear()
                return_to_idle()
                rec.Reset()


# --- clap detection ---------------------------------------------------------

class ClapDetector:
    def __init__(self, warmup_s: float = 0.0):
        self.in_event = False
        self.waiting_for_quiet = False
        self.peak = 0.0
        self.peak_t = 0.0
        self.last_clap_t = 0.0
        self.last_clap_peak = 0.0
        # a short warm-up: give the ambient floor 3 s to calibrate before we
        # accept a clap (in case ava starts with the music already on)
        warmup = max(0.0, float(warmup_s))
        self.cooldown_until = time.monotonic() + warmup if warmup else 0.0
        # ambient noise (a slow moving average): when music plays the floor
        # rises on its own -> the drums stop clapping on your behalf.
        # in silence it comes back down -> your claps stay easy to land.
        self.ambient = 0.0

    def effective_floor(self) -> float:
        return max(MIN_RMS, self.ambient * 4.0)

    def register_clap(self, t: float, peak: float = 0.0) -> bool:
        # True if this is the second clap of a valid double clap
        if t < self.cooldown_until:
            return False
        gap = t - self.last_clap_t
        if self.last_clap_t and MIN_GAP_S <= gap <= MAX_GAP_S:
            # two hands clapping make two hits of similar amplitude; two
            # keystrokes don't. this guard cuts out the last family of false
            # positives that got past the floor.
            louder = max(peak, self.last_clap_peak)
            quieter = min(peak, self.last_clap_peak)
            if quieter > 0 and louder / quieter > CLAP_PEAK_RATIO:
                if DEBUG:
                    print(f"[clap] paire desequilibree ({quieter:.2f} vs "
                          f"{louder:.2f}) -> ignore")
                self.last_clap_t, self.last_clap_peak = t, peak
                return False
            self.last_clap_t = 0.0
            self.last_clap_peak = 0.0
            self.cooldown_until = t + FLOW_COOLDOWN_S
            return True
        # otherwise: this is (maybe) the first clap of a new pair
        self.last_clap_t = t
        self.last_clap_peak = peak
        return False

    def feed(self, t: float, rms: float) -> bool:
        double = False
        # learn the ambient noise away from the peaks (time constant ~2 s)
        if not self.in_event and rms < 0.2:
            self.ambient = 0.995 * self.ambient + 0.005 * rms
        floor = self.effective_floor()
        if self.waiting_for_quiet:
            if rms < floor * 0.7:
                self.waiting_for_quiet = False
            return False
        if not self.in_event:
            if rms > floor:
                self.in_event = True
                self.peak = rms
                self.peak_t = t
        else:
            if rms > self.peak:
                self.peak = rms
                self.peak_t = t
            elapsed = (t - self.peak_t) * 1000.0
            if rms < DECAY_RATIO * self.peak:
                # the level collapsed: a clap, if it happened fast enough
                if elapsed <= DECAY_MS and self.peak > floor:
                    if DEBUG:
                        print(f"[clap] pic={self.peak:.3f} chute en {elapsed:.0f}ms "
                              f"(<{DECAY_MS}ms) -> CLAP")
                    if t >= self.last_clap_t + REFRACTORY_S:
                        double = self.register_clap(t, self.peak)
                else:
                    if DEBUG:
                        print(f"[voix] pic={self.peak:.3f} chute en {elapsed:.0f}ms "
                              f"(trop lent) -> ignore")
                self.in_event = False
            elif elapsed > MAX_EVENT_MS:
                # stays loud too long: a voice, or music
                if DEBUG:
                    print(f"[voix] pic={self.peak:.3f} soutenu >{MAX_EVENT_MS}ms -> ignore")
                self.in_event = False
                self.waiting_for_quiet = True
        return double


def pick_input_device():
    want = os.getenv("AVA_INPUT_DEVICE", "").strip()
    if not want:
        return None
    try:
        return int(want)
    except ValueError:
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and want.lower() in dev["name"].lower():
                return idx
    return None


def run_audio() -> None:
    # open the mic and loop: clap detection plus feeding vosk and whisper.
    # runs in the background while the overlay holds the main thread.
    detector = ClapDetector(warmup_s=3.0)
    q: deque = deque(maxlen=2000)
    device = pick_input_device()
    state = {"rate": SAMPLE_RATE}

    def callback(indata, frames, time_info, status):
        if status and DEBUG:
            print(f"[audio] {status}")
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(np.square(mono))))
        # we keep stacking levels even while paused: the watchdog uses them to
        # tell whether the stream is alive. only recognition is switched off.
        q.append((time.monotonic(), rms))
        if not _listening_paused.is_set():
            push_wake_audio(mono, state["rate"])

    def open_stream():
        for rate in (SAMPLE_RATE, 44100):
            try:
                s = sd.InputStream(
                    samplerate=rate, blocksize=int(rate * BLOCK_MS / 1000),
                    channels=1, dtype="float32", callback=callback, device=device,
                )
                s.start()
                state["rate"] = rate
                if DEBUG:
                    print(f"[audio] micro ouvert a {rate} hz")
                return s
            except Exception as exc:  # noqa: BLE001
                if DEBUG:
                    print(f"[audio] {rate} hz refuse ({exc}), essai suivant")
        return None

    stream = open_stream()
    if stream is None:
        print("impossible d'ouvrir le micro. verifie l'autorisation micro dans "
              "reglages > confidentialite.")
        return

    # watchdog: if the coreaudio stream dies quietly (err -50 and friends) no
    # more blocks arrive -> reopen the mic automatically.
    last_audio = time.monotonic()
    last_level_push = 0.0
    level_was_active = False
    try:
        while True:
            if not q:
                time.sleep(0.002)
                if time.monotonic() - last_audio > 5.0:
                    print("[audio] flux muet depuis 5s -> reouverture du micro")
                    try:
                        stream.stop(); stream.close()
                    except Exception:
                        pass
                    time.sleep(0.5)
                    stream = open_stream()
                    last_audio = time.monotonic()
                    if stream is None:
                        time.sleep(3)   # micro indisponible, on reessaiera
                continue
            t, rms = q.popleft()
            last_audio = time.monotonic()
            if t - last_level_push >= 0.075:
                listening = ASSISTANT_STATE.snapshot.state == AvaState.LISTENING
                if listening:
                    # A gentle curve: a normal voice takes up most of 0..1,
                    # without making the orb shiver at plain background noise.
                    ui("level", max(0.0, min(1.0, (rms - 0.004) * 24.0)))
                    level_was_active = True
                elif level_was_active:
                    ui("level", 0.0)
                    level_was_active = False
                last_level_push = t
            if CLAP_ENABLED and not _listening_paused.is_set() and detector.feed(t, rms):
                trigger_welcome("double clap")
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        try:
            if stream is not None:
                stream.stop(); stream.close()
        except Exception:
            pass


def main() -> None:
    if not INSTANCE_LOCK.acquire():
        print("ava est deja lancee. ferme l'instance existante avant d'en ouvrir une autre.")
        return
    if SETTINGS.get("ui", {}).get("start_hidden", False):
        ASSISTANT_STATE.dormant()
    else:
        ASSISTANT_STATE.idle()
    print('ava est prete. ecris dans le mini-plugin, clique sur le micro, '
          'ou dis "bonjour ava". (ctrl+c pour arreter)')
    if DEBUG:
        print("[debug] active : niveaux et decisions affiches a chaque pic")
    # the local voice model takes ~20 s to load: we get on with it right away so
    # the first "bonjour ava" doesn't have to wait.
    print(f"[info] voix : moteur {voice_tts.engine_name()}")
    voice_tts.prewarm()
    # the morning briefing is prepared at startup so it can start instantly on
    # the first clap (calendar + weather + ai news + quote)
    print("[info] preparation du briefing du matin (agenda, meteo, actu ia, citation)...")
    threading.Thread(target=get_welcome, daemon=True).start()
    threading.Thread(target=keep_welcome_warm, daemon=True).start()

    # the "ok ava …" voice assistant, alongside the clap
    threading.Thread(target=assistant_loop, daemon=True).start()

    # `AVA_GREET=1` plays the morning ritual at launch. without it, the only way
    # to see it again was to wait for tomorrow (it only runs once a day) or to
    # say "bonjour ava" into the mic — impractical the moment you want to check a
    # change to the briefing.
    if os.getenv("AVA_GREET") == "1":
        def greet_at_launch() -> None:
            time.sleep(2.0)             # laisse l'overlay et la voix se poser
            trigger_welcome("lancement (AVA_GREET)")

        threading.Thread(target=greet_at_launch, daemon=True,
                         name="ava-greet-at-launch").start()
    # preload whisper in the background so the first command is quick
    if WAKE_ENABLED:
        threading.Thread(target=get_whisper, daemon=True).start()

    if SETTINGS.get("morning", {}).get("open_apps_on_start", False):
        threading.Thread(
            target=open_startup_apps,
            daemon=True,
            name="ava-startup-apps",
        ).start()

    if OVERLAY_ENABLED and _overlay is not None:
        # the overlay MUST run on the main thread (macos): the audio goes to the
        # background and we start the orb window here (blocking until it closes).
        _overlay.set_handlers(submit_text_command, start_voice_interaction,
                              install_menu_bar)
        if SETTINGS.get("ui", {}).get("startup_animation", True):
            _overlay.prepare_startup(build_startup_payload(fetch_news=False))

            def refresh_startup() -> None:
                ui("startup", build_startup_payload(fetch_news=True))

            threading.Thread(
                target=refresh_startup,
                daemon=True,
                name="ava-startup-news",
            ).start()
        threading.Thread(target=run_audio, daemon=True).start()
        try:
            _overlay.start(str(paths.OVERLAY_HTML))
            # the window closed (a gui bug, whatever): ava must NOT die with it —
            # listening carries on without an interface.
            print("[overlay] fenetre fermee -> ava continue sans interface")
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] indisponible ({exc}) -> mode sans interface")
            if DEBUG:
                import traceback
                traceback.print_exc()
        # either way: keep the process (and so the listening) alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        try:
            run_audio()
        finally:
            INSTANCE_LOCK.release()


if __name__ == "__main__":
    main()
