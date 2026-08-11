"""Agent mode: Ava drives the screen herself, guided by a vision model.

The loop (in the spirit of "computer use" agents like H Company's Surfer-H /
Holo models):
    1. screenshot
    2. the vision model gets the image + the goal + the history so far
    3. it answers ONE action as JSON: click here, type this, press return...
    4. we execute through System Events (macOS accessibility), and loop
until "done", a bounded number of turns, or an action judged risky.

Backend: any OpenAI-compatible API with vision.
    - default: the Mistral API (MISTRAL_API_KEY already in .env for Voxtral)
    - local: AVA_AGENT_BASE_URL=http://127.0.0.1:1234/v1 with a UI-grounding
      model loaded in LM Studio (e.g. H Company's Holo1.5-7B) — same contract,
      no extra dependency.

Guardrails: turns are bounded, every action is announced out loud before it
runs, and anything that smells like send / pay / buy / delete / publish stops
the agent cold — that click belongs to Matheus.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from ava import net


MAX_STEPS = 6
RISKY_WORDS = ("envoyer", "envoie", "payer", "paiement", "acheter", "achat",
               "supprimer", "supprime", "publier", "publie",
               "send", "buy", "pay", "delete", "submit", "purchase")

SYSTEM_PROMPT = """Tu pilotes l'écran d'un Mac pour accomplir un objectif.
À chaque tour tu reçois une capture d'écran de {img_w}x{img_h} pixels.
Réponds UNIQUEMENT un objet JSON, sans texte autour :
{{"action": "...", "x": 0, "y": 0, "text": "", "say": "..."}}
Actions possibles :
- "click" / "double_click" : x,y en pixels de L'IMAGE reçue
- "type" : text = ce qu'il faut taper (clique d'abord le champ au tour d'avant)
- "key" : text = "return" | "escape" | "tab"
- "scroll_down" / "scroll_up"
- "open_app" : text = nom de l'application macOS à ouvrir
- "done" : l'objectif est atteint (say = ce que tu as fait)
- "impossible" : tu ne peux pas y arriver (say = pourquoi)
"say" = une phrase courte en français qui décrit l'action, dite à voix haute.
Une seule action par tour. Vise le CENTRE de l'élément, pas son bord.
Si l'élément demandé n'est PAS visible sur la capture, réponds "impossible"
au lieu de deviner des coordonnées.
"open_app" sert UNIQUEMENT pour une vraie application macOS (Safari, Notes...),
jamais pour un site ou un onglet.
Compare la capture avec l'historique : si ta dernière action n'a pas eu l'effet
attendu, ne la répète pas — change d'approche ou réponds "impossible"."""


@dataclass
class AgentResult:
    ok: bool
    message: str
    steps: int = 0
    history: list = field(default_factory=list)


def capture_width() -> int:
    # 1600 is a good default; raise AVA_AGENT_CAPTURE_WIDTH to 1920 when small
    # icons keep getting missed (more pixels, slightly slower steps).
    try:
        return max(800, min(2560, int(os.getenv("AVA_AGENT_CAPTURE_WIDTH", "1600"))))
    except ValueError:
        return 1600


def capture_screen(directory: Path, max_width_px: int | None = None) -> Path | None:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"agent-{stamp}.jpg"
    # -x silent, -m main display, jpg is far lighter than png for a model call
    result = subprocess.run(
        ["screencapture", "-x", "-m", "-t", "jpg", str(path)],
        capture_output=True, check=False, timeout=15,
    )
    if result.returncode != 0 or not path.exists():
        return None
    # sips ships with macOS: shrink so a retina capture doesn't blow the call up
    subprocess.run(
        ["sips", "-Z", str(max_width_px or capture_width()), str(path)],
        capture_output=True, check=False, timeout=15,
    )
    return path


def main_screen_size() -> tuple[int, int]:
    """Size, in points, of the PRIMARY display — the one `screencapture -m` shoots.

    Finder's "bounds of window of desktop" is the union of every display: with
    two stacked screens it reports 1920x2062 while the capture only covers the
    main one, and every click lands twice too low. NSScreen.screens()[0] is the
    display that owns the menu bar, which is exactly the one we photograph.
    """
    try:
        from AppKit import NSScreen  # ships with pywebview's pyobjc
        frame = NSScreen.screens()[0].frame()
        return int(frame.size.width), int(frame.size.height)
    except Exception:  # noqa: BLE001
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        try:
            x1, y1, x2, y2 = [int(v.strip()) for v in out.stdout.split(",")]
            return x2 - x1, y2 - y1
        except Exception:  # noqa: BLE001
            return 1440, 900


