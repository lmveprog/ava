"""ava in the menu bar, like any other macos extension.

ava used to be a pywebview window sitting on the desktop: she looked like a
python script somebody had started by hand, and there was no obvious gesture for
getting rid of her. now she's an `NSStatusItem` up in the top right next to the
others — the icon says what she's doing, left click opens and closes the panel,
right click gives the menu.

two details make all the difference:

- **the "accessory" activation policy**: no dock icon, no app name in the menu
  bar when the window has focus. that's what `LSUIElement` does in a real .app
  bundle, except we don't have one here — so we ask for it at runtime.
- **the icon follows the state** (idle, listening, thinking, speaking): it's the
  only visual feedback left once the panel is closed.

everything goes through `AppHelper.callAfter`: cocoa only lets you touch the ui
from the main thread, and ava calls these functions from her audio threads.
"""

from __future__ import annotations

from typing import Callable

try:
    import AppKit
    import objc
    from PyObjCTools import AppHelper
except Exception:  # pragma: no cover - off macos there is no menu bar
    AppKit = None
    objc = None
    AppHelper = None


# the icon speaks for the panel while it's closed. we stay on system symbols:
# they adapt on their own to light/dark, to the bar's height and to "reduce
# motion".
STATE_SYMBOLS = {
    "dormant": "moon.zzz",
    "idle": "waveform.circle",
    "listening": "waveform.circle.fill",
    "thinking": "ellipsis.circle",
    "action": "bolt.horizontal.circle",
    "speaking": "speaker.wave.2.circle.fill",
    "booting": "circle.dotted",
    "error": "exclamationmark.circle",
}
FALLBACK_SYMBOL = "waveform.circle"

STATE_LABELS = {
    "dormant": "En veille",
    "idle": "Prête",
    "listening": "Elle écoute",
    "thinking": "Elle réfléchit",
    "action": "Elle agit",
    "speaking": "Elle parle",
    "booting": "Démarrage",
    "error": "Souci",
}


def available() -> bool:
    return AppKit is not None and AppHelper is not None


_policy_set = False


def set_accessory_policy() -> bool:
    """Turn ava into an extension: no dock icon, no application menu.

    **Call this before creating the window.** Cocoa hides windows that are
    already open when you go from "regular" to "accessory", so applying it
    afterwards made ava's panel vanish without so much as an error. It's the
    runtime equivalent of `LSUIElement` in a real .app bundle, which we don't
    have here.
    """
    global _policy_set
    if not available() or _policy_set:
        return _policy_set
    try:
        application = AppKit.NSApplication.sharedApplication()
        application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        _policy_set = True
    except Exception as exc:  # noqa: BLE001
        print(f"[barre de menus] politique d'activation refusée : {exc}")
    return _policy_set


# --- the objective-c target for the menu items --------------------------------
# pyobjc needs a real objc object to receive the actions. we hand it a dict of
# python callables, which keeps this file from having to import ava.

if available():

    class _AvaMenuTarget(AppKit.NSObject):
        def initWithActions_(self, actions):
            self = objc.super(_AvaMenuTarget, self).init()
            if self is None:
                return None
            self._actions = dict(actions or {})
            return self

        @objc.python_method
        def _run(self, name):
            handler = self._actions.get(name)
            if handler is None:
                return
            try:
                handler()
            except Exception as exc:  # noqa: BLE001 - un menu ne doit jamais tuer ava
                print(f"[barre de menus] action « {name} » en échec : {exc}")

        def statusClicked_(self, sender):
            # right click (or ctrl+click) = menu, left click = open/close.
            event = AppKit.NSApp.currentEvent()
            right = False
            if event is not None:
                right = (event.type() == AppKit.NSEventTypeRightMouseUp
                         or bool(event.modifierFlags() & AppKit.NSEventModifierFlagControl))
            self._run("menu" if right else "toggle")

        def togglePanel_(self, sender):
            self._run("toggle")

        def startListening_(self, sender):
            self._run("listen")

        def togglePause_(self, sender):
            self._run("pause")

        def hushAva_(self, sender):
            self._run("hush")

        def openSettings_(self, sender):
            self._run("settings")

        def quitAva_(self, sender):
            self._run("quit")

else:  # pragma: no cover
    _AvaMenuTarget = None


