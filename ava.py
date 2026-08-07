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
import hashlib
import json
import os
import queue
import random
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
from ava_config import STORE as CONFIG, clap_min_rms
from computer_use import ComputerUseEngine, parse_computer_intent
from conversation import LocalConversationEngine
from instance_lock import SingleInstanceLock
from wake_words import PartialWakeGate, extract_wake

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
FLOW_COOLDOWN_S = 8.0              # apres un declenchement complet, on se tait

# mode debug : affiche le niveau mesure et le seuil a chaque pic
DEBUG = os.getenv("AVA_DEBUG", os.getenv("JARVIS_DEBUG", "")).strip().lower() in (
    "1", "true", "yes", "on")

# musique spotify (uri, pas le lien web)
SPOTIFY_URI = SETTINGS["morning"]["spotify_uri"]

# apps a ouvrir + leur quadrant : "tl" haut-gauche, "tr" haut-droite,
# "bl" bas-gauche, "br" bas-droite.
APPS = [(item["name"], item["position"]) for item in SETTINGS["morning"]["apps"]]

# le briefing du matin est construit a la volee (meteo + actu ia + citation),
# donc le texte change chaque jour. la ville pour la meteo :
CITY = SETTINGS["identity"]["city"]
USER_NAME = SETTINGS["identity"]["name"]
WAKE_PHRASES = tuple(SETTINGS["wake"]["phrases"])
SYSTEM_VOICE = SETTINGS["voice"]["system_fallback"]
COMPUTER_USE_ENABLED = SETTINGS["computer_use"]["enabled"]

# une citation differente chaque matin : on en pioche une selon le jour de l'annee,
# donc ca tourne et ca revient au bout d'un an.
QUOTES = [
    "L'echec n'est pas la fin, c'est une lecon ; le succes n'est qu'une repetition apres l'echec.",
    "Tu tombes, tu apprends, tu recommences.",
    "Le seul echec, c'est de ne pas essayer.",
    "La discipline pese quelques grammes, le regret pese des tonnes.",
    "Fais aujourd'hui ce que les autres ne veulent pas, pour vivre demain comme les autres ne peuvent pas.",
    "Un reve ecrit avec une date devient un objectif.",
    "Ce n'est pas la montagne que l'on conquiert, mais soi-meme.",
    "La chance sourit a ceux qui sont prets.",
    "Chaque expert a d'abord ete un debutant. Commence, tu affineras en route.",
    "La constance bat le talent quand le talent ne travaille pas.",
    "Ne compte pas les jours, fais que les jours comptent.",
    "Le meilleur moment pour planter un arbre, c'etait il y a vingt ans ; le deuxieme meilleur, c'est maintenant.",
    "Tu n'as pas besoin d'etre parfait, tu as besoin d'etre en mouvement.",
    "Les petites ameliorations quotidiennes menent a des resultats spectaculaires.",
    "Fais-le avec peur s'il le faut, mais fais-le.",
    "Ton futur se construit avec ce que tu fais aujourd'hui, pas demain.",
    "La motivation te lance, l'habitude te fait avancer.",
    "Tombe sept fois, releve-toi huit.",
    "Le succes, c'est d'aller d'echec en echec sans perdre son enthousiasme.",
    "Concentre-toi sur le progres, pas sur la perfection.",
    "Ce qui te coute aujourd'hui te rendra fier demain.",
    "Rien ne remplace le travail acharne.",
    "Sois si bon qu'on ne pourra pas t'ignorer.",
    "La meilleure facon de predire l'avenir, c'est de le creer.",
    "Un pas apres l'autre, c'est encore la meilleure facon d'avancer.",
    "Les difficultes preparent des gens ordinaires a des destins extraordinaires.",
    "Crois en toi et tout devient possible.",
    "Le courage, ce n'est pas l'absence de peur, c'est agir malgre elle.",
    "Ce que tu repetes chaque jour finit par te definir.",
    "Vise la lune : meme en cas d'echec, tu atterriras parmi les etoiles.",
]


def daily_quote() -> str:
    doy = datetime.date.today().timetuple().tm_yday
    return QUOTES[doy % len(QUOTES)]


# --- meteo (open-meteo, sans cle) -----------------------------------------

