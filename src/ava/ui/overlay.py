# the small transparent always-on-top window (top right) that shows ava's orb.
# driven from the backend through evaluate_js.
# must run on the MAIN THREAD (macos/webkit rule): call start() last from
# main(), and keep the rest of the program on threads.

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
except Exception:  # pragma: no cover - ava targets macos; plain pywebview otherwise
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
TOP = 38  # just under the macos menu bar
ANCHOR_GAP = 6  # the sliver of daylight between the icon and the panel
_target_position = (0, TOP)
_panel_visible = True


class _Api:
    """the bridge the settings panel's js talks to -> the config store."""

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

    # --- google connector (calendar) --------------------------------------
    # begin_connect returns immediately and works in the background: the panel
    # just polls google_status() once a second.

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
        """Save the voice settings, then play a sample of them.

        Returns immediately: local synthesis takes a few seconds and the panel
        must not freeze while it happens.
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
    """Wire the panel's actions up without importing ``app`` back the other way.

    `ready_handler` fires once the window is on screen: that's the moment the
    menu bar can be installed, and not before — the cocoa application doesn't
    exist yet.
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
    """Work out a cocoa origin inside the visible area of the current screen.

    `anchor` carries the frame of the menu bar icon. When it's there, the panel
    drops **under its own icon** rather than into the corner of the screen —
    that's the difference between an extension and a window someone left lying
    around.
    """
    if placement == "center":
        x = screen_x + (screen_width - width) / 2
        y = screen_y + (screen_height - height) / 2
    elif anchor is not None:
        anchor_x, anchor_y, anchor_width, _anchor_height = anchor
        x = anchor_x + anchor_width / 2 - width / 2
        y = anchor_y - height - ANCHOR_GAP
        # the icon can sit right on the edge (or behind the notch): pull the
        # panel back into view without unsticking it from its icon.
        x = max(screen_x + MARGIN, min(x, screen_x + screen_width - width - MARGIN))
        y = max(screen_y + MARGIN, min(y, screen_y + screen_height - height))
    else:
        x = screen_x + screen_width - width - MARGIN
        y = screen_y + screen_height - height - MARGIN
    return round(x), round(y)


def _menu_bar_anchor():
    """The frame of ava's icon, if the menu bar is actually up."""
    try:
        from ava.ui.menubar import MENU_BAR
        return MENU_BAR.anchor()
    except Exception:  # noqa: BLE001 - no menu bar, so we keep the corner
        return None


def _screen_holding(anchor):
    """The screen holding the centre of the anchor, if there is one."""
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
    """Resize and place Ava on the screen that actually holds her.

    The old Finder-based maths mixed desktop coordinates with pywebview's, which
    fell apart on Retina and on multiple displays. Cocoa hands us the visible
    area and its origin directly, so the frame goes on in one operation on the
    main thread.
    """
    if _window is None:
        return
    native = getattr(_window, "native", None)
    if native is not None and AppKit is not None and AppHelper is not None:
        def apply_native_frame() -> None:
            try:
                # the anchor is read here, on the main thread: it's view
                # geometry, and cocoa dislikes being asked anywhere else. it
                # still picks the screen (the one holding the icon) but only
                # drives the position for the panel — the startup scene centres
                # itself.
                anchor = _menu_bar_anchor()
                # with two displays the anchor and the window didn't live on
                # the same one: we took the icon's x (top screen) and the frame
                # of wherever the window happened to be (bottom screen), and ava
                # landed off-screen. the anchor decides — we follow the display
                # that actually carries the icon.
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

    # portable fallback, for tests or a non-cocoa pywebview backend.
    sw, sh = _screen_size()
    _window.resize(width, height)
    if placement == "center":
        _window.move(max(0, (sw - width) // 2), max(TOP, (sh - height) // 2))
    else:
        _window.move(max(0, sw - width - MARGIN), TOP)


def start(html_path: str) -> None:
    global _window, _target_position
    # before any window exists: ava has to already be an extension. switching
    # afterwards, cocoa would hide the window we just made.
    try:
        from ava.ui.menubar import set_accessory_policy
        set_accessory_policy()
    except Exception:  # noqa: BLE001 - off macos we just carry on without
        pass
    sw, sh = _screen_size()
    x = max(0, sw - WIDTH - MARGIN)
    _target_position = (x, TOP)
    has_startup = bool(_startup_payload)
    initial_width = START_WIDTH if has_startup else WIDTH
    initial_height = START_HEIGHT if has_startup else HEIGHT

    def _create():
        # NO x/y here: giving a position at creation fires a windowDidMove
        # while the window isn't attached to a screen yet -> cocoa crashes with
        # "'NoneType' object has no attribute 'frame'". we move it afterwards,
        # once it's on screen (see _on_loaded).
        return webview.create_window(
            "Ava", html_path,
            width=initial_width, height=initial_height,
            # easy_drag: you can grab the window to move it (input fields are
            # excluded, so typing and selecting still work).
            frameless=True, easy_drag=True, on_top=True,
            transparent=True, background_color="#000000",
            resizable=False, focus=True, js_api=_Api(),
        )

    # quick restart: a pywebview window that was just killed can leave
    # NSScreen.mainScreen() at None for a moment ("NoneType frame"). small delay
    # and a retry, so we don't fall back to headless mode for nothing.
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
        raise last_exc if last_exc else RuntimeError("no overlay window")

    def _on_loaded():
        # menu bar first: the panel's placement keys off the icon, so it has to
        # exist before we position the window.
        if _ready_handler is not None:
            try:
                _ready_handler()
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay] preparation interrompue : {exc}")
        # the startup scene is born in the middle. the panel then goes off to
        # its quiet spot in the top right corner.
        try:
            _place_window(
                START_WIDTH if has_startup else WIDTH,
                START_HEIGHT if has_startup else HEIGHT,
                "center" if has_startup else "top_right",
            )
        except Exception:
            pass
        # transparent background, and we take over (cancels the demo).
        _eval("window.avaOverlayMode && avaOverlayMode()")
        # the panel is the main way in: visible and ready to take text from
        # launch, unless you explicitly asked otherwise.
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
    # same guard on starting the gui loop: the screen may not be ready right
    # after a previous instance died.
    for attempt in range(3):
        try:
            webview.start()  # blocks the main thread until the window closes
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0)
    raise last_exc if last_exc else RuntimeError("overlay gui would not start")


