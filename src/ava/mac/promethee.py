"""starting a focus session in promethee, from ava.

promethee is an electron app with no cli and no useful url scheme (only
promethee://auth/callback exists), so we go through macos accessibility: force
chromium to expose its ax tree, find the "lancer la session" button and press
it. far more reliable than clicking at coordinates, given the window could be
on any screen.
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

# labels on the start button — the app follows the system language.
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
    # lazy import: ava has to stay launchable on a machine without pyobjc.
    import ApplicationServices as ax
    from AppKit import NSWorkspace
    return ax, NSWorkspace


def active_session() -> dict | None:
    """The running session according to promethee's local db, if there is one."""
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
            # the interface still takes a moment to paint
            time.sleep(2.0)
            return pid
    return None


def _attr(ax, element, name):
    err, value = ax.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _find_button(ax, element, labels, budget, depth=0):
    """Walk the ax tree looking for a button carrying one of these labels."""
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
    """Promethee lives in the menu bar: no window means no tree to search."""
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
    """Start a focus session. Does nothing if one is already running."""
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
    # without this flag, chromium exposes nothing but an empty shell to ax.
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
        # the window is there but the button isn't yet (or a panel covers it):
        # give the render a chance.
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

    # the click went out but the db hasn't been written yet: stay positive
    # without overstating how sure we are.
    return SessionReply(True, "J'ai lancé ta session Prométhée.")


def session_sentence() -> str:
    """A short line to slip into the morning briefing."""
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
