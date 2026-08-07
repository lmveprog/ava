"""actions ordinateur macos, limitees a une liste blanche explicite."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Callable


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class ComputerIntent:
    kind: str
    value: str = ""
    target: str = ""
    summary: str = ""
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ActionOutcome:
    handled: bool
    ok: bool = False
    message: str = ""
    needs_confirmation: bool = False
    intent: ComputerIntent | None = None


_SHORTCUTS = {
    "copie": ("c", False, "Je copie la selection."),
    "copier": ("c", False, "Je copie la selection."),
    "colle": ("v", True, "Je colle le contenu du presse-papiers."),
    "coller": ("v", True, "Je colle le contenu du presse-papiers."),
    "enregistre": ("s", False, "J'enregistre."),
    "sauvegarde": ("s", False, "J'enregistre."),
    "annule la derniere action": ("z", False, "J'annule la derniere action."),
    "retablis": ("z", False, "Je retablis la derniere action."),
    "nouvel onglet": ("t", False, "J'ouvre un nouvel onglet."),
    "nouveau onglet": ("t", False, "J'ouvre un nouvel onglet."),
    "ferme l onglet": ("w", True, "Je ferme l'onglet actif."),
    "ferme la fenetre": ("w", True, "Je ferme la fenetre active."),
}


def parse_computer_intent(text: str) -> ComputerIntent | None:
    raw = text.strip()
    command = _norm(raw)
    if not command:
        return None

    if any(p in command for p in (
        "capture d ecran", "capture ecran", "screenshot", "photo de l ecran",
    )):
        return ComputerIntent(
            "screenshot", summary="Je prends une capture de l'ecran.",
        )

    type_match = re.match(
        r"^(?:ecris|tape|saisis|dicte)(?: le texte)?\s+(.+)$",
        command,
    )
    if type_match:
        # on extrait depuis la phrase originale pour conserver accents et casse.
        original = re.sub(
            r"^(?:écris|ecris|tape|saisis|dicte)(?:\s+le\s+texte)?\s+",
            "",
            raw,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        submit = bool(re.search(r"\s+(?:et\s+)?(?:envoie|valide)$", _norm(original)))
        if submit:
            original = re.sub(
                r"\s+(?:et\s+)?(?:envoie|valide)\s*$", "", original,
                flags=re.IGNORECASE,
            ).strip()
        return ComputerIntent(
            "type_and_submit" if submit else "type_text",
            value=original,
            summary=(
                "Je vais saisir ce texte puis l'envoyer."
                if submit else "Je vais saisir ce texte dans l'application active."
            ),
            # la saisie seule reste reversible ; l'envoi ne l'est pas toujours.
            requires_confirmation=submit,
        )

    click_match = re.match(
        r"^(?:clique|appuie)(?:\s+sur)?\s+(.+)$", raw, re.IGNORECASE,
    )
    if click_match and not command.startswith("appuie sur entree"):
        target = click_match.group(1).strip()
        risky = any(word in _norm(target) for word in (
            "envoyer", "publier", "payer", "acheter", "supprimer", "confirmer",
        ))
        return ComputerIntent(
            "click_named",
            target=target,
            summary=f"Je vais cliquer sur {target}.",
            requires_confirmation=risky,
        )

    for phrase, (key, risky, summary) in _SHORTCUTS.items():
        if command == phrase:
            return ComputerIntent(
                "shortcut", value=key, summary=summary,
                requires_confirmation=risky,
            )

    if command in {"valide", "entree", "appuie sur entree", "confirme avec entree"}:
        return ComputerIntent(
            "press_key", value="return", summary="J'appuie sur Entree.",
            requires_confirmation=True,
        )
    if command in {"echappe", "escape", "appuie sur echappe"}:
        return ComputerIntent("press_key", value="escape", summary="Je ferme ce menu.")
    if command in {"tabulation", "appuie sur tabulation", "champ suivant"}:
        return ComputerIntent("press_key", value="tab", summary="Je passe au champ suivant.")

    if any(p in command for p in ("descends la page", "fais defiler vers le bas", "scroll bas")):
        return ComputerIntent("scroll", value="down", summary="Je descends dans la page.")
    if any(p in command for p in ("remonte la page", "fais defiler vers le haut", "scroll haut")):
        return ComputerIntent("scroll", value="up", summary="Je remonte dans la page.")

    if command in {"minimise la fenetre", "reduis la fenetre"}:
        return ComputerIntent("minimize", summary="Je reduis la fenetre active.")
    if command in {"agrandis la fenetre", "maximise la fenetre", "plein ecran"}:
        return ComputerIntent("maximize", summary="J'agrandis la fenetre active.")

    focus_match = re.match(
        r"^(?:va sur|passe sur|mets|affiche)\s+(.+?)(?:\s+au premier plan)?$",
        command,
    )
    if focus_match:
        target = focus_match.group(1).strip()
        return ComputerIntent(
            "focus_app", target=target,
            summary=f"Je passe sur {target}.",
        )
    return None


class MacComputerController:
    """execute uniquement les actions produites par ``parse_computer_intent``."""

    _KEY_CODES = {"return": 36, "escape": 53, "tab": 48, "up": 126, "down": 125}
    _ACCESSIBILITY_ACTIONS = {
        "type_text", "type_and_submit", "press_key", "shortcut", "scroll",
        "minimize", "maximize", "click_named",
    }

    def __init__(self, screenshot_dir: Path | None = None) -> None:
        self.screenshot_dir = screenshot_dir or Path.home() / "Pictures" / "Ava"

    def _run(self, args: list[str], timeout: float = 12) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=timeout,
        )

    def _osascript(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return self._run(["osascript", "-e", script, *args])

    def accessibility_enabled(self) -> bool:
        result = self._osascript('tell application "System Events" to get UI elements enabled')
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def execute(self, intent: ComputerIntent) -> ActionOutcome:
        if sys.platform != "darwin":
            return ActionOutcome(True, False, "Computer Use est disponible uniquement sur macOS.", intent=intent)
        try:
            if intent.kind in self._ACCESSIBILITY_ACTIONS and not self.accessibility_enabled():
                return ActionOutcome(
                    True, False,
                    "Autorise Ava dans Reglages Systeme, Confidentialite et securite, Accessibilite, puis reessaie.",
                    intent=intent,
                )
            method = getattr(self, f"_do_{intent.kind}", None)
            if method is None:
                return ActionOutcome(True, False, "Cette action n'est pas encore disponible.", intent=intent)
            detail = method(intent)
            return ActionOutcome(True, True, detail or intent.summary, intent=intent)
        except subprocess.TimeoutExpired:
            return ActionOutcome(True, False, "L'action a pris trop de temps et a ete arretee.", intent=intent)
        except Exception as exc:  # noqa: BLE001
            return ActionOutcome(True, False, f"Je n'ai pas pu terminer l'action : {exc}", intent=intent)

    def _require_ok(self, result: subprocess.CompletedProcess, fallback: str) -> str:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or fallback)
        return ""

    def _do_type_text(self, intent: ComputerIntent) -> str:
        script = '''on run argv
tell application "System Events" to keystroke (item 1 of argv)
end run'''
        self._require_ok(self._osascript(script, intent.value), "saisie impossible")
        return "Texte saisi."

    def _do_type_and_submit(self, intent: ComputerIntent) -> str:
        self._do_type_text(intent)
        self._do_press_key(ComputerIntent("press_key", value="return"))
        return "Texte saisi et envoye."

    def _do_press_key(self, intent: ComputerIntent) -> str:
        code = self._KEY_CODES[intent.value]
        script = f'tell application "System Events" to key code {code}'
        self._require_ok(self._osascript(script), "touche indisponible")
        return intent.summary

    def _do_shortcut(self, intent: ComputerIntent) -> str:
        modifiers = "{command down, shift down}" if intent.value == "z" and "retabl" in _norm(intent.summary) else "{command down}"
        script = f'tell application "System Events" to keystroke "{intent.value}" using {modifiers}'
        self._require_ok(self._osascript(script), "raccourci indisponible")
        return intent.summary

    def _do_scroll(self, intent: ComputerIntent) -> str:
        code = self._KEY_CODES[intent.value]
        script = f'''tell application "System Events"
repeat 3 times
key code {code}
delay 0.06
end repeat
end tell'''
        self._require_ok(self._osascript(script), "defilement indisponible")
        return intent.summary

    def _do_focus_app(self, intent: ComputerIntent) -> str:
        result = self._run(["open", "-a", intent.target])
        self._require_ok(result, f"application {intent.target} introuvable")
        return f"{intent.target} est au premier plan."

    def _do_minimize(self, intent: ComputerIntent) -> str:
        script = '''tell application "System Events"
set frontProcess to first application process whose frontmost is true
set value of attribute "AXMinimized" of front window of frontProcess to true
end tell'''
        self._require_ok(self._osascript(script), "fenetre non reductible")
        return intent.summary

    def _do_maximize(self, intent: ComputerIntent) -> str:
        script = '''tell application "System Events"
set frontProcess to first application process whose frontmost is true
tell front window of frontProcess
set value of attribute "AXFullScreen" to true
end tell
end tell'''
        self._require_ok(self._osascript(script), "plein ecran indisponible")
        return intent.summary

    def _do_click_named(self, intent: ComputerIntent) -> str:
        script = '''on run argv
set wanted to item 1 of argv
tell application "System Events"
set frontProcess to first application process whose frontmost is true
set allItems to entire contents of front window of frontProcess
repeat with candidate in allItems
try
set candidateName to name of candidate as text
if candidateName is wanted then
try
perform action "AXPress" of candidate
on error
click candidate
end try
return "clicked"
end if
end try
end repeat
error "element introuvable"
end tell
end run'''
        self._require_ok(self._osascript(script, intent.target), "element introuvable")
        return f"J'ai clique sur {intent.target}."

    def _do_screenshot(self, intent: ComputerIntent) -> str:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = self.screenshot_dir / f"ava_{stamp}.png"
        result = self._run(["screencapture", "-x", str(path)])
        self._require_ok(result, "capture impossible")
        return f"Capture enregistree dans {path}."


class ComputerUseEngine:
    """ajoute expiration et confirmation vocale aux actions sensibles."""

    CONFIRM_WORDS = {"confirme", "oui confirme", "vas y", "execute", "fais le"}
    CANCEL_WORDS = {"annule", "non annule", "laisse tomber", "stop"}

    def __init__(self, controller: MacComputerController | None = None, ttl_s: float = 30) -> None:
        self.controller = controller or MacComputerController()
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._pending: tuple[ComputerIntent, float] | None = None

    @property
    def pending(self) -> ComputerIntent | None:
        with self._lock:
            if self._pending is None:
                return None
            intent, created = self._pending
            if time.monotonic() - created > self.ttl_s:
                self._pending = None
                return None
            return intent

    def handle(
        self,
        text: str,
        app_resolver: Callable[[str], str | None] | None = None,
    ) -> ActionOutcome:
        command = _norm(text)
        pending = self.pending
        if pending is not None and command in self.CANCEL_WORDS:
            with self._lock:
                self._pending = None
            return ActionOutcome(True, True, "Action annulee.", intent=pending)
        if pending is not None and command in self.CONFIRM_WORDS:
            with self._lock:
                self._pending = None
            return self.controller.execute(pending)

        intent = parse_computer_intent(text)
        if intent is None:
            return ActionOutcome(False)
        if intent.kind == "focus_app" and app_resolver is not None:
            resolved = app_resolver(intent.target)
            if resolved:
                intent = ComputerIntent(
                    intent.kind, intent.value, resolved,
                    f"Je passe sur {resolved}.", intent.requires_confirmation,
                )
        if intent.requires_confirmation:
            with self._lock:
                self._pending = (intent, time.monotonic())
            return ActionOutcome(
                True, False,
                f"{intent.summary} Dis 'Bonjour Ava, confirme' pour continuer, ou 'Bonjour Ava, annule'.",
                True, intent,
            )
        return self.controller.execute(intent)
