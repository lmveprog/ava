# overlay.py - petite fenetre transparente always-on-top (en haut a droite)
# qui affiche l'orbe d'ava. pilotee depuis le backend via evaluate_js.
# doit tourner sur le THREAD PRINCIPAL (contrainte macos/webkit) : appeler
# start() en dernier depuis main(), le reste du programme dans des threads.

import base64
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Callable
import urllib.parse

DEBUG = os.getenv("AVA_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

import webview

try:
    import AppKit
    from PyObjCTools import AppHelper
except Exception:  # pragma: no cover - Ava cible macOS, fallback pywebview sinon
    AppKit = None
    AppHelper = None

_window = None
_command_handler: Callable[[str], object] | None = None
_voice_handler: Callable[[], object] | None = None
_ready_handler: Callable[[], object] | None = None
_startup_payload: dict | None = None

WIDTH = 420
HEIGHT = 590
START_WIDTH = 720
START_HEIGHT = 520
MARGIN = 12
TOP = 38  # sous la barre de menu macos
ANCHOR_GAP = 6  # le petit jour entre l'icone et le haut du panneau
_target_position = (0, TOP)
_panel_visible = True


class _Api:
    """pont expose au js du panneau de reglages -> store de config."""

    def get_config(self):
        try:
            from ava.config import STORE
            return STORE.snapshot()
        except Exception:
            return {}

    def save_config(self, patch):
        try:
            from ava.config import STORE
            return STORE.update(patch or {})
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def submit_text(self, text):
        value = str(text or "").strip()
        if not value:
            return {"accepted": False, "error": "La commande est vide."}
        if _command_handler is None:
            return {"accepted": False, "error": "Ava n'est pas encore prete."}
        try:
            result = _command_handler(value)
            return result if isinstance(result, dict) else {"accepted": bool(result)}
        except Exception as exc:  # noqa: BLE001
            return {"accepted": False, "error": str(exc)}

    def start_listening(self):
        if _voice_handler is None:
            return {"accepted": False, "error": "Le micro n'est pas encore pret."}
        try:
            result = _voice_handler()
            return result if isinstance(result, dict) else {"accepted": bool(result)}
        except Exception as exc:  # noqa: BLE001
            return {"accepted": False, "error": str(exc)}

    # --- connecteur google (agenda) ---------------------------------------
    # begin_connect rend la main tout de suite et travaille en fond : le
    # panneau se contente de repasser sur google_status() toutes les secondes.

    def google_status(self):
        try:
            from ava.services.google_auth import AUTH
            return AUTH.status()
        except Exception as exc:  # noqa: BLE001
            return {"configured": False, "connected": False, "error": str(exc)}

    def google_connect(self):
        try:
            from ava.services.google_auth import AUTH
            return AUTH.begin_connect()
        except Exception as exc:  # noqa: BLE001
            return {"started": False, "error": str(exc)}

    def google_disconnect(self):
        try:
            from ava.services.google_auth import AUTH
            return AUTH.disconnect()
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

    def test_voice(self, voice=None):
        """Enregistre les reglages de voix puis en joue un extrait.

        Rend la main tout de suite : la synthese locale prend quelques secondes
        et le panneau ne doit pas se figer pendant ce temps.
        """
        try:
            from ava.config import STORE
            if isinstance(voice, dict):
                STORE.update({"voice": voice})

            def play():
                from ava.audio import voice_tts as voice_tts
                sample = ("Bonjour Mathieu, c'est Ava. Voilà à quoi je ressemble "
                          "avec ces réglages.")
                path = voice_tts.synthesize(sample)
                if path:
                    voice_tts.speak_file(path)
                else:
                    voice_tts.say_fallback(
                        sample, STORE.snapshot()["voice"]["system_fallback"])

            import threading
            threading.Thread(target=play, daemon=True).start()
            return {"started": True}
        except Exception as exc:  # noqa: BLE001
            return {"started": False, "error": str(exc)}

    def open_external(self, url):
        value = str(url or "").strip()
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"opened": False}
        try:
            subprocess.run(["open", value], check=False, timeout=8)
            return {"opened": True}
        except Exception:
            return {"opened": False}

    def finish_startup(self):
        finish_startup()
        return {"finished": True}