class MenuBar:
    """The status item and its context menu."""

    def __init__(self) -> None:
        self._item = None
        self._target = None
        self._menu = None
        self._status_item = None      # la ligne « Ava · Prête », desactivee
        self._pause_item = None
        self._toggle_item = None
        self._state = "idle"
        self._paused = False
        self._panel_open = True

    # --- installation ---------------------------------------------------------

    def install(self, actions: dict[str, Callable[[], object]]) -> bool:
        """Create the status item. Must be called from the main thread.

        Returns False off macos: ava then carries on without a menu bar rather
        than refusing to start.
        """
        if not available():
            return False
        payload = dict(actions or {})
        if AppKit.NSThread.isMainThread():
            self._install_now(payload)
            return True
        # pywebview fires its `loaded` event from a thread of its own: without
        # waiting, the caller would place the panel before the icon exists, so
        # with no anchor — and ava landed on the wrong screen. the cocoa loop is
        # already running (webview.start), so waiting here is safe.
        import threading
        done = threading.Event()

        def run() -> None:
            try:
                self._install_now(payload)
            finally:
                done.set()

        AppHelper.callAfter(run)
        if not done.wait(timeout=5.0):
            print("[barre de menus] installation lente : le panneau se placera sans ancre")
        return True

    def _install_now(self, actions: dict) -> None:
        try:
            set_accessory_policy()
            self._target = _AvaMenuTarget.alloc().initWithActions_(actions)
            bar = AppKit.NSStatusBar.systemStatusBar()
            self._item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)

            button = self._item.button()
            button.setTarget_(self._target)
            button.setAction_("statusClicked:")
            button.sendActionOn_(
                AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp)
            button.setToolTip_("Ava")

            self._menu = self._build_menu()
            self._apply_state(self._state)
        except Exception as exc:  # noqa: BLE001
            print(f"[barre de menus] installation impossible : {exc}")
            self._item = None

    def _build_menu(self):
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        self._status_item = self._add(menu, "Ava", None)
        self._status_item.setEnabled_(False)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._add(menu, "Parler à Ava", "startListening:")
        # enough to cut a thirty-second briefing short without killing ava.
        self._add(menu, "La faire taire", "hushAva:", key="."),
        self._toggle_item = self._add(menu, "Masquer le panneau", "togglePanel:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._pause_item = self._add(menu, "Mettre l'écoute en pause", "togglePause:")
        self._add(menu, "Réglages…", "openSettings:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._add(menu, "Quitter Ava", "quitAva:", key="q")
        return menu

    def _add(self, menu, title: str, selector: str | None, key: str = ""):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, selector, key)
        if selector:
            item.setTarget_(self._target)
        menu.addItem_(item)
        return item

    # --- driven from ava ------------------------------------------------------

    def show_menu(self) -> None:
        """Drop the menu down under the icon (right click)."""
        if self._item is None or self._menu is None:
            return
        # attach the menu for the duration of the click only: otherwise a left
        # click would open the menu instead of calling our action.
        self._item.setMenu_(self._menu)
        self._item.button().performClick_(None)
        self._item.setMenu_(None)

    def set_state(self, state: str) -> None:
        if self._item is None:
            self._state = state
            return
        AppHelper.callAfter(self._apply_state, state)

    def _apply_state(self, state: str) -> None:
        self._state = str(state or "idle")
        if self._item is None:
            return
        try:
            symbol = STATE_SYMBOLS.get(self._state, FALLBACK_SYMBOL)
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol, "Ava")
            if image is None:
                image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    FALLBACK_SYMBOL, "Ava")
            if image is not None:
                # "template": macos recolours the icon against the bar's
                # background, so it stays readable in light and in dark.
                image.setTemplate_(True)
                self._item.button().setImage_(image)
            label = STATE_LABELS.get(self._state, "Prête")
            if self._paused:
                label = "Écoute en pause"
            self._item.button().setToolTip_(f"Ava — {label}")
            if self._status_item is not None:
                self._status_item.setTitle_(f"Ava · {label}")
        except Exception as exc:  # noqa: BLE001
            print(f"[barre de menus] icône non appliquée : {exc}")

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._item is None:
            return
        AppHelper.callAfter(self._apply_paused)

    def _apply_paused(self) -> None:
        if self._pause_item is not None:
            self._pause_item.setTitle_(
                "Reprendre l'écoute" if self._paused else "Mettre l'écoute en pause")
            self._pause_item.setState_(
                AppKit.NSControlStateValueOn if self._paused
                else AppKit.NSControlStateValueOff)
        self._apply_state(self._state)

    def set_panel_open(self, open_: bool) -> None:
        self._panel_open = bool(open_)
        if self._item is None:
            return
        AppHelper.callAfter(self._apply_panel_open)

    def _apply_panel_open(self) -> None:
        if self._toggle_item is not None:
            self._toggle_item.setTitle_(
                "Masquer le panneau" if self._panel_open else "Afficher le panneau")

    def anchor(self) -> tuple[float, float, float, float] | None:
        """The icon's frame in screen coordinates, to hang the panel under it.

        This is what makes ava drop *under her own icon* rather than into some
        arbitrary corner, even on an external display or around a notch.
        """
        if self._item is None:
            return None
        try:
            button = self._item.button()
            window = button.window()
            if window is None:
                return None
            frame = window.convertRectToScreen_(button.bounds())
            # right after creation the bar hasn't laid the icon out yet and
            # returns (0, 0). a menu bar icon is never at the bottom left of the
            # main screen, so we'd rather admit we don't know yet than send the
            # panel somewhere wrong.
            if frame.origin.x == 0 and frame.origin.y == 0:
                return None
            return (frame.origin.x, frame.origin.y,
                    frame.size.width, frame.size.height)
        except Exception:  # noqa: BLE001
            return None


MENU_BAR = MenuBar()


def quit_ava() -> None:
    """Stop ava until the next login.

    Exiting cleanly is enough: the launch agent is set to
    `KeepAlive = {SuccessfulExit: false}`, so an exit code of 0 doesn't trigger
    a relaunch, while a crash still does.

    We very deliberately do **not** `launchctl bootout` here: that leaves the
    service marked "disabled" in the user domain, and ava then never came back
    at the next login — it took a manual `launchctl enable` to revive her.
    """
    if available():
        AppKit.NSApp.terminate_(None)
    else:  # pragma: no cover
        raise SystemExit(0)
