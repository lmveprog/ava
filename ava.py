#!/usr/bin/env python3
# ava - assistante vocale mac : "bonjour ava" / double clap -> reveil du bureau,
# "ok ava" -> commandes vocales.
# on ecoute le micro, on detecte un double clap, et on lance :
#   1. la musique sur spotify
#   2. la voix de bienvenue (elevenlabs, mise en cache pour partir instantanement)
#   3. les apps rangees en 4 quadrants a l'ecran
#
# lancer :  .venv/bin/python jarvis.py
# stopper : ctrl+c

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

from assistant_state import AssistantStateMachine, AvaState, InvalidTransition
from app_catalog import AppCatalog
from ava_config import STORE as CONFIG, clap_min_rms
from calendar_tools import MacCalendar
from computer_use import ComputerUseEngine, parse_computer_intent
from conversation import LocalConversationEngine
import google_calendar
from instance_lock import SingleInstanceLock
import net
import promethee
import ai_news
import quotes
from screen_vision import ScreenVision
import skills
import traces
from understanding import ROUTER as INTENT_ROUTER
import voice_tts
from wake_words import (PartialWakeGate, extract_wake, normalize_speech,
                        strip_wake_prefix, strip_wake_suffix)
from web_research import ResearchReply, WebResearch

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
SETTINGS = CONFIG.snapshot()

# --- reglages ---------------------------------------------------------------

# micro : les macbook echantillonnent a 48000 hz, pas 44100.
# un mauvais sample rate = cause numero 1 des claps non detectes.
SAMPLE_RATE = 48000
BLOCK_MS = 10                      # taille d'un bloc d'analyse (ms)

# detection du clap par la FORME du son, pas par le volume seul.
# une voix atteint le meme niveau qu'un clap (clap 0.42-0.66, voix ~0.54) :
# on ne peut pas les separer au volume. mais un clap s'effondre en <100 ms,
# une voix reste forte. donc quand un pic depasse le seuil on attend, et on
# verifie que le niveau est bien retombe.
MIN_RMS = CONFIG.clap_min_rms()    # derive du curseur de sensibilite (0..100)
DECAY_MS = 100                     # delai max pour que le son "s'effondre"
DECAY_RATIO = 0.35                 # doit retomber sous 35% du pic => c'est un clap
MAX_EVENT_MS = 300                 # si ca reste fort plus longtemps => voix/musique

# double clap : ecart entre les deux claps
MIN_GAP_S = SETTINGS["wake"]["clap"]["min_gap_ms"] / 1000
MAX_GAP_S = SETTINGS["wake"]["clap"]["max_gap_ms"] / 1000
CLAP_ENABLED = SETTINGS["wake"]["clap"]["enabled"]
REFRACTORY_S = 0.10                # petit temps mort apres un clap (echo)
# afplay met un instant a sortir le premier echantillon : on decale le
# transcript d'autant, sinon le texte parle avant la voix.
AFPLAY_LEAD_S = 0.22
CLAP_PEAK_RATIO = 2.5              # les deux coups d'une paire doivent se ressembler
FLOW_COOLDOWN_S = 8.0              # apres un declenchement complet, on se tait

# mode debug : affiche le niveau mesure et le seuil a chaque pic
DEBUG = os.getenv("AVA_DEBUG", os.getenv("JARVIS_DEBUG", "")).strip().lower() in (
    "1", "true", "yes", "on")

# musique spotify : une URI force un album/une playlist ; vide = lecture
# aleatoire dans le dernier contexte utilise (playlist, titres aimes, etc.).
SPOTIFY_URI = SETTINGS["morning"]["spotify_uri"]

# apps a ouvrir + leur quadrant : "tl" haut-gauche, "tr" haut-droite,
# "bl" bas-gauche, "br" bas-droite.
APPS = [(item["name"], item["position"], item.get("url", ""))
        for item in SETTINGS["morning"]["apps"]]

# le briefing du matin est construit a la volee (meteo + actu ia + citation),
# donc le texte change chaque jour. la ville pour la meteo :
CITY = SETTINGS["identity"]["city"]
USER_NAME = SETTINGS["identity"]["name"]
WAKE_PHRASES = tuple(SETTINGS["wake"]["phrases"])
SYSTEM_VOICE = SETTINGS["voice"]["system_fallback"]
COMPUTER_USE_ENABLED = SETTINGS["computer_use"]["enabled"]
SKILLS_ENABLED = SETTINGS["skills"]["enabled"]
CONTINUOUS_LISTENING = SETTINGS["conversation"]["continuous_listening"]
FOLLOWUP_TIMEOUT_S = SETTINGS["conversation"]["followup_timeout_seconds"]
MAX_CONTINUOUS_TURNS = SETTINGS["conversation"]["max_continuous_turns"]

# le fonds de citations vit dans `quotes.py` : elles sont attribuees, et la
# rotation evite ce qui vient d'etre dit.
# accents obligatoires : c'est lu par une synthese vocale, et "espere" ne se
# prononce pas du tout comme "espère".

def daily_quote() -> str:
    """La citation dite dans le briefing, auteur compris."""
    return quotes.of_the_day().spoken()


def startup_quote() -> str:
    """La citation affichee sur la scene de demarrage."""
    return quotes.of_the_day().spoken()


# --- meteo (open-meteo, sans cle) -----------------------------------------

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


# une famille de temps (glyphe) par code wmo, pour l'illustration de la scene.
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
    # meteo brute (voix ET scene visuelle), mise en cache 20 min pour ne pas
    # rappeler l'api a chaque rafraichissement de la scene.
    with _weather_lock:
        age = time.time() - _weather_cache["ts"]
        usable = (_weather_cache["info"] is not None
                  and _weather_cache["city"] == CITY)
        if usable and age < 1200:
            return dict(_weather_cache["info"])
        # hors ligne, une meteo d'il y a deux heures vaut mieux qu'un briefing
        # ampute : la temperature bouge de deux degres, la saison ne change pas.
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
    """La meteo en une phrase, sans repeter le meme chiffre.

    L'ancienne version disait « il fait actuellement 33 degres […] aujourd'hui
    jusqu'a 33 degres » : quand la temperature du moment est deja le maximum du
    jour, annoncer le maximum n'apprend rien et donne l'impression qu'ava
    meuble.
    """
    info = weather_info()
    if not info:
        return ""
    temp, tmax, tmin = info["temp"], info["tmax"], info["tmin"]
    opening = f"Côté ciel, {temp} degrés à {info['city']}, {info['desc']}"
    if temp >= tmax:
        return f"{opening}. On ne montera pas plus haut, et {tmin} degrés au plus bas."
    return f"{opening}. On ira jusqu'à {tmax} degrés, avec {tmin} au plus bas."


# --- actu ia (google news rss, sans cle) -----------------------------------

# l'actualite est figee pour toute la duree d'une session : le briefing du matin
# et la scene de demarrage doivent raconter la meme chose. `ai_news` gere la
# peremption sur le disque (3 h).
_AI_NEWS_LOCK = threading.Lock()
_AI_NEWS_RUNTIME: dict = {}


def ai_news_item() -> dict:
    """L'actualite ia du moment. Le detail vit dans `ai_news` (flux rss dates)."""
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
    """Un titre d'actualite prononcable par une voix francaise.

    La traduction passait par Ollama, qui ne tourne pas : les titres anglais
    arrivaient donc intacts dans la synthese vocale francaise. `ai_news` s'en
    charge desormais avec le meme petit modele que le routage d'intentions, et
    garde le resultat sur le disque.
    """
    return ai_news.translate_title(title)