WMO = {
    0: "ciel degage", 1: "plutot degage", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine legere", 53: "bruine", 55: "bruine dense",
    56: "bruine verglacante", 57: "bruine verglacante",
    61: "pluie legere", 63: "pluie", 65: "forte pluie",
    66: "pluie verglacante", 67: "pluie verglacante",
    71: "neige legere", 73: "neige", 75: "forte neige", 77: "grains de neige",
    80: "averses legeres", 81: "averses", 82: "fortes averses",
    85: "averses de neige", 86: "fortes averses de neige",
    95: "orage", 96: "orage avec grele", 99: "orage avec grele",
}

_geo_cache: dict = {}


def _geocode(city: str):
    if city in _geo_cache:
        return _geo_cache[city]
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": city, "count": 1, "language": "fr"}, timeout=10)
    res = r.json().get("results")
    if not res:
        return None
    latlon = (res[0]["latitude"], res[0]["longitude"])
    _geo_cache[city] = latlon
    return latlon


def weather_sentence() -> str:
    try:
        latlon = _geocode(CITY)
        if not latlon:
            return ""
        lat, lon = latlon
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 1}, timeout=10).json()
        temp = round(w["current"]["temperature_2m"])
        desc = WMO.get(w["current"]["weather_code"], "temps variable")
        tmax = round(w["daily"]["temperature_2m_max"][0])
        tmin = round(w["daily"]["temperature_2m_min"][0])
        return (f"A {CITY}, il fait actuellement {temp} degres, {desc}. "
                f"Aujourd'hui, jusqu'a {tmax} degres et un minimum de {tmin}.")
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[meteo] indisponible : {exc}")
        return ""


# --- actu ia (google news rss, sans cle) -----------------------------------

def ai_news_sentence() -> str:
    try:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": "intelligence artificielle when:1d",
            "hl": "fr-FR", "gl": "FR", "ceid": "FR:fr"})
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        if not items:
            return "Cote intelligence artificielle, rien de majeur a signaler depuis hier."
        title = items[0].find("title").text or ""
        # google news ajoute " - Nom du media" a la fin, on l'enleve
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]
        return f"Dans le monde de l'intelligence artificielle depuis hier : {title}."
    except Exception as exc:  # noqa: BLE001
        if DEBUG:
            print(f"[actu ia] indisponible : {exc}")
        return ""


def build_welcome_text() -> str:
    parts = [f"Bonjour {USER_NAME}, c'est Ava. J'espere que tu vas bien ! Tout est maintenant pret pour toi."]
    meteo = weather_sentence()
    if meteo:
        parts.append(meteo)
    actu = ai_news_sentence()
    if actu:
        parts.append(actu)
    parts.append(f"Ta citation du jour : {daily_quote()}")
    parts.append("Bonne journee !")
    return " ".join(parts)

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVEN_VOICE = SETTINGS["voice"]["voice_id"] or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVEN_MODEL = SETTINGS["voice"]["model_id"] or os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
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
    elif snapshot.state != AvaState.BOOTING:
        ui("set_state", snapshot.state.value, snapshot.label or None)


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
    # on pilote spotify en applescript. le lien web ne marche pas, il faut l'uri.
    print("  -> spotify")
    _osascript(f'tell application "Spotify" to play track "{SPOTIFY_URI}"')


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
    return {
        "tl": (left,  top, w, h),
        "tr": (right, top, w, h),
        "bl": (left,  mid, w, h),
        "br": (right, mid, w, h),
    }[where]


def open_and_place(app: str, where: str, sb) -> None:
    # ouvre l'app puis la range dans son coin (via system events).
    # macos peut demander l'autorisation "accessibilite" la 1re fois.
    print(f"  -> {app} ({where})")
    subprocess.run(["open", "-a", app], check=False)
    x, y, w, h = quadrant_rect(where, sb)
    script = f'''
    tell application "System Events"
        set tries to 0
        repeat until (exists (process "{app}")) or tries > 20
            delay 0.2
            set tries to tries + 1
        end repeat
        try
            tell process "{app}"
                set frontmost to true
                delay 0.3
                set position of window 1 to {{{x}, {y}}}
                set size of window 1 to {{{w}, {h}}}
            end tell
        end try
    end tell
    '''
    _osascript(script)