def set_handlers(
    command_handler: Callable[[str], object] | None = None,
    voice_handler: Callable[[], object] | None = None,
    ready_handler: Callable[[], object] | None = None,
) -> None:
    """Branche les actions du mini-plugin sans importer ``ava`` en retour.

    `ready_handler` se declenche quand la fenetre est affichee : c'est le moment
    ou l'on peut installer la barre de menus, pas avant (l'application cocoa
    n'existe pas encore).
    """
    global _command_handler, _voice_handler, _ready_handler
    _command_handler = command_handler
    _voice_handler = voice_handler
    _ready_handler = ready_handler


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


def _layout_origin(
    screen_x: float,
    screen_y: float,
    screen_width: float,
    screen_height: float,
    width: float,
    height: float,
    placement: str,
    anchor: tuple[float, float, float, float] | None = None,
) -> tuple[int, int]:
    """Calcule une origine Cocoa dans la zone visible de l'ecran courant.

    `anchor` porte le cadre de l'icone de barre de menus. Quand il est fourni, le
    panneau tombe **sous son icone** plutot que dans le coin de l'ecran : c'est
    ce qui fait la difference entre une extension et une fenetre posee la.
    """
    if placement == "center":
        x = screen_x + (screen_width - width) / 2
        y = screen_y + (screen_height - height) / 2
    elif anchor is not None:
        anchor_x, anchor_y, anchor_width, _anchor_height = anchor
        x = anchor_x + anchor_width / 2 - width / 2
        y = anchor_y - height - ANCHOR_GAP
        # l'icone peut etre tout au bord (ou derriere l'encoche) : on rentre le
        # panneau dans la zone visible sans le decoller de son icone.
        x = max(screen_x + MARGIN, min(x, screen_x + screen_width - width - MARGIN))
        y = max(screen_y + MARGIN, min(y, screen_y + screen_height - height))
    else:
        x = screen_x + screen_width - width - MARGIN
        y = screen_y + screen_height - height - MARGIN
    return round(x), round(y)


def _menu_bar_anchor():
    """Le cadre de l'icone d'ava, si la barre de menus est bien installee."""
    try:
        from ava.ui.menubar import MENU_BAR
        return MENU_BAR.anchor()
    except Exception:  # noqa: BLE001 - sans barre de menus on garde le coin
        return None


def _screen_holding(anchor):
    """L'ecran qui contient le centre de l'ancre, s'il y en a un."""
    if AppKit is None or anchor is None:
        return None
    x, y, width, height = anchor
    point = AppKit.NSMakePoint(x + width / 2, y + height / 2)
    try:
        for screen in AppKit.NSScreen.screens():
            if AppKit.NSPointInRect(point, screen.frame()):
                return screen
    except Exception:  # noqa: BLE001
        pass
    return None