def ai_news_sentence() -> str:
    item = ai_news_item()
    if not item:
        # mieux vaut sauter la rubrique que meubler : une actualite absente ne
        # merite pas qu'on en parle pendant le briefing.
        return ""
    return ai_news.sentence(item)


# --- agenda du jour (calendar.app, donc google agenda s'il est synchronise) --

CALENDAR = MacCalendar()
GOOGLE_CALENDAR = google_calendar.CALENDAR

WEEKDAYS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS_FR = ("", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre")


def spoken_date(day: datetime.date | None = None) -> str:
    """« samedi 8 août », tel qu'on le dit a voix haute."""
    day = day or datetime.date.today()
    number = "premier" if day.day == 1 else str(day.day)
    return f"{WEEKDAYS_FR[day.weekday()]} {number} {MONTHS_FR[day.month]}"


def spoken_title(title: str) -> str:
    """Rend un titre d'agenda dicible.

    Les titres de Matheus sont pleins d'emojis (« 💻 Exo code #1 ») : la voix
    de synthese les prononce ou s'etrangle dessus. On enleve aussi le dieze,
    qui se lit « croisillon » au lieu de « numero ».
    """
    value = str(title or "").strip()
    # emojis, pictogrammes, drapeaux, variantes et jointures
    value = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
        "\U00002190-\U000021FF\U00002B00-\U00002BFF️‍•]",
        " ", value)
    value = re.sub(r"#\s*(\d+)", r"numéro \1", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -–—:;,")
    return value or "Sans titre"


def _spoken_hour(moment: datetime.datetime) -> str:
    # "14h30" se lit mal : on prefere "14 heures 30", et "midi pile" reste "12 heures".
    if moment.minute == 0:
        return f"{moment.hour} heures"
    return f"{moment.hour} heures {moment.minute:02d}"


def agenda_events(day_offset: int = 0):
    """Les rendez-vous d'un jour, quelle que soit la source.

    Google Agenda gagne des qu'il est connecte (c'est celui que Matheus tient
    vraiment a jour) ; Calendar.app reste le repli hors-ligne. Les deux
    renvoient des objets aux memes attributs, donc la suite s'en moque.
    """
    if GOOGLE_CALENDAR.connected():
        try:
            return GOOGLE_CALENDAR.events_for_day(day_offset), "google"
        except Exception as exc:  # noqa: BLE001 - on retombe sur Calendar.app
            if DEBUG:
                print(f"[agenda] google indisponible : {exc}")
    return CALENDAR.events_for_day(day_offset), "calendar"


def calendar_sentence(limit: int = 3) -> str:
    """L'agenda du jour, en tete du briefing. Silencieuse si l'agenda n'est pas
    lisible : un briefing du matin n'est pas le moment de rappeler une
    autorisation.

    On annonce **le compte d'abord**, puis les trois prochains rendez-vous, puis
    le reste en nombre. Enumerer les dix rendez-vous d'une journee chargee
    poussait le briefing au-dela de la minute, et personne ne retient le
    dixieme ; le compte, lui, dit tout de suite a quoi ressemble la journee.
    Les rendez-vous deja passes ne sont jamais cites — a 14 h, ceux du matin
    n'apprennent plus rien.
    """
    try:
        events, _source = agenda_events(0)
    except Exception:  # noqa: BLE001 - agenda indisponible, on n'en parle pas
        return ""
    now = datetime.datetime.now()
    # ce qui est deja passe n'interesse plus personne a 9 h du matin.
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
    # chaque rendez-vous fait sa propre phrase : enchaines par des points-virgules
    # ils sortaient d'une traite, sans respiration pour les separer a l'oreille.
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
    # meteo (illustration) + briefing ecrit ("ce qu'ava dit") seulement au
    # passage enrichi : le 1er passage reste instantane (etat "loading").
    weather = weather_info() if fetch_news else None
    # `briefing` peut etre impose par l'appelant : le reveil a deja fabrique ce
    # texte-la ET l'audio correspondant. Le recalculer ici donnait un transcript
    # different de ce qu'Ava prononce (l'agenda et l'heure ont bouge entre-temps),
    # et refaisait pour rien les appels agenda + base Promethee.
    if briefing is None:
        briefing = build_welcome_text() if fetch_news else ""
    # une seule pioche : afficher une citation et en prononcer une autre donnerait
    # l'impression que la scene et la voix ne parlent pas du meme jour.
    scene_quote = quotes.of_the_day() if fetch_news else quotes.Quote("", "")
    return {
        "name": USER_NAME,
        "city": CITY,
        "date": f"{weekdays[now.weekday()]} {now.day} {months[now.month]}",
        # `ai_news` rend deja le titre traduit : le repasser dans la traduction
        # ne ferait que le renvoyer tel quel, en payant un aller-retour reseau.
        "news": news.get("title", "") if news else "Recherche d'une actualité IA récente…",
        "source": news.get("source", "Vérification en cours"),
        "source_url": news.get("url", ""),
        "published": news.get("published", ""),
        # « hier », « ce matin » : la fraicheur se lit aussi a l'ecran.
        "freshness": news.get("freshness", "") if news else "",
        # le salut suit l'heure, comme le briefing parle : « BONJOUR » en gros a
        # 22 h, a cote d'un texte qui dit « Bonsoir », ca se voyait tout de suite.
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
    """Le briefing, dans l'ordre ou il sert.

    L'ordre n'est pas decoratif. On ouvre sur le repere (qui parle, quand), on
    donne **tout de suite** ce qui engage la journee — l'agenda — puis seulement
    le decor, et on ferme sur l'action. Avant, les cinq blocs arrivaient a plat,
    du meme ton, colles bout a bout : la seule information vraiment utile
    (« tu as un rendez-vous a 20 h ») etait noyee entre la meteo et une citation
    anonyme, et ca s'ouvrait sur « c'est Ava, j'espere que tu vas bien ! »,
    repete a l'identique tous les matins.

    Chaque bloc porte maintenant sa propre entree en matiere, pour qu'on entende
    qu'on change de sujet sans avoir a le dire.
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

    # l'espace se monte pendant qu'elle parle : au moment ou elle arrive ici,
    # les fenetres sont deja en place. Annoncer « je t'ouvre tes applications »
    # apres coup sonnerait faux.
    if promethee.active_session():
        parts.append("Ta session Prométhée tourne déjà, ton espace est en place. "
                     "Bon travail !")
    else:
        parts.append("Je te lance une session Prométhée, ton espace est en place. "
                     "Bon travail !")
    return " ".join(parts)

CACHE_DIR = HERE / ".cache" / "ava_welcome"
INSTANCE_LOCK = SingleInstanceLock(HERE / ".cache" / "ava.lock")

# --- overlay visuel (fenetre orbe en haut a droite) -------------------------
OVERLAY_ENABLED = os.getenv("AVA_OVERLAY", os.getenv("JARVIS_OVERLAY", "1")).strip().lower() not in (
    "0", "false", "no", "off")
try:
    import overlay as _overlay
except Exception:                       # pywebview absent ?
    _overlay = None
    OVERLAY_ENABLED = False

try:
    from menubar import MENU_BAR, quit_ava
except Exception:                       # hors macos : pas de barre de menus
    MENU_BAR = None
    quit_ava = None


def ui(fn: str, *args) -> None:
    # relaie un etat vers l'overlay s'il est actif, sinon ne fait rien
    if OVERLAY_ENABLED and _overlay is not None:
        try:
            getattr(_overlay, fn)(*args)
        except Exception:
            pass


def _render_assistant_state(snapshot) -> None:
    # l'etat fonctionnel et l'etat visuel restent toujours synchronises.
    if snapshot.state == AvaState.DORMANT:
        ui("dormant")
    elif snapshot.state == AvaState.IDLE:
        ui("idle")
    elif snapshot.state != AvaState.BOOTING:
        ui("set_state", snapshot.state.value, snapshot.label or None)
    # l'icone de la barre de menus est le seul retour visuel qui subsiste quand
    # le panneau est ferme : elle doit suivre le meme etat.
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
        # un flux qui se termine doit toujours pouvoir remettre ava en veille.
        if state == AvaState.DORMANT or state == AvaState.DORMANT.value:
            ASSISTANT_STATE.dormant()
        elif DEBUG:
            print(f"[etat] transition ignoree : {ASSISTANT_STATE.snapshot.state} -> {state}")


def return_to_idle() -> None:
    """Revient au mini-plugin, ou le cache si l'utilisateur l'a demande."""
    if SETTINGS.get("ui", {}).get("start_hidden", False):
        ASSISTANT_STATE.dormant()
    else:
        ASSISTANT_STATE.idle()


# --- actions du reveil ------------------------------------------------------

def _osascript(script: str) -> None:
    # petit helper : lance un bout d'applescript, sans planter si ca rate
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[applescript] souci : {exc}")


def play_spotify() -> None:
    print("  -> spotify")
    if SPOTIFY_URI:
        # Une cible explicite reste prioritaire. La configuration attend une URI
        # Spotify (spotify:playlist:...), pas un lien web.
        _osascript(f'tell application "Spotify" to play track "{SPOTIFY_URI}"')
        return
    # Sans cible, Spotify reprend son dernier contexte d'ecoute. On active le
    # shuffle puis on avance pour obtenir un morceau different au rituel.
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
    # taille de l'ecran en "points" (pas les pixels retina)
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
    # une petite fenetre calee en bas a droite, avec une marge : de quoi voir ce
    # qui joue sans manger un quart de l'ecran.
    small_w, small_h, margin = 380, 220, 24
    return {
        "tl": (left,  top, w, h),
        "tr": (right, top, w, h),
        "bl": (left,  mid, w, h),
        "br": (right, mid, w, h),
        "small": (x2 - small_w - margin, y2 - small_h - margin, small_w, small_h),
    }[where]


def open_and_place(app: str, where: str, sb, url: str = "") -> None:
    # ouvre l'app puis la range dans son coin (via system events).
    # macos peut demander l'autorisation "accessibilite" la 1re fois.
    print(f"  -> {app} ({where}){' ' + url if url else ''}")
    launch_name = app
    resolved = APP_CATALOG.resolve(app) if "APP_CATALOG" in globals() else None
    if url:
        # `open -a <app> <url>` lance l'app *sur* la page voulue, en une fois.
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
    # ⚠️ attendre que le *process* existe ne suffit pas : il existe avant
    # d'avoir peint sa fenetre, et un navigateur ouvert sur une adresse en
    # fabrique une nouvelle apres coup. On posait donc la geometrie sur une
    # fenetre qui n'etait pas encore la — resultat, Dia s'etalait sur la moitie
    # de l'ecran au lieu de tenir dans son quadrant. On attend la fenetre, puis
    # on **verifie** que la taille a bien pris, et on recommence sinon.
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
    """Monte l'espace de travail **pendant** qu'Ava parle.

    Avant, les fenetres s'ouvraient une fois le briefing fini : le briefing se
    deroulait devant un bureau vide, puis, dans le silence, les applications
    apparaissaient. On regardait deux scenes sans rapport. Menees ensemble, on
    voit ce qu'elle annonce se faire pendant qu'elle le dit.
    """
    job = threading.Thread(target=open_startup_apps, daemon=True,
                           name="ava-espace-de-travail")
    job.start()
    return job


def close_startup_apps() -> list[str]:
    """Referme ce que le rituel du matin a ouvert. Rend les noms fermes.

    Le pendant manquait : ava dispose les quatre applications en quadrants au
    reveil, et « ferme tout » partait en recherche web. On ne ferme que la liste
    configuree — jamais tout ce qui tourne, il y a du travail dans les autres.
    """
    closed: list[str] = []
    for app, _where, _url in APPS:
        _osascript(f'tell application "{app}" to quit')
        closed.append(app)
    return closed


def ensure_welcome_audio(text: str, mood: str = "") -> Path | None:
    """L'audio d'une phrase, mis en cache. None = il faudra passer par `say`.

    Le choix du moteur (mistral, chatterbox local, elevenlabs, voix systeme) vit
    dans voice_tts : ava ne sait plus qui parle, elle sait juste ou est le
    fichier. `mood` colore le timbre quand le moteur sait le faire.
    """
    return voice_tts.synthesize(text, mood)


# briefing prepare a l'avance pour partir vite au clap.
# on le rafraichit le matin (changement de jour) ou s'il a plus de 6h,
# pas a chaque clap -> voix instantanee et quota preserve.
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


# garde partagee : le clap ET le mot-cle "ok ava" passent par ici, pour ne pas
# declencher deux reveils coup sur coup. _flow_active bloque tout nouveau
# declenchement tant que le reveil est en cours (sinon ava, qui dit son propre
# nom pendant le briefing, se re-declencherait en boucle via le micro).
_last_trigger = {"ts": 0.0}
_trigger_lock = threading.Lock()
_flow_active = threading.Event()


# ecoute en pause : le micro reste ouvert (le chien de garde continue de
# surveiller le flux, et rouvrir coreaudio coute cher) mais plus rien ne peut
# reveiller ava. c'est la reponse au « ava me derange » : un clic dans la barre
# de menus et elle se tait, sans qu'on ait a la tuer.
_listening_paused = threading.Event()
# AVA_PAUSED=1 : ava demarre sans ecouter (pratique pour la regler, ou pour
# travailler a cote sans qu'elle se reveille).
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
    # on jette l'audio accumule pendant le reveil (le micro a entendu ava
    # parler dans les enceintes) pour ne pas re-declencher juste apres.
    while True:
        try:
            _wake_q.get_nowait()
        except queue.Empty:
            break


def _audio_ms(path) -> int:
    # duree d'un audio via afinfo (macos), pour caler le transcript sur la voix.
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
    # estimation quand on n'a pas de fichier (voix "say") : ~165 mots/minute.
    words = len(str(text or "").split())
    return max(3200, round(words / 165 * 60 * 1000))


def start_promethee_session() -> None:
    # le grand reveil ouvre aussi la journee de travail : une session de focus
    # dans promethee, sans que matheus ait a cliquer.
    try:
        reply = promethee.start_session()
    except Exception as exc:  # noqa: BLE001 - jamais bloquer le reveil pour ca
        print(f"  -> promethee : {exc}")
        return
    print(f"  -> promethee : {reply.text}")


WELCOME_WARM_INTERVAL_S = 900


def keep_welcome_warm(interval_s: int = WELCOME_WARM_INTERVAL_S) -> None:
    """Garde le briefing du jour pret a partir, texte ET audio.

    Sans ca, le premier « bonjour ava » qui suit un changement de date ou une
    modification de l'agenda tombe sur un cache vide : la scene de demarrage
    reste figee ~1 minute, le temps que la voix locale synthetise 45 s de
    parole. Ici on refait le texte regulierement ; si rien n'a bouge, l'audio
    est deja en cache et l'operation ne coute rien.
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
        # Le rituel reprend la grande scene du lancement. Elle reste au centre
        # jusqu'a la fin du briefing et de l'ouverture des applications.
        startup_payload = build_startup_payload(fetch_news=False)
        startup_payload["auto_finish"] = False
        ui("startup", startup_payload)
        play_spotify()
        # Promethee met quelques secondes a s'ouvrir et a peindre son bouton :
        # on lance en parallele du briefing pour que la session soit deja
        # partie quand ava finit de parler.
        promethee_job = threading.Thread(target=start_promethee_session, daemon=True)
        promethee_job.start()
        start_workspace()

        # Garde la scene visible pendant la preparation du briefing ; au premier
        # lancement le reseau ou ElevenLabs peuvent prendre quelques secondes.
        text, path = get_welcome()
        startup_payload = build_startup_payload(fetch_news=True, briefing=text)
        startup_payload["auto_finish"] = False
        ui("startup", startup_payload)
        if path:
            print("  -> voix")
            set_assistant_state(AvaState.SPEAKING, "Ton briefing du matin")
            # transcript cale sur la duree reelle de la voix (karaoke) : on
            # demarre afplay AVANT le texte, puis on laisse au lecteur le temps
            # d'attaquer. Avant, le texte partait le premier et prenait une
            # demi-seconde d'avance sur la voix pour tout le briefing.
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
        # le cooldown ne demarre qu'une fois le reveil fini
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        return_to_idle()


# le grand rituel n'a de sens qu'une fois : il ouvre la musique, range quatre
# applications a l'ecran et parle pendant 45 secondes. le rejouer parce qu'on a
# dit « bonjour ava » a 18 h — ou parce qu'un bruit a ete pris pour un double
# clap — c'est exactement ce qui rendait ava insupportable en pleine journee.
_ritual = {"day": None}


def ritual_done_today(day: str | None = None) -> bool:
    return _ritual["day"] == (day or datetime.date.today().isoformat())


def mark_ritual_done(day: str | None = None) -> None:
    _ritual["day"] = day or datetime.date.today().isoformat()


def run_short_greeting() -> None:
    """Le bonjour d'apres : ava salue et ecoute, sans rejouer tout le rituel."""
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
        # reserve le flux AVANT de lancer le thread : sans cela, le mot-cle peut
        # gagner la course et demarrer une seconde interaction en parallele.
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


# --- mot-cle vocal "ok ava" (vosk, hors-ligne) -----------------------------
# sur mac, ouvrir deux flux micro en meme temps est instable (erreurs coreaudio).
# on garde donc UN seul flux (celui du clap, a 48000 hz) et on lui repique
# l'audio : on le reechantillonne a 16000 hz pour vosk et on le pousse dans
# cette file. le clap et "ok ava" partagent ainsi le meme micro.
WAKE_MODEL_DIR = HERE / "models" / "vosk-model-small-fr-0.22"
WAKE_SR = 16000
WAKE_ENABLED = WAKE_MODEL_DIR.exists()
_wake_q: queue.Queue = queue.Queue(maxsize=800)
_command_q: queue.Queue = queue.Queue(maxsize=1800)
_capture_audio = threading.Event()


# gain applique a la voix avant vosk : sans lui il faut "gueuler" pour reveiller.
# soft-limiter tanh -> on booste la voix normale/douce sans saturer les pics.
# reglable via AVA_WAKE_GAIN (0 ou 1 = desactive).
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
        # tanh : ~lineaire (x*gain) sur la voix douce, sature en douceur sur les pics
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
    # reechantillonne vraiment vers 16000 hz, y compris au fallback 44100 hz.
    # le callback micro ne doit jamais bloquer : si vosk prend du retard, on
    # abandonne le bloc le plus ancien au lieu de faire grossir la ram.
    pcm = resample_for_wake(mono, rate)
    if not pcm:
        return
    if WAKE_ENABLED:
        _put_drop_oldest(_wake_q, pcm)
    if _capture_audio.is_set():
        _put_drop_oldest(_command_q, pcm)


# --- la voix d'ava (reponses courtes) --------------------------------------

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

# le lecteur en cours, pour pouvoir l'arreter net. c'est l'idee de fond des
# pipelines temps reel (pipecat propage une « interruption » qui vide les
# tampons de chaque etage) ramenee a ce qu'ava a besoin : tant qu'on attendait
# `player.wait()`, un briefing de trente secondes etait **insecable**, et la
# seule facon de la faire taire etait de la tuer.
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


def speak(text: str, state: str = "speaking") -> None:
    # met l'orbe dans l'etat voulu + affiche le texte, puis parle.
    # la voix est fabriquee en local et mise en cache par texte ; si le modele
    # ne se charge pas, on bascule sur la voix systeme macos "say".
    global _player
    set_assistant_state(state, text)
    # on mesure la fabrication de la voix, pas la lecture : c'est l'attente
    # avant qu'ava ouvre la bouche qui se ressent.
    with traces.span("voix", route=voice_tts.engine_name()) as trace:
        cached = voice_tts.is_cached(text)
        path = ensure_welcome_audio(text)
        # une phrase deja dite sort du disque : ni reseau, ni attente.
        trace["route"] = "cache" if cached else voice_tts.engine_name()
        trace["network"] = not cached and voice_tts.engine_name() == "mistral"
    if path:
        # meme calage que le briefing : la voix demarre, puis la bulle se
        # remplit au rythme mesure sur l'audio.
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
    """Un minuteur qui ne retient pas Ava au moment de quitter.

    ⚠️ `threading.Timer` fabrique un thread **non-daemon** : un minuteur de
    trente minutes empechait donc le process de se terminer pendant trente
    minutes. Comme le launchagent ne relance qu'apres une sortie effective,
    « quitter » depuis la barre de menus paraissait simplement ne rien faire.
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


# --- comprendre et executer une commande -----------------------------------

def _norm(s: str) -> str:
    # minuscules + sans accents, pour comparer facilement
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


# quelques raccourcis parle -> vraie app (le reste est trouve automatiquement)
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


# petits nombres en toutes lettres (whisper ecrit souvent les chiffres, mais au cas ou)
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


# en francais on compte le quart d'heure avant la minute. sans ces trois cas,
# « rappelle-moi dans un quart d'heure » comprenait « un » puis « heure » et
# lancait un minuteur d'UNE HEURE.
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
    # « rendez-vous », « rendez vous », « apres-demain »... : les tirets sont un
    # hasard de transcription, on les efface avant de chercher des mots-cles.
    return re.sub(r"[-']", " ", str(command or ""))


# on ne dit presque jamais le mot « agenda » pour demander son agenda : on
# demande « qu'est-ce que j'ai aujourd'hui », « c'est quoi mon programme
# demain », « a quelle heure est mon rendez-vous ». exiger le mot envoyait ces
# trois formulations — les plus courantes — en recherche web.
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


# --- ecrire dans l'agenda ----------------------------------------------------

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
    """Sort le libelle du rendez-vous d'une phrase dictee."""
    value = str(raw or "").strip()
    # « ... appelé X », « ... pour X », « ... : X » : ce qui suit est le titre.
    for marker in (" intitule ", " intitulé ", " appele ", " appelé ", " nomme ", " nommé "):
        if marker in value.lower():
            return value[value.lower().index(marker) + len(marker):].strip(" .")[:120] or "Rendez-vous"
    # sinon on retire les mots de service et les indications de temps.
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
    # Le panneau d'Ava ne doit pas masquer le message que l'utilisateur montre.
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
    # hors ligne, Ava repondait « je n'ai pas trouve de source suffisamment
    # claire » : elle avait l'air d'avoir cherche et echoue, alors qu'elle
    # n'etait meme pas sortie du mac. dire la vraie raison evite de repeter la
    # question dix fois en croyant mal la poser.
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
        # la recherche web est le dernier recours du routage : si elle explose,
        # c'est toute la commande qui mourait.
        if DEBUG:
            print(f"[recherche] echec : {exc}")
        speak("Je n'arrive pas à chercher sur le web pour le moment.")
        return
    _present_research(reply)


# une duree qui contient le mot « heure » n'est pas une question sur l'heure.
_DURATION_HINTS = ("quart d heure", "quart d'heure", "demi heure", "demi-heure",
                   "minuteur", "chrono", "timer", "rappelle", "reveille",
                   "previens", "pendant")
_APPOINTMENT_HINTS = ("rendez vous", "rendez-vous", "rdv", "reunion", "reunions",
                      "cours", "train", "avion", "match")


def _is_time_question(command: str) -> bool:
    """« Quelle heure il est ? » — et surtout rien d'autre.

    Tester `"heure" in c` attrapait tout ce qui contient le mot : « rappelle-moi
    dans un quart d'heure » se faisait repondre l'heure qu'il est au lieu de
    lancer un minuteur, et « a quelle heure est mon rendez-vous » ne regardait
    jamais l'agenda.
    """
    value = str(command or "")
    if "heure" not in value:
        return False
    if any(hint in value for hint in _DURATION_HINTS):
        return False
    if any(hint in value for hint in _APPOINTMENT_HINTS):
        return False
    # « dans deux heures », « dans une heure » : c'est un delai, pas une question.
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


# « merci » est deja traite plus haut dans le routage : on ne le redouble pas.
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
    """Point d'entree unique, et filet de securite : quoi qu'il arrive en
    dessous, Ava doit repondre quelque chose plutot que mourir en silence.

    C'est aussi le point de mesure : on note la route empruntee et le temps mis,
    jamais ce qui a ete dit (voir l'entete de `traces`).
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


# la route empruntee se decide au fond du routage : on la note au passage plutot
# que de faire remonter une valeur de retour a travers vingt branches.
_ROUTE = threading.local()

# le nom dit tout seul apres le reveil : c'est un appel, pas une commande.
_CALLED_BY_NAME = frozenset({"ava", "hey ava", "ok ava", "eva", "avah"})
# ce que la transcription rend quand on hesite : ni commande, ni question.
_FILLERS = frozenset({"euh", "heu", "hein", "hm", "hmm", "mmh", "bah", "ben",
                      "voila", "donc", "alors"})
# une phrase coupee net : on redemande le complement au lieu de chercher le
# verbe tout seul sur internet.
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
    # « salut ava », « merci ava » : on appelle son assistante par son nom en fin
    # de phrase autant qu'en tete. sans ca, ces tournures ne ressemblaient a
    # aucune commande connue et finissaient en recherche web.
    if c in _CALLED_BY_NAME:
        speak("Oui ?")
        return
    c = _norm(strip_wake_suffix(c)) or c
    # hesitations : ne rien chercher sur le web parce que Matheus a dit « euh ».
    if c in _FILLERS:
        speak("Je t'écoute.")
        return

    # Les capacités qui combinent plusieurs outils passent avant les verbes
    # generiques : « ouvre mon agenda et dis-moi… » ne doit pas etre pris pour
    # le nom d'une application, et « quel est ce probleme » declenche la vision.
    # ecrire avant lire : « ajoute un rdv demain » contient aussi « demain ».
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

    # computer use : seules les actions comprises par le parseur deterministe
    # sont executees. envoyer, coller, fermer et cliquer sur un bouton sensible
    # passent par une confirmation vocale separee.
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

    # refermer ce que le rituel a ouvert : le pendant de open_startup_apps.
    if c in ("ferme tout", "ferme moi tout", "quitte tout", "range tout",
             "ferme mes applications", "ferme les applications"):
        closed = close_startup_apps()
        speak("Je ferme tout." if closed else "Il n'y a rien à fermer.")
        return

    # ouvrir une app / un site ("votre"/"offre" = voxtral qui entend mal "ouvre")
    for verb in ("ouvre moi ", "ouvre-moi ", "ouvre ", "ouvrir ", "lance ",
                 "lancer ", "demarre ", "affiche ", "votre ", "offre ",
                 "montre ", "va sur "):
        if c.startswith(verb):
            return _open_target(c.split(verb, 1)[1].strip())

    # verbe dit sans complement : demander quoi, plutot que chercher le verbe
    # tout seul sur le web (« ouvre » renvoyait des resultats sur le mot ouvre).
    if c in _TRUNCATED:
        speak(_TRUNCATED[c])
        return

    # mails
    if any(w in c for w in ("mail", "mails", "gmail", "courriel", "boite mail")):
        open_url("https://mail.google.com/mail/u/0/")
        speak("J'ouvre ta boite mail.")
        return

    # recherche interne : Ava lit les resultats et montre les sources, sans
    # sortir l'utilisateur vers un navigateur sauf clic volontaire.
    search_match = re.match(
        r"^\s*(?:recherche(?:-moi| moi)?|cherche(?:-moi| moi)?|google)\s+(.+)$",
        raw, re.IGNORECASE,
    )
    if search_match:
        _research(search_match.group(1).strip())
        return

    # heure qu'il est
    if _is_time_question(c):
        now = datetime.datetime.now()
        speak(f"Il est {now.hour} heures {now.minute:02d}.")
        return

    # date du jour
    if "quel jour" in c or "quelle date" in c:
        speak("Nous sommes le " + datetime.date.today().strftime("%d/%m/%Y") + ".")
        return

    # meteo
    if "meteo" in c or "quel temps" in c or "il fait combien" in c:
        speak(weather_sentence() or "Météo indisponible pour le moment.")
        return

    # actu ia. « quoi de neuf en IA » ne contenait aucun des mots attendus et
    # partait en recherche web ; « ia » se cherche en mot entier, sinon il
    # s'attrape dans « diaporama », « italie », « bavarois »...
    if ("intelligence artificielle" in c or "actu" in c or "nouveaute" in c
            or ("quoi de neuf" in c and not _is_calendar_summary(c))
            or re.search(r"\b(?:ia|ai)\b", c)):
        speak(ai_news_sentence() or "Rien de neuf côté intelligence artificielle.")
        return

    # mots de transport dits tout seuls : « pause », « stop », « reprends ».
    # avant, ils tombaient dans le filet de la recherche web et Ava partait
    # chercher le mot « pause » sur internet.
    if c in ("pause", "stop", "silence", "chut", "coupe", "coupe la musique", "stop la musique"):
        _spotify("pause"); speak("Je coupe."); return
    if c in ("play", "reprends", "reprend", "continue", "relance"):
        _spotify("play"); speak("Je reprends."); return
    # « suivant » / « precedent » dits seuls : sans le mot « morceau », ils
    # tombaient dans le filet et Ava cherchait « precedent » sur le web.
    if c in ("suivant", "suivante", "next", "la suite", "passe", "zappe"):
        _spotify("next track"); speak("Morceau suivant."); return
    if c in ("precedent", "precedente", "previous", "retour", "reviens", "davant"):
        _spotify("previous track"); speak("Morceau précédent."); return

    # controle spotify : morceau suivant / precedent / reprendre
    if any(w in c for w in ("musique", "chanson", "morceau", "titre", "spotify", "son morceau")):
        if any(w in c for w in ("suivant", "suivante", "prochain", "prochaine", "next", "apres")):
            _spotify("next track"); speak("Morceau suivant."); return
        if any(w in c for w in ("precedent", "precedente", "reviens", "davant", "previous", "retour")):
            _spotify("previous track"); speak("Morceau précédent."); return
        if any(w in c for w in ("reprend", "continue", "relance", "remets", "remet")):
            _spotify("play"); speak("Je reprends la musique."); return

    # verrouiller l'ecran
    if "verrouille" in c or "verrouiller" in c or ("lock" in c and "ecran" in c):
        _osascript('tell application "System Events" to keystroke "q" using {control down, command down}')
        speak("Je verrouille l'écran."); return

    # capture d'ecran (avant la luminosite, sinon "ecran" est ambigu)
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

    # luminosite (via les touches systeme ; peut ne pas marcher sur tous les macs)
    if "luminosite" in c or "lumiere" in c or ("ecran" in c and any(
            w in c for w in ("clair", "sombre", "lumineux", "fonce"))):
        up = any(w in c for w in ("monte", "augmente", "plus", "clair", "lumineux"))
        code = 144 if up else 145
        for _ in range(4):
            _osascript(f'tell application "System Events" to key code {code}')
        speak("Voila." if up else "C'est fait."); return

    # minuteur
    if any(w in c for w in ("minuteur", "chrono", "timer")) or \
            ("dans" in c and any(w in c for w in ("rappelle", "reveille", "previens"))):
        secs, label = _parse_duration_s(c)
        if secs:
            start_timer(secs, "C'est l'heure ! Ton minuteur est terminé.")
            speak(f"Minuteur lancé pour {label}."); return
        speak("Pour combien de temps ?"); return

    # note rapide (ajoutee dans ~/Documents/ava-notes.md)
    if re.search(r"\bnote", c) or "prends note" in c or "rappelle moi de" in c or "ajoute une note" in c:
        m = re.search(r"(?:noter?|note[rz]?|prends? note|rappelle[- ]?moi de|ajoute une note)"
                      r"\s+(?:que\s+|de\s+|d'\s*)?(.*)", raw, re.I)
        content = (m.group(1).strip() if m else "").strip(" .")
        if content:
            p = Path.home() / "Documents" / "ava-notes.md"
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"- {stamp} — {content}\n")
            speak("C'est noté."); return
        speak("Qu'est-ce que je dois noter ?"); return

    # musique (spotify) : jouer ton morceau favori ou mettre en pause
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

    # discussion libre via LM Studio (ou tout serveur local compatible OpenAI).
    # rien n'est envoye hors du Mac par defaut.
    discussion_starters = (
        "discute avec moi de ", "parle moi de ", "explique moi ",
        "qu est ce que ", "qu'est ce que ", "que penses tu de ",
        "aide moi a reflechir a ",
    )
    for starter in discussion_starters:
        if c.startswith(starter):
            question = raw[len(starter):].strip() or raw
            answer = CONVERSATION.ask(question)
            if answer.available:
                speak(answer.text)
            else:
                speak("Je n'arrive à joindre aucun moteur de discussion pour le moment.")
            return

    # phrase sans verbe mais qui contient une app connue ("l'application discord",
    # "votre discord"...) : on ouvre l'app plutot que de chercher sur le web.
    # (place en dernier pour ne pas court-circuiter musique/mails/etc.)
    for token in c.replace("'", " ").split():
        if token in _APP_ALIASES:
            return _open_target(token)

    # politesses et mots de remplissage : repondre court, surtout ne pas partir
    # en recherche web parce que Matheus a dit « merci ».
    courtesy = _small_talk(c)
    if courtesy:
        speak(courtesy)
        return

    # dernier recours avant le filet : on demande a un petit modele ce que
    # Matheus voulait dire. tout ce qui arrive ici allait de toute facon partir
    # en recherche web, donc on ne ralentit aucune commande qui marchait deja.
    if _dispatch_understood(raw):
        return

    # Conversation par defaut. Si aucun moteur ne repond, Ava effectue une
    # recherche interne sourcee au lieu d'ouvrir un onglet opaque.
    answer = CONVERSATION.ask(raw)
    if answer.available:
        speak(answer.text)
    else:
        _research(raw)


def _dispatch_understood(raw: str) -> bool:
    """Execute l'intention devinee par le modele. False = on n'a rien compris.

    Chaque branche reutilise exactement les actions du routage par mots-cles :
    ce module decide *quoi* faire, jamais *comment*.
    """
    installed = skills.discover() if SKILLS_ENABLED else []
    cached = INTENT_ROUTER.knows(raw)
    result = INTENT_ROUTER.understand(raw, skills.catalogue(installed))
    if not result.usable:
        return False
    # une tournure deja apprise ne coute plus rien : ca se voit dans les traces.
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
        speak("J'ouvre ta boite mail.")
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
        answer = CONVERSATION.ask(target or raw)
        if answer.available:
            speak(answer.text)
            return True
        return False        # pas de moteur : on laisse la recherche web prendre

    return False


def _run_skill(name: str, raw: str, installed: list) -> bool:
    """Etapes « activation » et « execution » d'une competence.

    Deux facons pour une competence de repondre : un script qu'on lance et dont
    on lit la sortie, ou — s'il n'y en a pas — ses instructions passees au
    moteur de discussion. Dans les deux cas Ava dit ce qui remonte, elle
    n'invente rien.
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

    # pas de script : les instructions du SKILL.md servent de consigne.
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


# --- boucle assistant : "ok ava ..." + commande ----------------------------

# --- transcription de la commande avec whisper (local, precis en francais) --
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
    """VAD leger qui s'adapte au bruit de la piece avant chaque commande."""

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
        """Ingere un niveau et renvoie ``True`` quand la phrase est terminee."""
        level = max(0.0, float(rms))
        block_s = max(0.0, float(duration))
        self.elapsed += block_s
        if not self.started:
            threshold = self.start_threshold
            if level >= threshold:
                self.started = True
                self.voiced += block_s
                return False
            # Le plancher suit lentement la ventilation et la musique, sans se
            # laisser aspirer par un debut de mot bref.
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
    """Capture une phrase sur une file dediee et publie un brouillon en direct."""
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


# --- voxtral (api mistral) : transcription principale si une cle est la ------
# meme montage que le projet hackathon-mistral-vibe : wav multipart vers
# /v1/audio/transcriptions, modele voxtral-mini-latest, et fallback local
# (whisper) si pas de cle / pas de reseau / erreur. rien ne casse jamais.
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
    # renvoie le texte, ou None si indisponible (le fallback whisper prend le relais)
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
    # meme si le reveil courant ne se fait que sur "ava" seul, on nettoie un
    # eventuel "ok ava"/"bonjour ava" dit en tete de commande (une seule traite).
    stripped = strip_wake_prefix(value)
    if stripped is not None:
        return stripped
    return value


# whisper large-v3-turbo compile pour le gpu du mac. mesures sur ce mac, meme
# extrait de 11 s de francais :
#
#     faster-whisper small (cpu)   1,85 s   « Bonjour Mathieu, c'est **Hava** »
#     voxtral (reseau)             ~1-2 s + la latence, et une cle qui s'epuise
#     whisper-large-v3-turbo (mlx) **0,32 s**  « Mathieu, c'est **Ava** »
#
# le local n'est donc plus le repli degrade : il est a la fois le plus rapide et
# le plus juste. une commande de 3,5 s se transcrit en 0,23 s, sans reseau.
MLX_WHISPER_MODEL = os.getenv("AVA_STT_MODEL", "mlx-community/whisper-large-v3-turbo").strip()
_mlx_whisper_ok = True


def mlx_transcribe(pcm: bytes) -> str | None:
    """Transcription locale sur le gpu. None si mlx n'est pas disponible."""
    global _mlx_whisper_ok
    if not _mlx_whisper_ok or not pcm:
        return None
    try:
        import mlx_whisper
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=MLX_WHISPER_MODEL, language="fr",
            condition_on_previous_text=False, temperature=0.0,
            # oriente le decodage vers du vocabulaire de commande : sans ca,
            # « ava » ressort en « hava », « à va », « ava ? »...
            initial_prompt="Commande vocale en français adressée à Ava, "
                           "assistante sur Mac : ouvre une application, mets la "
                           "musique, quelle heure est-il, la météo, mon agenda.",
        )
        text = str(result.get("text", "")).strip()
        if DEBUG:
            print(f"[whisper-mlx] '{text}'")
        return text
    except ImportError:
        # machine sans apple silicon : on ne reessaiera pas a chaque phrase.
        _mlx_whisper_ok = False
        return None
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[whisper-mlx] échec : {exc}")
        return None


def transcribe(pcm: bytes) -> str:
    if not pcm:
        return ""
    # 1) le gpu du mac : le plus rapide, le plus juste, et il ne depend de rien.
    text = mlx_transcribe(pcm)
    if text:
        return clean_transcript(text)
    # 2) voxtral, seulement si on l'a explicitement demande (il sort du mac).
    if os.getenv("AVA_USE_VOXTRAL") == "1":
        text = voxtral_transcribe(pcm)
        if text:
            return clean_transcript(text)
    # 3) filet : whisper sur le cpu, si mlx n'existe pas sur cette machine.
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # vad_filter enleve les silences/bruits (evite les hallucinations de whisper),
    # initial_prompt oriente vers du vocabulaire de commande en francais.
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


# ce qui trahit une phrase adressee a ava : un verbe d'action, une question,
# une politesse. une conversation de television n'en a aucun besoin.
_ADDRESSED_MARKERS = (
    "ouvre", "ouvrir", "lance", "ferme", "mets", "met ", "coupe", "monte",
    "baisse", "rappelle", "ajoute", "cherche", "dis moi", "explique", "montre",
    "envoie", "note", "joue", "arrete", "verrouille", "affiche", "trouve",
    "quel", "quelle", "qui ", "quoi", "comment", "pourquoi", "quand", "combien",
    "est ce que", "peux tu", "tu peux", "merci", "stop", "pause", "s il te plait",
)


def looks_ambient(text: str) -> bool:
    """Est-ce un flot de paroles qui ne s'adressait pas a Ava ?

    Releve dans les vrais journaux : un « salut ava » a ouvert l'ecoute pendant
    qu'un match passait a la television, et Ava est partie chercher sur le web
    « Thibaut Delphis et les Anéciens », puis a enchaine deux relances sur le
    commentaire du match. Le micro entend la piece entiere ; le seul indice
    disponible, c'est la **forme** de ce qui a ete transcrit.

    Une commande est courte et fait une seule phrase. Un commentaire sportif,
    une reunion ou une radio arrivent en plusieurs phrases sans verbe d'action
    ni question — c'est ce contraste qu'on lit ici, jamais le sujet.
    """
    raw = str(text or "").strip()
    words = _norm(raw).split()
    sentences = [part for part in re.split(r"[.!?…]+", raw) if part.strip()]
    # le nombre de phrases tranche avant la longueur : « Thibaut Delphis. Même
    # s'il n'arrive plus a se relancer. Desormais, les Aneciens » ne fait que
    # douze mots, et c'est pourtant du commentaire sportif pur.
    if len(sentences) >= 3 and len(words) >= 8:
        return True
    # ⚠️ un mot interrogatif ne suffit pas a prouver qu'on s'adresse a Ava : « ce
    # que j'adore c'est **quand** on est dans la baignoire » vient d'une video
    # qui passait dans la piece, et « quand » l'a fait passer pour une question.
    # Au-dela de vingt-cinq mots en plusieurs phrases, c'est du recit, pas une
    # demande — personne ne commande son assistante en quarante mots.
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
    """Execute plusieurs tours sans redemander le wake word entre les reponses."""
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

        # L'audio d'Ava vient de se terminer : on purge sa voix et on ouvre
        # directement une fenetre de suivi. Le silence ferme simplement la
        # session ; il ne provoque pas une nouvelle reponse vocale.
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
        # en suivi, aucun mot de reveil n'a ete dit : si ce qui arrive ressemble
        # a la piece plutot qu'a une demande, on referme sans rien repondre.
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
            # Deuxieme chance silencieuse : Ava ne parle plus par-dessus le debut
            # de la commande et le micro reste immediatement disponible.
            set_assistant_state(AvaState.LISTENING, "Je n'ai rien entendu — reessaie")
            command = transcribe(_record_utterance()).strip()
        print(f"[ava] commande : '{command}'")
        if len(command) < 3:
            speak("Je n'ai pas compris. Tu peux parler plus près du micro ou écrire ici.")
            return
        # le mot de reveil a bien ete dit, mais ce qui a suivi peut venir de la
        # piece (television, reunion). on le dit plutot que de partir chercher
        # sur le web le nom d'un joueur de football entendu au passage.
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
    """Point d'entree non bloquant du champ texte et des boutons Oui / Non."""
    command = str(text or "").strip()[:4000]
    if not command:
        return {"accepted": False, "error": "Écris une demande avant de l'envoyer."}
    # « stop » pendant qu'elle parle veut dire « tais-toi », pas « lance une
    # commande stop » : ca doit couper sans attendre la fin de la phrase.
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


# ce qu'on dit ou ecrit pour la faire taire, sans lui demander autre chose.
_HUSH = frozenset({"stop", "chut", "tais toi", "silence", "arrete", "arrete toi",
                   "ca suffit", "stop ava", "c est bon"})


def start_voice_interaction() -> dict:
    """Demarre l'ecoute depuis le bouton micro, sans exiger le wake word."""
    # cliquer le micro pendant qu'elle parle, c'est vouloir la couper pour
    # parler : on ne demande pas a l'utilisateur d'attendre la fin du briefing.
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


# --- les gestes de la barre de menus ----------------------------------------

def _menu_toggle_panel() -> None:
    if _overlay is None:
        return
    MENU_BAR.set_panel_open(_overlay.toggle_panel())


def _menu_listen() -> None:
    # parler a ava depuis le menu suppose de la voir repondre.
    if _overlay is not None and not _overlay.panel_visible():
        _overlay.set_panel_visible(True)
        MENU_BAR.set_panel_open(True)
    start_voice_interaction()


def _menu_settings() -> None:
    if _overlay is not None:
        _overlay.open_settings()
        MENU_BAR.set_panel_open(True)


def install_menu_bar() -> None:
    """Installe l'icone de barre de menus une fois la fenetre prete."""
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


# reveil beaucoup plus fiable : au lieu d'ecouter TOUT le francais (ou "bonjour ava"
# se noie dans le vocabulaire), on contraint vosk a une petite grammaire = juste
# les phrases de reveil + "[unk]" (qui absorbe le reste). le modele n'a plus qu'a
# trancher entre "reveil ou pas" -> il se trompe beaucoup moins. reglable :
# AVA_WAKE_GRAMMAR=0 pour revenir au vocabulaire complet.
_WAKE_GRAMMAR = os.getenv("AVA_WAKE_GRAMMAR", "1").strip().lower() not in ("0", "false", "no", "off")


def _wake_grammar(phrases) -> str:
    # construit la liste des phrases autorisees a partir de la config + quelques
    # variantes d'attaque courantes (ok/okay/hey/salut...), toujours suivies du nom.
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
        # on garde le nom seul reveillable et on decline les attaques usuelles.
        add(name)
        for pfx in list(prefixes):
            add(f"{pfx} {name}")
    add("[unk]")
    return json.dumps(entries)


def _strip_unk(text: str) -> str:
    # vosk en mode grammaire crache "[unk]" pour tout ce qui n'est pas une phrase
    # de reveil : on l'enleve avant l'analyse (sinon il pollue la commande captee).
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
        # pendant qu'ava ecoute/agit, on ne CONSOMME PAS la file : _record_utterance
        # a besoin de TOUT l'audio de la commande. si on lit ici en meme temps,
        # l'audio est partage entre les deux -> commande vide ou charabia.
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
                # "bonjour ava" = le grand reveil du matin (comme le double clap).
                # "ok ava" (et les autres) = mode commande.
                # seul un vrai bonjour lance le grand rituel (musique + apps +
                # briefing de 45 s). « salut ava » / « coucou ava » passent en
                # mode commande : les declencher par erreur en pleine journee
                # de travail etait bien trop couteux.
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


# --- detection du clap ------------------------------------------------------

class ClapDetector:
    def __init__(self, warmup_s: float = 0.0):
        self.in_event = False
        self.waiting_for_quiet = False
        self.peak = 0.0
        self.peak_t = 0.0
        self.last_clap_t = 0.0
        self.last_clap_peak = 0.0
        # petit echauffement : on laisse 3 s au plancher ambiant pour
        # s'etalonner avant d'accepter un clap (si ava demarre musique allumee)
        warmup = max(0.0, float(warmup_s))
        self.cooldown_until = time.monotonic() + warmup if warmup else 0.0
        # bruit ambiant (moyenne glissante lente) : quand la musique joue, le
        # plancher monte tout seul -> la batterie ne "clappe" plus a ta place.
        # dans le silence il redescend -> tes claps restent faciles a passer.
        self.ambient = 0.0

    def effective_floor(self) -> float:
        return max(MIN_RMS, self.ambient * 4.0)

    def register_clap(self, t: float, peak: float = 0.0) -> bool:
        # renvoie True si c'est le 2e clap d'un double clap valide
        if t < self.cooldown_until:
            return False
        gap = t - self.last_clap_t
        if self.last_clap_t and MIN_GAP_S <= gap <= MAX_GAP_S:
            # deux mains qui claquent font deux coups d'amplitude voisine ;
            # deux touches de clavier, non. ce garde-fou coupe la derniere
            # famille de faux positifs qui passait le plancher.
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
        # sinon : c'est (peut-etre) le 1er clap d'une nouvelle paire
        self.last_clap_t = t
        self.last_clap_peak = peak
        return False

    def feed(self, t: float, rms: float) -> bool:
        double = False
        # apprentissage du bruit ambiant hors des pics (constante ~2s)
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
                # le niveau s'est effondre : clap si c'est arrive vite
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
                # reste fort trop longtemps : voix ou musique
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
    # ouvre le micro et boucle : detection du clap + alimentation de vosk/whisper.
    # tourne en tache de fond quand l'overlay occupe le thread principal.
    detector = ClapDetector(warmup_s=3.0)
    q: deque = deque(maxlen=2000)
    device = pick_input_device()
    state = {"rate": SAMPLE_RATE}

    def callback(indata, frames, time_info, status):
        if status and DEBUG:
            print(f"[audio] {status}")
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(np.square(mono))))
        # on continue d'empiler les niveaux meme en pause : le chien de garde
        # s'en sert pour savoir si le flux est vivant. seule la reconnaissance
        # est coupee.
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

    # chien de garde : si le flux coreaudio meurt en silence (err -50 & co),
    # plus aucun bloc n'arrive -> on rouvre le micro automatiquement.
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
                    # Courbe douce : la voix normale occupe l'essentiel de 0..1,
                    # sans faire trembler l'orbe avec le simple bruit de fond.
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
    # le modele de voix local met ~20 s a se charger : on s'en occupe tout de
    # suite pour que le premier "bonjour ava" n'attende pas.
    print(f"[info] voix : moteur {voice_tts.engine_name()}")
    voice_tts.prewarm()
    # on prepare le briefing du matin des le demarrage pour qu'il parte
    # instantanement au premier clap (agenda + meteo + actu ia + citation)
    print("[info] preparation du briefing du matin (agenda, meteo, actu ia, citation)...")
    threading.Thread(target=get_welcome, daemon=True).start()
    threading.Thread(target=keep_welcome_warm, daemon=True).start()

    # assistant vocal "ok ava ..." en parallele du clap
    threading.Thread(target=assistant_loop, daemon=True).start()

    # `AVA_GREET=1` joue le rituel du matin au lancement. sans ca, la seule
    # facon de le revoir etait d'attendre le lendemain (il ne se joue qu'une
    # fois par jour) ou de dire « bonjour ava » devant le micro — impraticable
    # des qu'on veut verifier une modification du briefing.
    if os.getenv("AVA_GREET") == "1":
        def greet_at_launch() -> None:
            time.sleep(2.0)             # laisse l'overlay et la voix se poser
            trigger_welcome("lancement (AVA_GREET)")

        threading.Thread(target=greet_at_launch, daemon=True,
                         name="ava-greet-at-launch").start()
    # on precharge whisper en fond pour que la 1re commande soit rapide
    if WAKE_ENABLED:
        threading.Thread(target=get_whisper, daemon=True).start()

    if SETTINGS.get("morning", {}).get("open_apps_on_start", False):
        threading.Thread(
            target=open_startup_apps,
            daemon=True,
            name="ava-startup-apps",
        ).start()

    if OVERLAY_ENABLED and _overlay is not None:
        # l'overlay DOIT tourner sur le thread principal (macos) : l'audio passe
        # en fond, et on lance la fenetre orbe ici (bloquant jusqu'a fermeture).
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
            _overlay.start(str(HERE / "overlay" / "ava.html"))
            # la fenetre s'est fermee (bug gui, etc.) : ava ne doit PAS mourir
            # pour autant — l'ecoute continue sans interface.
            print("[overlay] fenetre fermee -> ava continue sans interface")
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] indisponible ({exc}) -> mode sans interface")
            if DEBUG:
                import traceback
                traceback.print_exc()
        # dans tous les cas : on garde le process (et donc l'ecoute) en vie
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
