"""ava dans la barre de menus, comme n'importe quelle autre extension macos.

avant, ava etait une fenetre pywebview posee sur le bureau : elle ressemblait a
un script python lance a la main, et il n'y avait aucun geste evident pour s'en
debarrasser. desormais c'est un `NSStatusItem` en haut a droite, a cote de
prometheus et des autres : l'icone dit ce qu'ava est en train de faire, le clic
gauche ouvre et referme le panneau, le clic droit donne le menu.

deux details qui font toute la difference :

- **politique d'activation « accessory »** : plus d'icone dans le dock, plus de
  nom dans la barre de menus quand la fenetre a le focus. c'est ce que fait un
  `LSUIElement` dans un vrai bundle .app, sauf qu'ici on n'en a pas — on le
  demande a l'execution.
- **l'icone suit l'etat** (au repos, ecoute, reflexion, parole) : c'est le seul
  retour visuel qui reste quand le panneau est ferme.

tout passe par `AppHelper.callAfter` : cocoa n'accepte de toucher a l'interface
que depuis le thread principal, et ava appelle ces fonctions depuis ses threads
audio.
"""

from __future__ import annotations

from typing import Callable

try:
    import AppKit
    import objc
    from PyObjCTools import AppHelper
except Exception:  # pragma: no cover - hors macos, la barre de menus n'existe pas
    AppKit = None
    objc = None
    AppHelper = None


# l'icone parle a la place du panneau quand il est ferme. on reste sur des
# symboles systeme : ils s'adaptent tout seuls au theme clair/sombre, a la
# taille de la barre et au mode « reduire les animations ».
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
    """Fait d'ava une extension : ni icone dans le dock, ni menu applicatif.

    **A appeler avant de creer la fenetre.** Cocoa masque les fenetres deja
    ouvertes quand on passe de « regular » a « accessory » : appliquee apres
    coup, la bascule faisait disparaitre le panneau d'ava sans le moindre
    message d'erreur. C'est l'equivalent a l'execution du `LSUIElement` d'un
    vrai bundle .app, qu'on n'a pas ici.
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


# --- la cible objective-c des elements de menu --------------------------------
# pyobjc exige un vrai objet objc pour recevoir les actions. on lui passe un
# dictionnaire de callables python, ce qui evite d'importer ava ici.

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
            # clic droit (ou ctrl+clic) = menu, clic gauche = ouvrir/fermer.
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
    """L'element de barre de menus et son menu contextuel."""

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
        """Cree l'element de barre de menus. A appeler depuis le thread principal.

        Renvoie False hors macos : ava continue alors sans barre de menus plutot
        que de refuser de demarrer.
        """
        if not available():
            return False
        payload = dict(actions or {})
        if AppKit.NSThread.isMainThread():
            self._install_now(payload)
            return True
        # pywebview declenche son evenement `loaded` depuis un thread a lui :
        # sans attendre, l'appelant placerait le panneau avant que l'icone
        # existe, donc sans ancre — et ava atterrissait sur le mauvais ecran.
        # la boucle cocoa tourne deja (webview.start), l'attente est sure.
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
        # de quoi couper un briefing de trente secondes sans tuer ava.
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

    # --- pilotage depuis ava --------------------------------------------------

    def show_menu(self) -> None:
        """Deroule le menu sous l'icone (clic droit)."""
        if self._item is None or self._menu is None:
            return
        # on accroche le menu le temps du clic seulement : sinon le clic gauche
        # ouvrirait le menu au lieu d'appeler notre action.
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
                # « template » : macos recolore l'icone selon le fond de la
                # barre, donc elle reste lisible en clair comme en sombre.
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
        """Cadre de l'icone en coordonnees ecran, pour caler le panneau dessous.

        C'est ce qui fait qu'ava tombe *sous son icone* et pas dans un coin
        arbitraire, meme sur un ecran externe ou avec une encoche.
        """
        if self._item is None:
            return None
        try:
            button = self._item.button()
            window = button.window()
            if window is None:
                return None
            frame = window.convertRectToScreen_(button.bounds())
            # juste apres la creation, la barre n'a pas encore dispose l'icone et
            # renvoie (0, 0). une icone de barre de menus n'est jamais en bas a
            # gauche de l'ecran principal : on prefere avouer qu'on ne sait pas
            # encore, plutot que d'envoyer le panneau au mauvais endroit.
            if frame.origin.x == 0 and frame.origin.y == 0:
                return None
            return (frame.origin.x, frame.origin.y,
                    frame.size.width, frame.size.height)
        except Exception:  # noqa: BLE001
            return None


MENU_BAR = MenuBar()


def quit_ava() -> None:
    """Arrete ava jusqu'au prochain demarrage de session.

    Il suffit de sortir proprement : le launchagent est passe en
    `KeepAlive = {SuccessfulExit: false}`, donc un code de sortie 0 ne declenche
    pas de relance, alors qu'un plantage en declenche toujours une.

    On ne fait surtout **pas** `launchctl bootout` ici : la commande laisse le
    service marque « disabled » dans le domaine utilisateur, et ava ne repartait
    plus du tout au demarrage suivant — il fallait un `launchctl enable` a la
    main pour la ressusciter.
    """
    if available():
        AppKit.NSApp.terminate_(None)
    else:  # pragma: no cover
        raise SystemExit(0)