def welcome_audio_path(text: str) -> Path:
    # nom = hash de (texte + voix + modele) : le texte change chaque jour,
    # donc on obtient un nouveau fichier par jour, mais deux claps rapproches
    # rejouent le meme cache (instantane, pas de nouvel appel api).
    key = f"{text}|{ELEVEN_VOICE}|{ELEVEN_MODEL}".encode("utf-8")
    name = hashlib.sha256(key).hexdigest()[:16] + ".mp3"
    return CACHE_DIR / name


def ensure_welcome_audio(text: str) -> Path | None:
    # renvoie le mp3 (depuis le cache si possible), sinon le genere via elevenlabs.
    if not ELEVEN_KEY or not ELEVEN_VOICE:
        if DEBUG:
            print("[voix] pas de cle / voice id -> voix desactivee")
        return None
    path = welcome_audio_path(text)
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=ELEVEN_KEY)
        audio = client.text_to_speech.convert(
            voice_id=ELEVEN_VOICE,
            model_id=ELEVEN_MODEL,
            text=text,
            output_format="mp3_44100_128",
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            for chunk in audio:
                if chunk:
                    f.write(chunk)
        return path
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "402" in msg:
            print("[voix] erreur 402 : cette voix est refusee sur le plan gratuit "
                  "(voix de la library). prends une voix native d'elevenlabs.")
        else:
            print(f"[voix] impossible de generer l'audio : {exc}")
        return None


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


def _drain_wake_queue() -> None:
    # on jette l'audio accumule pendant le reveil (le micro a entendu ava
    # parler dans les enceintes) pour ne pas re-declencher juste apres.
    while True:
        try:
            _wake_q.get_nowait()
        except queue.Empty:
            break


def run_welcome_flow() -> None:
    try:
        set_assistant_state(AvaState.BOOTING, "Preparation de ton espace", force=True)
        print("*** reveil du bureau ***")
        ui("boot", [a for a, _ in APPS], "Preparation de ton espace...")
        sb = screen_bounds()
        # la musique et les apps ne se bloquent pas entre elles
        play_spotify()
        for i, (app, where) in enumerate(APPS):
            open_and_place(app, where, sb)
            ui("boot_step", i)
        # garde la carte visible pendant la preparation du briefing ; au premier
        # lancement le reseau ou elevenlabs peuvent prendre quelques secondes.
        text, path = get_welcome()
        if path:
            print("  -> voix")
            set_assistant_state(AvaState.SPEAKING, "Ton briefing du matin")
            subprocess.run(["afplay", str(path)], check=False)
        elif text:
            set_assistant_state(AvaState.SPEAKING, "Ton briefing du matin")
            subprocess.run(["say", "-v", SYSTEM_VOICE, text], check=False)
        else:
            ui("boot_done")
        print("*** pret. ***\n")
    finally:
        _drain_wake_queue()
        # le cooldown ne demarre qu'une fois le reveil fini
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        ASSISTANT_STATE.dormant()


def trigger_welcome(source: str) -> None:
    with _trigger_lock:
        now = time.time()
        if _flow_active.is_set() or now - _last_trigger["ts"] < FLOW_COOLDOWN_S:
            return
        _last_trigger["ts"] = now
        # reserve le flux AVANT de lancer le thread : sans cela, le mot-cle peut
        # gagner la course et demarrer une seconde interaction en parallele.
        _flow_active.set()
    print(f"\n[declencheur : {source}]")
    try:
        threading.Thread(target=run_welcome_flow, daemon=True).start()
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
    if not WAKE_ENABLED:
        return
    pcm = resample_for_wake(mono, rate)
    if not pcm:
        return
    _put_drop_oldest(_wake_q, pcm)


# --- la voix d'ava (reponses courtes) --------------------------------------

def speak(text: str, state: str = "speaking") -> None:
    # met l'orbe dans l'etat voulu + affiche le texte, puis parle.
    # on genere via elevenlabs (mis en cache par texte), et si ca echoue
    # (quota, reseau...), on bascule sur la voix systeme macos "say".
    set_assistant_state(state, text)
    path = ensure_welcome_audio(text)
    if path:
        subprocess.run(["afplay", str(path)], check=False)
    else:
        subprocess.run(["say", "-v", SYSTEM_VOICE, text], check=False)


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
        ASSISTANT_STATE.dormant()


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
    "dia": "Dia", "promethee": "Promethee 2", "promethe": "Promethee 2",
    "code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "visual studio code": "Visual Studio Code", "cursor": "Cursor",
    "finder": "Finder", "messages": "Messages", "photos": "Photos",
    "reglages": "System Settings", "parametres": "System Settings",
    "calendrier": "Calendar", "agenda": "Calendar", "notes": "Notes",
    "rappels": "Reminders", "musique": "Music", "obsidian": "Obsidian",
    "whatsapp": "WhatsApp", "discord": "Discord",
}