def _place_window(width: int, height: int, placement: str) -> None:
    """Redimensionne et place Ava sur l'ecran qui la contient vraiment.

    Le calcul historique via Finder melangeait les coordonnees de bureau et
    celles de pywebview, surtout avec Retina ou plusieurs ecrans. Cocoa fournit
    directement la zone visible et son origine ; on applique donc le cadre en
    une seule operation sur le thread principal.
    """
    if _window is None:
        return
    native = getattr(_window, "native", None)
    if native is not None and AppKit is not None and AppHelper is not None:
        def apply_native_frame() -> None:
            try:
                # l'ancre se lit ici, donc sur le thread principal : c'est de la
                # geometrie de vue, cocoa n'aime pas qu'on la consulte ailleurs.
                # elle sert toujours a choisir l'ecran (celui qui porte l'icone)
                # mais ne commande la position que pour le panneau — la scene de
                # demarrage, elle, se centre.
                anchor = _menu_bar_anchor()
                # avec deux ecrans, l'ancre et la fenetre ne vivaient pas sur le
                # meme : on prenait le x de l'icone (ecran du haut) et le cadre
                # de l'ecran ou trainait la fenetre (ecran du bas), et ava
                # atterrissait hors champ. l'ancre commande : on se cale sur
                # l'ecran qui porte vraiment l'icone.
                screen = (_screen_holding(anchor) if anchor is not None else None)
                screen = screen or native.screen() or AppKit.NSScreen.mainScreen()
                visible = screen.visibleFrame()
                if DEBUG:
                    print(f"[overlay] placement {placement} ancre={anchor} "
                          f"ecran=({visible.origin.x:.0f},{visible.origin.y:.0f} "
                          f"{visible.size.width:.0f}x{visible.size.height:.0f})")
                frame = native.frame()
                frame.size.width = width
                frame.size.height = height
                frame.origin.x, frame.origin.y = _layout_origin(
                    visible.origin.x,
                    visible.origin.y,
                    visible.size.width,
                    visible.size.height,
                    width,
                    height,
                    placement,
                    anchor if placement != "center" else None,
                )
                native.setFrame_display_(frame, True)
                if placement == "center":
                    native.makeKeyAndOrderFront_(None)
            except Exception:
                pass

        AppHelper.callAfter(apply_native_frame)
        return

    # Fallback portable pour les tests ou un backend pywebview non-Cocoa.
    sw, sh = _screen_size()
    _window.resize(width, height)
    if placement == "center":
        _window.move(max(0, (sw - width) // 2), max(TOP, (sh - height) // 2))
    else:
        _window.move(max(0, sw - width - MARGIN), TOP)


def start(html_path: str) -> None:
    global _window, _target_position
    # avant toute fenetre : ava doit deja etre une extension. bascule apres
    # coup, cocoa masquerait la fenetre qu'on vient de creer.
    try:
        from ava.ui.menubar import set_accessory_policy
        set_accessory_policy()
    except Exception:  # noqa: BLE001 - hors macos on continue sans
        pass
    sw, sh = _screen_size()
    x = max(0, sw - WIDTH - MARGIN)
    _target_position = (x, TOP)
    has_startup = bool(_startup_payload)
    initial_width = START_WIDTH if has_startup else WIDTH
    initial_height = START_HEIGHT if has_startup else HEIGHT

    def _create():
        # PAS de x/y ici : donner une position a la creation declenche un
        # windowDidMove alors que la fenetre n'est pas encore rattachee a un
        # ecran -> crash cocoa "'NoneType' object has no attribute 'frame'".
        # on la deplace apres coup, une fois affichee (voir _on_loaded).
        return webview.create_window(
            "Ava", html_path,
            width=initial_width, height=initial_height,
            # easy_drag : on peut attraper la fenetre pour la deplacer (les champs
            # de saisie restent exclus, donc taper/selectionner marche toujours).
            frameless=True, easy_drag=True, on_top=True,
            transparent=True, background_color="#000000",
            resizable=False, focus=True, js_api=_Api(),
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
        # la barre de menus d'abord : le placement du panneau se cale sur
        # l'icone, donc elle doit exister avant qu'on positionne la fenetre.
        if _ready_handler is not None:
            try:
                _ready_handler()
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay] preparation interrompue : {exc}")
        # La scene de demarrage nait au centre. Le mini-plugin rejoint ensuite
        # son emplacement discret en haut a droite.
        try:
            _place_window(
                START_WIDTH if has_startup else WIDTH,
                START_HEIGHT if has_startup else HEIGHT,
                "center" if has_startup else "top_right",
            )
        except Exception:
            pass
        # fond transparent + on prend la main (annule la demo).
        _eval("window.avaOverlayMode && avaOverlayMode()")
        # Le mini-plugin est le point d'entree principal : visible et pret a
        # recevoir du texte des le lancement, sauf choix explicite contraire.
        start_hidden = False
        ui_settings = {}
        try:
            from ava.config import STORE
            ui_settings = STORE.snapshot().get("ui", {})
            start_hidden = bool(ui_settings.get("start_hidden"))
        except Exception:
            pass
        _eval(f"window.avaReady && avaReady({_j(ui_settings)})")
        if start_hidden:
            finish_startup()
            _eval("window.avaDormant && avaDormant()")
        elif has_startup and _startup_payload:
            _eval(f"window.avaStartup && avaStartup({_j(_startup_payload)})")
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

def prepare_startup(payload: dict) -> None:
    """Memorise la scene avant la creation de la fenetre."""
    global _startup_payload
    _startup_payload = dict(payload or {})


def startup(payload: dict) -> None:
    global _startup_payload
    _startup_payload = dict(payload or {})
    _place_window(START_WIDTH, START_HEIGHT, "center")
    _eval(f"window.avaStartup && avaStartup({_j(_startup_payload)})")


def startup_brief(text: str, ms: int = 0, delays=None) -> None:
    # revele le transcript du briefing en le calant sur la parole : `delays`
    # porte l'instant de chaque mot, mesure sur l'audio reellement genere.
    marks = [int(v) for v in (delays or [])][:400]
    _eval(f"window.avaStartupBrief && avaStartupBrief({_j(text)}, {int(ms)}, {_j(marks)})")


def finish_startup() -> None:
    global _startup_payload
    _place_window(WIDTH, HEIGHT, "top_right")
    _eval("window.avaStartupDone && avaStartupDone()")
    _startup_payload = None

# --- le panneau s'ouvre et se ferme depuis la barre de menus -----------------

def panel_visible() -> bool:
    return _panel_visible


def set_panel_visible(visible: bool) -> bool:
    """Sort la fenetre de l'ecran, ou la ramene sous son icone.

    On retire vraiment la fenetre (`orderOut`) plutot que de vider son contenu :
    tant qu'elle est la, elle intercepte les clics et reste dans le selecteur de
    fenetres. « on peut l'enlever quand on veut » veut dire qu'elle disparait.
    """
    global _panel_visible
    _panel_visible = bool(visible)
    if _window is None:
        return _panel_visible
    native = getattr(_window, "native", None)
    if native is None or AppKit is None or AppHelper is None:
        # backend non-cocoa (tests) : on se contente de l'etat logique.
        return _panel_visible

    def apply() -> None:
        try:
            if _panel_visible:
                # on la replace avant de la montrer : l'icone a pu bouger (ecran
                # externe branche, extension ajoutee a cote) pendant l'absence.
                _place_window(WIDTH, HEIGHT, "top_right")
                native.orderFrontRegardless()
            else:
                native.orderOut_(None)
        except Exception:
            pass

    AppHelper.callAfter(apply)
    return _panel_visible


def toggle_panel() -> bool:
    return set_panel_visible(not _panel_visible)


def open_settings() -> None:
    """Ramene le panneau et deroule les reglages (entree du menu)."""
    set_panel_visible(True)
    _eval("window.avaOpenSettings && avaOpenSettings()")


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


def hide() -> None:
    """Cache temporairement le panneau, notamment avant une capture d'ecran."""
    _eval("window.avaHide && avaHide()")


def show() -> None:
    _eval("window.avaShow && avaShow()")


def transcript(text: str, final: bool = False) -> None:
    _eval(f"window.avaTranscript && avaTranscript({_j(text)}, {str(bool(final)).lower()})")


def message(role: str, text: str, illustration: str = "", delays=None) -> None:
    # `delays` : instant de chaque mot, mesure sur la voix generee, pour que la
    # bulle se remplisse au rythme de la parole plutot qu'a cadence fixe.
    marks = [int(v) for v in (delays or [])][:400]
    _eval(
        f"window.avaMessage && avaMessage({_j(role)}, {_j(text)}, "
        f"{_j(illustration)}, {_j(marks)})"
    )


def sources(items) -> None:
    # these urls come off the open web and end up as the href of a chip in the
    # panel. anything that isn't plain http(s) — javascript:, file:, data: —
    # is dropped here rather than trusted to the click handler downstream.
    safe = []
    for item in list(items or [])[:5]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))[:1200]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        safe.append({"title": str(item.get("title", ""))[:160], "url": url})
    _eval(f"window.avaSources && avaSources({_j(safe)})")


