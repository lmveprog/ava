"""lance une session de focus dans promethee depuis ava.

promethee est une app electron sans cli ni url scheme utile (seul
promethee://auth/callback existe), donc on passe par l'accessibilite macos :
on force chromium a exposer son arbre ax, on retrouve le bouton "lancer la
session" et on l'active. c'est bien plus fiable qu'un clic aux coordonnees,
la fenetre pouvant etre sur n'importe quel ecran.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess
import time

BUNDLE_ID = "app.promethee"
APP_NAME = "Promethee"
DB_PATH = Path.home() / "Library/Application Support/Promethee/promethee.db"

# libelles du bouton de demarrage, l'app suit la langue du systeme.
START_LABELS = {
    "lancer la session", "lancer une session", "lancer une session de focus",
    "start session", "start a session", "start focus session",
}


@dataclass(frozen=True)
class SessionReply:
    ok: bool
    text: str
    task: str = ""
    already: bool = False


def _frameworks():
    # import paresseux : ava doit rester lancable sur une machine sans pyobjc.
    import ApplicationServices as ax
    from AppKit import NSWorkspace
    return ax, NSWorkspace


def active_session() -> dict | None:
    """La session en cours d'apres la base locale de promethee, si elle existe."""
    if not DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "select id, task, started_at from sessions "
                "where ended_at is null and deleted = 0 "
                "order by started_at desc limit 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {"id": row[0], "task": row[1] or "", "started_at": row[2]}


def _pid() -> int | None:
    _, NSWorkspace = _frameworks()
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == BUNDLE_ID:
            return int(app.processIdentifier())
    return None


def _launch_and_wait(timeout: float = 25.0) -> int | None:
    pid = _pid()
    if pid:
        return pid
    subprocess.run(["open", "-g", "-b", BUNDLE_ID], check=False, capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        pid = _pid()
        if pid:
            # l'interface met encore un instant a se peindre
            time.sleep(2.0)
            return pid
    return None


def _attr(ax, element, name):
    err, value = ax.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _find_button(ax, element, labels, budget, depth=0):
    """Descend l'arbre ax a la recherche d'un bouton portant un de ces libelles."""
    if budget[0] <= 0 or depth > 40:
        return None
    budget[0] -= 1
    if _attr(ax, element, "AXRole") == "AXButton":
        for key in ("AXTitle", "AXDescription", "AXValue"):
            value = _attr(ax, element, key)
            if value and str(value).strip().lower() in labels:
                return element
        return None
    for child in _attr(ax, element, "AXChildren") or []:
        hit = _find_button(ax, child, labels, budget, depth + 1)
        if hit is not None:
            return hit
    return None


def _show_dashboard() -> None:
    """Promethee vit dans la barre de menus : sans fenetre, pas d'arbre a fouiller."""
    script = f'''
    tell application "System Events" to tell application process "{APP_NAME}"
      set extras to menu bar 2
      click menu bar item 1 of extras
      delay 0.4
      repeat with item_ in (menu items of menu 1 of menu bar item 1 of extras)
        try
          set label_ to name of item_
          if label_ contains "tableau de bord" or label_ contains "Dashboard" then
            click item_
            return "ok"
          end if
        end try
      end repeat
      key code 53
      return "absent"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=20)
    time.sleep(1.5)


def start_session(*, wait_confirm: float = 6.0) -> SessionReply:
    """Demarre une session de focus. Ne fait rien si une session tourne deja."""
    running = active_session()
    if running:
        task = running["task"]
        detail = f' « {task} »' if task else ""
        return SessionReply(True, f"Une session Prométhée{detail} tourne déjà, je la laisse courir.",
                            task=task, already=True)

    try:
        ax, _ = _frameworks()
    except Exception:  # noqa: BLE001 - pyobjc absent
        return SessionReply(False, "Je n'arrive pas à piloter Prométhée : le module d'accessibilité manque.")

    pid = _launch_and_wait()
    if not pid:
        return SessionReply(False, "Je n'ai pas réussi à ouvrir Prométhée.")

    app = ax.AXUIElementCreateApplication(pid)
    # sans ce drapeau, chromium n'expose qu'une coquille vide a l'accessibilite.
    ax.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    time.sleep(0.8)

    button = None
    for attempt in range(3):
        windows = _attr(ax, app, "AXWindows") or []
        if not windows:
            _show_dashboard()
            continue
        budget = [40000]
        for window in windows:
            button = _find_button(ax, window, START_LABELS, budget)
            if button is not None:
                break
        if button is not None:
            break
        # la fenetre existe mais le bouton n'y est pas encore (ou un panneau
        # le recouvre) : on laisse une chance au rendu.
        time.sleep(1.0 + attempt)

    if button is None:
        return SessionReply(False, "Prométhée est ouvert mais je ne trouve pas le bouton de démarrage.")

    if ax.AXUIElementPerformAction(button, "AXPress") != 0:
        return SessionReply(False, "Le bouton de session Prométhée n'a pas répondu.")

    deadline = time.time() + wait_confirm
    while time.time() < deadline:
        time.sleep(0.4)
        started = active_session()
        if started:
            task = started["task"]
            detail = f' « {task} »' if task else ""
            return SessionReply(True, f"Session Prométhée{detail} lancée.", task=task)

    # le clic est parti, la base n'a pas encore ete ecrite : on reste positif
    # sans mentir sur la certitude.
    return SessionReply(True, "J'ai lancé ta session Prométhée.")


def session_sentence() -> str:
    """Phrase courte a glisser dans le briefing du matin."""
    reply = start_session()
    if not reply.ok:
        return ""
    if reply.already:
        return "Ta session Prométhée tourne déjà."
    if reply.task:
        return f"Je te lance une session Prométhée : {reply.task}."
    return "Je te lance une session Prométhée."


if __name__ == "__main__":
    print(start_session())