def _installed_apps() -> dict:
    apps: dict = {}
    for d in ("/Applications", "/Applications/Utilities", "/System/Applications",
              "/System/Applications/Utilities", str(Path.home() / "Applications")):
        try:
            for p in Path(d).glob("*.app"):
                apps[_norm(p.stem)] = p.stem
        except Exception:
            pass
    return apps


def resolve_app(spoken: str):
    spoken = _norm(spoken)
    if not spoken:
        return None
    if spoken in _APP_ALIASES:
        return _APP_ALIASES[spoken]
    apps = _installed_apps()
    if spoken in apps:
        return apps[spoken]
    for norm_name, real in apps.items():          # correspondance partielle
        if spoken in norm_name or norm_name in spoken:
            return real
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
    speak(f"Desole, je ne trouve pas l'application {target}.")


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


def _parse_duration_s(text: str):
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


def execute_command(cmd: str) -> None:
    raw = cmd.strip()
    c = _norm(raw)
    if not c:
        speak("Je n'ai pas compris, tu peux repeter ?")
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
        speak(outcome.message or "Action terminee.")
        return

    # ouvrir une app / un site ("votre"/"offre" = voxtral qui entend mal "ouvre")
    for verb in ("ouvre moi ", "ouvre-moi ", "ouvre ", "ouvrir ", "lance ",
                 "lancer ", "demarre ", "affiche ", "votre ", "offre ",
                 "montre ", "va sur "):
        if c.startswith(verb):
            return _open_target(c.split(verb, 1)[1].strip())

    # mails
    if any(w in c for w in ("mail", "mails", "gmail", "courriel", "boite mail")):
        open_url("https://mail.google.com/mail/u/0/")
        speak("J'ouvre ta boite mail.")
        return

    # recherche web
    for verb in ("recherche moi ", "cherche moi ", "recherche ", "cherche ", "google "):
        if verb in c:
            q = c.split(verb, 1)[1].strip()
            if q:
                open_url("https://www.google.com/search?q=" + urllib.parse.quote(q))
                speak(f"Voici les resultats pour {q}.")
                return

    # heure
    if "heure" in c:
        now = datetime.datetime.now()
        speak(f"Il est {now.hour} heures {now.minute:02d}.")
        return

    # date du jour
    if "quel jour" in c or "quelle date" in c:
        speak("Nous sommes le " + datetime.date.today().strftime("%d/%m/%Y") + ".")
        return

    # meteo
    if "meteo" in c or "quel temps" in c or "il fait combien" in c:
        speak(weather_sentence() or "Meteo indisponible pour le moment.")
        return

    # actu ia
    if "intelligence artificielle" in c or "actu" in c or "nouveaute" in c:
        speak(ai_news_sentence() or "Rien de neuf cote intelligence artificielle.")
        return

    # controle spotify : morceau suivant / precedent / reprendre
    if any(w in c for w in ("musique", "chanson", "morceau", "titre", "spotify", "son morceau")):
        if any(w in c for w in ("suivant", "suivante", "prochain", "prochaine", "next", "apres")):
            _spotify("next track"); speak("Morceau suivant."); return
        if any(w in c for w in ("precedent", "precedente", "reviens", "davant", "previous", "retour")):
            _spotify("previous track"); speak("Morceau precedent."); return
        if any(w in c for w in ("reprend", "continue", "relance", "remets", "remet")):
            _spotify("play"); speak("Je reprends la musique."); return

    # verrouiller l'ecran
    if "verrouille" in c or "verrouiller" in c or ("lock" in c and "ecran" in c):
        _osascript('tell application "System Events" to keystroke "q" using {control down, command down}')
        speak("Je verrouille l'ecran."); return

    # capture d'ecran (avant la luminosite, sinon "ecran" est ambigu)
    if "capture" in c or "screenshot" in c or "screen shot" in c:
        dest = str(Path.home() / "Desktop" /
                   ("capture-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"))
        subprocess.run(["screencapture", "-x", dest], check=False)
        speak("Capture d'ecran enregistree sur le bureau."); return

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
            threading.Timer(
                secs, notify, args=("C'est l'heure ! Ton minuteur est termine.",)
            ).start()
            speak(f"Minuteur lance pour {label}."); return
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
            speak("C'est note."); return
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
                speak("Le moteur de discussion local n'est pas lance. Ouvre LM Studio et charge un modele pour discuter avec moi.")
            return

    # phrase sans verbe mais qui contient une app connue ("l'application discord",
    # "votre discord"...) : on ouvre l'app plutot que de chercher sur le web.
    # (place en dernier pour ne pas court-circuiter musique/mails/etc.)
    for token in c.replace("'", " ").split():
        if token in _APP_ALIASES:
            return _open_target(token)

    # rien reconnu : on lance une recherche web de la phrase entiere
    open_url("https://www.google.com/search?q=" + urllib.parse.quote(raw))
    speak("Je ne suis pas sure d'avoir compris, je lance une recherche.")