def preview(path: str, caption: str = "Capture analysee localement") -> None:
    try:
        image_path = Path(path)
        if not image_path.is_file() or image_path.stat().st_size > 12_000_000:
            return
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data_url = f"data:{mime};base64,{encoded}"
        _eval(f"window.avaPreview && avaPreview({_j(data_url)}, {_j(caption)})")
    except OSError:
        pass


def choices(prompt: str, yes_label: str = "Oui", no_label: str = "Non") -> None:
    _eval(
        f"window.avaChoices && avaChoices({_j(prompt)}, {_j(yes_label)}, {_j(no_label)})"
    )


def clear_choices() -> None:
    _eval("window.avaClearChoices && avaClearChoices()")


def interrupted() -> None:
    """Marque la derniere bulle comme coupee en cours de phrase.

    Sans ce signal, le texte reste affiche en entier alors qu'Ava s'est tue au
    milieu : on lit une reponse complete qu'on n'a jamais entendue.
    """
    _eval("window.avaInterrupted && avaInterrupted()")


def level(value: float) -> None:
    safe = max(0.0, min(1.0, float(value)))
    _eval(f"window.avaLevel && avaLevel({safe:.3f})")


def error(message: str) -> None:
    _eval(f"window.avaError && avaError({_j(message)})")


def boot(apps, sub=None) -> None:
    sub_js = _j(sub) if sub else "null"
    _eval(f"window.avaBoot && avaBoot({_j(list(apps))},{sub_js})")


def boot_step(i: int) -> None:
    _eval(f"window.avaBootStep && avaBootStep({int(i)})")


def boot_done() -> None:
    _eval("window.avaBootDone && avaBootDone()")