def _eval(code: str) -> None:
    if _window is None:
        return
    try:
        _window.evaluate_js(code)
    except Exception:
        pass


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=True)


# --- the api the backend calls ---------------------------------------------

def prepare_startup(payload: dict) -> None:
    """Remember the scene before the window is created."""
    global _startup_payload
    _startup_payload = dict(payload or {})


def startup(payload: dict) -> None:
    global _startup_payload
    _startup_payload = dict(payload or {})
    _place_window(START_WIDTH, START_HEIGHT, "center")
    _eval(f"window.avaStartup && avaStartup({_j(_startup_payload)})")


def startup_brief(text: str, ms: int = 0, delays=None) -> None:
    # reveal the briefing transcript in step with the speech: `delays` carries
    # the moment of each word, measured on the audio actually generated.
    marks = [int(v) for v in (delays or [])][:400]
    _eval(f"window.avaStartupBrief && avaStartupBrief({_j(text)}, {int(ms)}, {_j(marks)})")


def finish_startup() -> None:
    global _startup_payload
    _place_window(WIDTH, HEIGHT, "top_right")
    _eval("window.avaStartupDone && avaStartupDone()")
    _startup_payload = None

# --- the panel opens and closes from the menu bar ---------------------------

def panel_visible() -> bool:
    return _panel_visible


def set_panel_visible(visible: bool) -> bool:
    """Take the window off the screen, or bring it back under its icon.

    We really remove the window (`orderOut`) rather than emptying it: while it's
    there it swallows clicks and stays in the window switcher. "you can get rid
    of it whenever you want" has to mean it actually goes away.
    """
    global _panel_visible
    _panel_visible = bool(visible)
    if _window is None:
        return _panel_visible
    native = getattr(_window, "native", None)
    if native is None or AppKit is None or AppHelper is None:
        # non-cocoa backend (tests): the logical state is all we track.
        return _panel_visible

    def apply() -> None:
        try:
            if _panel_visible:
                # reposition before showing: the icon may have moved while we
                # were away (external display plugged in, another extension
                # added next to it).
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
    """Bring the panel back and open the settings (menu entry)."""
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
    # "off": hides the pill and the startup card entirely. any other state
    # (set_state/boot) wakes them back up.
    _eval("window.avaDormant && avaDormant()")


def hide() -> None:
    """Hide the panel for a moment — before a screenshot, mostly."""
    _eval("window.avaHide && avaHide()")


def show() -> None:
    _eval("window.avaShow && avaShow()")


def transcript(text: str, final: bool = False) -> None:
    _eval(f"window.avaTranscript && avaTranscript({_j(text)}, {str(bool(final)).lower()})")


def message(role: str, text: str, illustration: str = "", delays=None) -> None:
    # `delays`: the moment of each word, measured on the generated voice, so the
    # bubble fills at the pace of the speech rather than at a fixed rate.
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
    """Mark the last bubble as cut off mid-sentence.

    Without this, the text stays on screen in full while Ava stopped halfway
    through — you end up reading a complete answer you never heard.
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