# --- boucle assistant : "ok ava ..." + commande ----------------------------

# --- transcription de la commande avec whisper (local, precis en francais) --
WHISPER_SIZE = os.getenv("AVA_WHISPER_SIZE", "small").strip()  # small ou medium
_whisper = None
_whisper_lock = threading.Lock()


def get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")
        return _whisper


def _record_utterance(max_s: float = 8.0, silence_s: float = 0.9,
                      start_timeout_s: float = 6.0) -> bytes:
    # enregistre ce que tu dis apres "oui ?", et s'arrete des que tu te tais
    frames = []
    preroll: list = []
    started = False
    silence = 0.0
    spoken = 0.0
    waited = 0.0
    while True:
        try:
            data = _wake_q.get(timeout=1.0)
        except queue.Empty:
            if started:
                break
            waited += 1.0
            if waited > start_timeout_s:
                break
            continue
        arr = np.frombuffer(data, dtype=np.int16)
        if arr.size == 0:
            continue
        dur = arr.size / WAKE_SR
        rms = float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))
        if not started:
            waited += dur
            preroll.append(data)     # on garde ~0.5s d'avant-voix
            if len(preroll) > 50:
                preroll.pop(0)
            if rms > 0.012:         # la voix commence (seuil bas : voix douce ok)
                started = True
                # le pre-roll evite de couper la 1re syllabe ("Ouvre" -> "vre")
                frames.extend(preroll)
                preroll = []
            elif waited > start_timeout_s:
                break
            continue
        frames.append(data)
        spoken += dur
        if rms < 0.012:             # silence : on compte pour savoir quand tu as fini
            silence += dur
            if silence >= silence_s:
                break
        else:
            silence = 0.0
        if spoken > max_s:
            break
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
            timeout=10,
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


def transcribe(pcm: bytes) -> str:
    if not pcm:
        return ""
    # 1) voxtral si possible (meilleure precision en francais)
    text = voxtral_transcribe(pcm)
    if text is not None and text != "":
        return text
    # 2) whisper local en secours (hors-ligne, gratuit)
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # vad_filter enleve les silences/bruits (evite les hallucinations de whisper),
    # initial_prompt oriente vers du vocabulaire de commande en francais.
    segments, _ = get_whisper().transcribe(
        audio, language="fr", beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.5,
        initial_prompt="Commande vocale en francais : ouvre une application, "
                       "cherche sur internet, mets la musique, quelle heure est-il, "
                       "la meteo, mes mails.",
    )
    return " ".join(s.text for s in segments).strip()


# petites reponses variees quand ava prend l'appel, avant d'ecouter la demande
ACK_PHRASES = ["Oui ?", "Je t'ecoute.", f"Oui {USER_NAME} ?", "Hmm ?", "J'ecoute.", "Oui ?"]

COMPUTER_USE = ComputerUseEngine(
    ttl_s=SETTINGS["computer_use"]["confirmation_ttl_seconds"],
)
CONVERSATION = LocalConversationEngine(
    base_url=SETTINGS["conversation"]["base_url"],
    model=SETTINGS["conversation"]["model"],
)


