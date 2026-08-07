# overlay.py - petite fenetre transparente always-on-top (en haut a droite)
# qui affiche l'orbe d'ava. pilotee depuis le backend via evaluate_js.
# doit tourner sur le THREAD PRINCIPAL (contrainte macos/webkit) : appeler
# start() en dernier depuis main(), le reste du programme dans des threads.

import json
import subprocess
import time

import webview

_window = None

WIDTH = 380
HEIGHT = 460
MARGIN = 12
TOP = 38  # sous la barre de menu macos


class _Api:
    """pont expose au js du panneau de reglages -> store de config."""

    def get_config(self):
        try:
            from ava_config import STORE
            return STORE.snapshot()
        except Exception:
            return {}

    def save_config(self, patch):
        try:
            from ava_config import STORE
            return STORE.update(patch or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}


def _screen_size():
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=8)
        x1, y1, x2, y2 = [int(v.strip()) for v in out.stdout.split(",")]
        return x2 - x1, y2 - y1
    except Exception:
        return 1440, 900


def start(html_path: str) -> None:
    global _window
    sw, _sh = _screen_size()
    x = max(0, sw - WIDTH - MARGIN)

    def _create():
        # PAS de x/y ici : donner une position a la creation declenche un
        # windowDidMove alors que la fenetre n'est pas encore rattachee a un
        # ecran -> crash cocoa "'NoneType' object has no attribute 'frame'".
        # on la deplace apres coup, une fois affichee (voir _on_loaded).
        return webview.create_window(
            "Ava", html_path,
            width=WIDTH, height=HEIGHT,
            frameless=True, easy_drag=False, on_top=True,
            transparent=True, background_color="#000000",
            resizable=False, focus=False, js_api=_Api(),
        )

    # relance rapide : une fenetre pywebview qui vient d'etre tuee peut laisser
    # NSScreen.mainScreen() a None un court instant ("NoneType frame"). petit
    # delai + reessai pour ne pas tomber en "mode sans interface" pour rien.
    last_exc = None
    for attempt in range(10):
        try:
            _window = _create()
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _window = None
            try:
                webview.windows.clear()
            except Exception:
                pass
            time.sleep(0.8)
    if _window is None:
        raise last_exc if last_exc else RuntimeError("fenetre overlay introuvable")

    def _on_loaded():
        # maintenant que la fenetre est rattachee a un ecran, on peut la placer
        # en haut a droite sans declencher le crash cocoa.
        try:
            _window.move(x, TOP)
        except Exception:
            pass
        # fond transparent + on prend la main (annule la demo).
        _eval("window.avaOverlayMode && avaOverlayMode()")
        # confirme visuellement que le lancement a fonctionne, puis revient en
        # veille invisible. un vrai reveil annule automatiquement ce minuteur.
        start_hidden = False
        startup_hint_seconds = 5
        try:
            from ava_config import STORE
            ui_settings = STORE.snapshot().get("ui", {})
            start_hidden = bool(ui_settings.get("start_hidden"))
            startup_hint_seconds = int(ui_settings.get("startup_hint_seconds", 5))
        except Exception:
            pass
        if start_hidden and startup_hint_seconds > 0:
            _eval(f"window.avaStartupHint && avaStartupHint({startup_hint_seconds})")
        elif start_hidden:
            _eval("window.avaDormant && avaDormant()")
        else:
            _eval("window.avaIdle && avaIdle()")

    _window.events.loaded += _on_loaded
    # meme protection sur le demarrage de la boucle gui : l'ecran peut ne pas
    # etre pret juste apres la mort d'une instance precedente.
    for attempt in range(3):
        try:
            webview.start()  # bloque le thread principal jusqu'a fermeture
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0)
    raise last_exc if last_exc else RuntimeError("gui overlay indemarrable")


def _eval(code: str) -> None:
    if _window is None:
        return
    try:
        _window.evaluate_js(code)
    except Exception:
        pass


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=True)


# --- api appelee par le backend --------------------------------------------

def set_state(state: str, label=None) -> None:
    if label is None:
        _eval(f"window.avaSetState && avaSetState({_j(state)})")
    else:
        _eval(f"window.avaSetState && avaSetState({_j(state)},{_j(label)})")


def idle() -> None:
    _eval("window.avaIdle && avaIdle()")


def dormant() -> None:
    # "eteinte" : cache completement la pilule et la carte de demarrage.
    # n'importe quel autre etat (set_state/boot) les reveille.
    _eval("window.avaDormant && avaDormant()")


def boot(apps, sub=None) -> None:
    sub_js = _j(sub) if sub else "null"
    _eval(f"window.avaBoot && avaBoot({_j(list(apps))},{sub_js})")


def boot_step(i: int) -> None:
    _eval(f"window.avaBootStep && avaBootStep({int(i)})")


def boot_done() -> None:
    _eval("window.avaBootDone && avaBootDone()")