def image_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True, text=True, check=False, timeout=10,
    ).stdout
    w = re.search(r"pixelWidth:\s*(\d+)", out)
    h = re.search(r"pixelHeight:\s*(\d+)", out)
    return (int(w.group(1)) if w else 1440, int(h.group(1)) if h else 900)


def extract_json(text: str) -> dict | None:
    # Models wrap the JSON in markdown fences often enough: fish the object out.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_holo_action(text: str) -> dict | None:
    """Understand the native dialect of grounding models (H Company's Holo).

    Instructed to answer JSON, those models often reply in their trained action
    style instead: `Click(x=512, y=300)`, `type(content="hello")`,
    `press("enter")`, `scroll(down)`... Rather than failing the turn, we
    translate the common shapes into our step dict.
    """
    raw = text.strip()
    flat = raw.lower()

    def coords(match: re.Match) -> tuple[int, int]:
        return int(float(match.group(1))), int(float(match.group(2)))

    numbers = r"[^\d-]*(-?\d+(?:\.\d+)?)[^\d-]+(-?\d+(?:\.\d+)?)"
    click = re.search(r"(?:double[_ ]?click|doubleclick)\(" + numbers, flat)
    if click:
        x, y = coords(click)
        return {"action": "double_click", "x": x, "y": y,
                "say": "Je double-clique."}
    click = re.search(r"(?:left_)?click(?:_at)?\(" + numbers, flat)
    if click:
        x, y = coords(click)
        return {"action": "click", "x": x, "y": y, "say": "Je clique."}
    # "CLICK <point>[[512, 300]]" and friends
    click = re.search(r"click[^\[]*\[\[" + numbers, flat)
    if click:
        x, y = coords(click)
        return {"action": "click", "x": x, "y": y, "say": "Je clique."}

    typed = re.search(r"(?:type|write|input)\((?:content=|text=)?[\"']?([^\"')]+)", raw,
                      re.IGNORECASE)
    if typed:
        return {"action": "type", "text": typed.group(1).strip(),
                "say": "Je tape le texte."}

    key = re.search(r"(?:press|key|hotkey)\([\"']?(\w+)", flat)
    if key:
        name = {"enter": "return", "return": "return", "esc": "escape",
                "escape": "escape", "tab": "tab"}.get(key.group(1))
        if name:
            return {"action": "key", "text": name, "say": "J'appuie sur la touche."}

    if re.search(r"scroll\((?:down|.*direction=[\"']?down)", flat):
        return {"action": "scroll_down", "say": "Je descends dans la page."}
    if re.search(r"scroll\((?:up|.*direction=[\"']?up)", flat):
        return {"action": "scroll_up", "say": "Je remonte dans la page."}

    if re.search(r"^(?:done|finished|termine|terminé)\b|\btask (?:is )?(?:done|complete)", flat):
        return {"action": "done", "say": "C'est fait."}
    if re.search(r"^(?:impossible|infeasible|cannot|can't)\b", flat):
        return {"action": "impossible", "say": raw[:120]}

    # last resort: a JSON answer truncated mid-string (token limit) still
    # carries its action and coordinates — salvage them rather than fail.
    action = re.search(r'"action"\s*:\s*"(\w+)"', raw)
    if action:
        step: dict = {"action": action.group(1), "say": ""}
        x = re.search(r'"x"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
        y = re.search(r'"y"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
        if x and y:
            step["x"] = int(float(x.group(1)))
            step["y"] = int(float(y.group(1)))
        text = re.search(r'"text"\s*:\s*"([^"]*)"', raw)
        if text:
            step["text"] = text.group(1)
        return step
    return None


def looks_risky(step: dict) -> bool:
    blob = " ".join(str(step.get(k, "")) for k in ("say", "text")).lower()
    return any(word in blob for word in RISKY_WORDS)


class ScreenAgent:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "AVA_AGENT_BASE_URL",
            os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
        ).rstrip("/")
        self.model = os.getenv("AVA_AGENT_MODEL", "mistral-small-latest").strip()
        self.api_key = os.getenv("MISTRAL_API_KEY", "").strip()

    def _is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url

    def available(self) -> bool:
        return bool(self.api_key) or self._is_local()

    # --- model call ----------------------------------------------------------

    def _ask_model(self, goal: str, shot: Path, img_w: int, img_h: int,
                   history: list[str]) -> str:
        image_b64 = base64.b64encode(shot.read_bytes()).decode("ascii")
        past = ("Actions déjà faites :\n" + "\n".join(history)) if history else \
            "C'est le premier tour."
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "max_tokens": 320,
                "temperature": 0.1,
                "messages": [
                    {"role": "system",
                     "content": SYSTEM_PROMPT.format(img_w=img_w, img_h=img_h)},
                    {"role": "user", "content": [
                        {"type": "text",
                         "text": f"Objectif : {goal}\n{past}\nQuelle est la prochaine action ?"},
                        # object form: the openai standard, accepted by mistral
                        # AND required by lm studio (a bare string is refused).
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]},
                ],
            },
            timeout=net.timeout(45) if not self._is_local() else 45,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def _ensure_local_server(self) -> bool:
        # `lms server start` doesn't necessarily survive a reboot. when the
        # local endpoint refuses the connection, start it ourselves and give
        # the model a moment to jit-load, instead of telling the user to go
        # find a terminal.
        lms = Path.home() / ".lmstudio" / "bin" / "lms"
        if not self._is_local() or not lms.exists():
            return False
        result = subprocess.run([str(lms), "server", "start"],
                                capture_output=True, timeout=30, check=False)
        time.sleep(2.0)
        return result.returncode == 0

    # --- action execution ----------------------------------------------------

    @staticmethod
    def _osascript(script: str, *args: str) -> None:
        subprocess.run(["osascript", "-e", script, *args],
                       capture_output=True, check=False, timeout=12)

    def _click(self, x: int, y: int, double: bool = False) -> None:
        # CGEvent, not System Events' `click at`: the latter silently does
        # nothing on modern macOS ("elle prend la main et il ne se passe
        # rien"). A synthetic mouse event through the HID tap is what real
        # automation tools post, and it lands everywhere.
        try:
            import Quartz
            point = (float(x), float(y))
            move = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
            time.sleep(0.05)
            clicks = 2 if double else 1
            for click_no in range(1, clicks + 1):
                for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    event = Quartz.CGEventCreateMouseEvent(
                        None, kind, point, Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventSetIntegerValueField(
                        event, Quartz.kCGMouseEventClickState, click_no)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
                    time.sleep(0.02)
        except Exception:  # noqa: BLE001 — pyobjc missing: degraded applescript
            script = f'tell application "System Events" to click at {{{x}, {y}}}'
            self._osascript(script)
            if double:
                time.sleep(0.12)
                self._osascript(script)

    def _execute(self, step: dict, scale_x: float, scale_y: float) -> None:
        action = step.get("action", "")
        if action in ("click", "double_click"):
            x = int(float(step.get("x", 0)) * scale_x)
            y = int(float(step.get("y", 0)) * scale_y)
            self._click(x, y, double=(action == "double_click"))
        elif action == "type":
            self._osascript(
                'on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run',
                str(step.get("text", "")),
            )
        elif action == "key":
            codes = {"return": 36, "escape": 53, "tab": 48}
            code = codes.get(str(step.get("text", "")).lower())
            if code is not None:
                self._osascript(f'tell application "System Events" to key code {code}')
        elif action in ("scroll_down", "scroll_up"):
            code = 125 if action == "scroll_down" else 126
            self._osascript(
                f'tell application "System Events"\nrepeat 3 times\nkey code {code}\ndelay 0.05\nend repeat\nend tell')
        elif action == "open_app":
            subprocess.run(["open", "-a", str(step.get("text", ""))],
                           capture_output=True, check=False)

    # --- the loop ------------------------------------------------------------

    def run(self, goal: str, screen_size: tuple[int, int],
            say: Callable[[str], None], shots_dir: Path | None = None) -> AgentResult:
        if not self.available():
            return AgentResult(False, "Il me faut une clé Mistral, ou un modèle "
                                      "vision local, pour piloter l'écran.")
        if not self._is_local() and not net.reachable("agent"):
            return AgentResult(False, "Je suis hors ligne, je ne peux pas "
                                      "piloter l'écran pour le moment.")
        history: list[str] = []
        screen_w, screen_h = screen_size
        directory = shots_dir or Path.home() / "Pictures" / "Ava" / "agent"
        for step_no in range(1, MAX_STEPS + 1):
            shot = capture_screen(directory)
            if shot is None:
                return AgentResult(False, "Je n'arrive pas à capturer l'écran.",
                                   step_no - 1, history)
            img_w, img_h = image_size(shot)
            try:
                try:
                    reply = self._ask_model(goal, shot, img_w, img_h, history)
                except requests.exceptions.ConnectionError:
                    # local server down (fresh boot): restart it once, retry.
                    if not self._ensure_local_server():
                        raise
                    reply = self._ask_model(goal, shot, img_w, img_h, history)
                step = extract_json(reply) or parse_holo_action(reply)
                if not self._is_local():
                    net.note_success("agent")
            except Exception as exc:  # noqa: BLE001
                if not self._is_local():
                    net.note_failure("agent", exc)
                return AgentResult(False, "Le modèle vision ne répond pas, "
                                          "j'arrête là.", step_no - 1, history)
            if not step or "action" not in step:
                return AgentResult(False, "Je n'ai pas compris la réponse du "
                                          "modèle, j'arrête pour être sûre.",
                                   step_no - 1, history)
            action = str(step.get("action", ""))
            said = str(step.get("say", "")).strip()
            if action in ("done", "impossible"):
                print(f"[agent] {step_no}/{MAX_STEPS} verdict={action}")
            if action == "done":
                return AgentResult(True, said or "C'est fait.", step_no - 1, history)
            if action == "impossible":
                return AgentResult(False, said or "Je ne peux pas faire ça à "
                                                  "l'écran.", step_no - 1, history)
            if looks_risky(step):
                return AgentResult(False,
                                   "Je m'arrête juste avant : " + (said or action) +
                                   ". Ce genre d'action, c'est toi qui la fais.",
                                   step_no - 1, history)
            if said:
                say(said)
            print(f"[agent] {step_no}/{MAX_STEPS} {action} "
                  f"{ {k: v for k, v in step.items() if k != 'say'} }")
            self._execute(step, screen_w / max(1, img_w), screen_h / max(1, img_h))
            history.append(f"{step_no}. {action} — {said or step.get('text', '')}")
            time.sleep(0.8)   # let the interface settle before the next capture
        return AgentResult(False, "J'ai atteint ma limite de tours sans terminer. "
                                  "Regarde où j'en suis et reprends la main.",
                           MAX_STEPS, history)


def main(argv: list[str] | None = None) -> int:
    """Test bench for the screen agent, no voice needed.

        .venv/bin/python -m ava.mac.screen_agent "clique sur la corbeille"
        .venv/bin/python -m ava.mac.screen_agent --live "ouvre mes téléchargements"

    Dry run (default): one capture, one model call, the proposed action printed
    with both image and screen coordinates — nothing is clicked. --live runs
    the real bounded loop, actions narrated on stdout.
    """
    import argparse

    from dotenv import load_dotenv

    from ava import paths

    load_dotenv(paths.ENV_FILE)
    parser = argparse.ArgumentParser(description="banc d'essai du mode agent")
    parser.add_argument("goal", nargs="+", help="l'objectif, en français")
    parser.add_argument("--live", action="store_true",
                        help="exécute vraiment les actions (défaut : à blanc)")
    args = parser.parse_args(argv)
    goal = " ".join(args.goal)

    agent = ScreenAgent()
    print(f"endpoint : {agent.base_url}  modèle : {agent.model}")

    screen = main_screen_size()

    if args.live:
        result = agent.run(goal, screen, say=lambda msg: print(f"  ava : {msg}"))
        print(("✓ " if result.ok else "✗ ") + result.message)
        return 0 if result.ok else 1

    shot = capture_screen(Path.home() / "Pictures" / "Ava" / "agent")
    if shot is None:
        print("capture impossible"); return 1
    img_w, img_h = image_size(shot)
    print(f"capture : {shot.name} ({img_w}x{img_h}, écran {screen[0]}x{screen[1]})")
    started = time.time()
    try:
        reply = agent._ask_model(goal, shot, img_w, img_h, [])
    except requests.exceptions.ConnectionError:
        if not agent._ensure_local_server():
            print("le serveur local ne répond pas (lms server start ?)"); return 1
        reply = agent._ask_model(goal, shot, img_w, img_h, [])
    print(f"réponse en {time.time() - started:.1f}s : {reply.strip()[:300]}")
    step = extract_json(reply) or parse_holo_action(reply)
    if not step:
        print("→ réponse incomprise (ni json ni dialecte holo)"); return 1
    print(f"→ action : {step}")
    if step.get("action") in ("click", "double_click"):
        sx = int(float(step.get("x", 0)) * screen[0] / max(1, img_w))
        sy = int(float(step.get("y", 0)) * screen[1] / max(1, img_h))
        print(f"→ à l'écran, ça cliquerait en ({sx}, {sy}) — rien n'a été cliqué (--live pour le faire)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