def apply_runtime_settings(settings: dict) -> None:
    global SETTINGS, MIN_RMS, MIN_GAP_S, MAX_GAP_S, CLAP_ENABLED
    global SPOTIFY_URI, APPS, CITY, USER_NAME, WAKE_PHRASES, SYSTEM_VOICE
    global ELEVEN_VOICE, ELEVEN_MODEL, COMPUTER_USE_ENABLED, ACK_PHRASES
    SETTINGS = settings
    clap = settings["wake"]["clap"]
    MIN_RMS = clap_min_rms(clap["sensitivity"])
    MIN_GAP_S = clap["min_gap_ms"] / 1000
    MAX_GAP_S = clap["max_gap_ms"] / 1000
    CLAP_ENABLED = clap["enabled"]
    SPOTIFY_URI = settings["morning"]["spotify_uri"]
    APPS = [(item["name"], item["position"]) for item in settings["morning"]["apps"]]
    CITY = settings["identity"]["city"]
    USER_NAME = settings["identity"]["name"]
    WAKE_PHRASES = tuple(settings["wake"]["phrases"])
    SYSTEM_VOICE = settings["voice"]["system_fallback"]
    ELEVEN_VOICE = settings["voice"]["voice_id"] or os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    ELEVEN_MODEL = settings["voice"]["model_id"]
    COMPUTER_USE_ENABLED = settings["computer_use"]["enabled"]
    COMPUTER_USE.ttl_s = settings["computer_use"]["confirmation_ttl_seconds"]
    CONVERSATION.configure(
        settings["conversation"]["base_url"], settings["conversation"]["model"],
    )
    ACK_PHRASES = [
        "Oui ?", "Je t'ecoute.", f"Oui {USER_NAME} ?", "Hmm ?", "J'ecoute.", "Oui ?",
    ]
    with _welcome_lock:
        _welcome.update(day=None, ts=0.0, text="", path=None)


CONFIG.subscribe(apply_runtime_settings)


def handle_wake(prefilled_command: str = "") -> None:
    with _trigger_lock:
        if _flow_active.is_set():
            return
        _flow_active.set()
    try:
        set_assistant_state(AvaState.LISTENING, "Je t'ecoute", force=True)
        speak(random.choice(ACK_PHRASES), state="listening")  # ava prend l'appel
        _drain_wake_queue()                 # on jette l'audio du "ok ava" + du "oui ?"
        set_assistant_state(AvaState.LISTENING, "Je t'ecoute")
        command = prefilled_command.strip()
        if not command:
            command = transcribe(_record_utterance()).strip()
        if len(command) < 3:
            # souvent la commande est dite PENDANT le "oui ?" (donc jetee par
            # l'anti-echo). on offre une deuxieme chance au lieu d'abandonner.
            set_assistant_state(AvaState.LISTENING, "Vas-y, je t'ecoute")
            speak("Vas-y, je t'ecoute.", state="listening")
            _drain_wake_queue()
            command = transcribe(_record_utterance()).strip()
        set_assistant_state(AvaState.THINKING, "Un instant")
        print(f"[ava] commande : '{command}'")
        if len(command) < 3:
            speak("Je n'ai pas compris, tu peux repeter ?")
            return
        execute_command(command)
    finally:
        _drain_wake_queue()
        _last_trigger["ts"] = time.time()
        _flow_active.clear()
        ASSISTANT_STATE.dormant()


def assistant_loop() -> None:
    if not WAKE_ENABLED:
        print("[ava] modele vocal absent -> commandes vocales desactivees")
        return
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except Exception:
        print("[ava] vosk absent -> commandes vocales desactivees")
        return
    SetLogLevel(-1)  # on coupe les logs bavards de vosk
    model = Model(str(WAKE_MODEL_DIR))
    rec = KaldiRecognizer(model, WAKE_SR)  # vocabulaire complet
    partial_gate = PartialWakeGate()
    _last_partial_dbg: dict = {}       # pour ne pas spammer le meme partiel
    print('[ava] "bonjour ava" = grand reveil | "ok ava" = commande vocale')
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
            txt = json.loads(rec.Result()).get("text", "")
            if DEBUG and txt.strip():
                print(f"[vosk final]   '{txt}'")
            wake = extract_wake(txt, WAKE_PHRASES)
            partial_gate.reset()
        else:
            txt = json.loads(rec.PartialResult()).get("partial", "")
            if DEBUG and txt.strip() and txt != _last_partial_dbg.get("t"):
                _last_partial_dbg["t"] = txt
                print(f"[vosk partiel] '{txt}'")
            wake = partial_gate.feed(txt, WAKE_PHRASES)
        if wake.detected:
            if DEBUG:
                print(f"[ava] entendu : '{txt}'")
            rec.Reset()
            partial_gate.reset()
            try:
                # "bonjour ava" = le grand reveil du matin (comme le double clap).
                # "ok ava" (et les autres) = mode commande.
                greeting = wake.phrase.split()[0] in ("bonjour", "bonsoir", "coucou")
                if greeting and not wake.trailing_command:
                    trigger_welcome("bonjour ava")
                else:
                    handle_wake(wake.trailing_command)
            except Exception as exc:  # noqa: BLE001
                print(f"[ava] interaction interrompue : {exc}")
                _flow_active.clear()
                ASSISTANT_STATE.dormant()
                rec.Reset()


# --- detection du clap ------------------------------------------------------

class ClapDetector:
    def __init__(self):
        self.in_event = False
        self.waiting_for_quiet = False
        self.peak = 0.0
        self.peak_t = 0.0
        self.last_clap_t = 0.0
        # petit echauffement : on laisse 3 s au plancher ambiant pour
        # s'etalonner avant d'accepter un clap (si ava demarre musique allumee)
        self.cooldown_until = time.monotonic() + 3.0
        # bruit ambiant (moyenne glissante lente) : quand la musique joue, le
        # plancher monte tout seul -> la batterie ne "clappe" plus a ta place.
        # dans le silence il redescend -> tes claps restent faciles a passer.
        self.ambient = 0.0

    def effective_floor(self) -> float:
        return max(MIN_RMS, self.ambient * 4.0)

    def register_clap(self, t: float) -> bool:
        # renvoie True si c'est le 2e clap d'un double clap valide
        if t < self.cooldown_until:
            return False
        gap = t - self.last_clap_t
        if self.last_clap_t and MIN_GAP_S <= gap <= MAX_GAP_S:
            self.last_clap_t = 0.0
            self.cooldown_until = t + FLOW_COOLDOWN_S
            return True
        # sinon : c'est (peut-etre) le 1er clap d'une nouvelle paire
        self.last_clap_t = t
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
                        double = self.register_clap(t)
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
    detector = ClapDetector()
    q: deque = deque(maxlen=2000)
    device = pick_input_device()
    state = {"rate": SAMPLE_RATE}

    def callback(indata, frames, time_info, status):
        if status and DEBUG:
            print(f"[audio] {status}")
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(np.square(mono))))
        q.append((time.monotonic(), rms))
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
            if CLAP_ENABLED and detector.feed(t, rms):
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
    ASSISTANT_STATE.dormant()
    print('ava est en veille. dis "bonjour ava" ou claque deux fois des mains. (ctrl+c pour arreter)')
    if DEBUG:
        print("[debug] active : niveaux et decisions affiches a chaque pic")
    if not ELEVEN_KEY or not ELEVEN_VOICE:
        print("[info] voix desactivee (pas de cle elevenlabs) : le reste marche.")
    else:
        # on prepare le briefing du matin des le demarrage pour qu'il parte
        # instantanement au premier clap (meteo + actu ia + citation du jour)
        print("[info] preparation du briefing du matin (meteo, actu ia, citation)...")
        threading.Thread(target=get_welcome, daemon=True).start()

    # assistant vocal "ok ava ..." en parallele du clap
    threading.Thread(target=assistant_loop, daemon=True).start()
    # on precharge whisper en fond pour que la 1re commande soit rapide
    if WAKE_ENABLED:
        threading.Thread(target=get_whisper, daemon=True).start()

    if OVERLAY_ENABLED and _overlay is not None:
        # l'overlay DOIT tourner sur le thread principal (macos) : l'audio passe
        # en fond, et on lance la fenetre orbe ici (bloquant jusqu'a fermeture).
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
