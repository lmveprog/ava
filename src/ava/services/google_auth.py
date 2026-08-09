"""connexion google d'ava : oauth 2.0 « application de bureau » avec pkce.

le principe est celui des connecteurs claude : un bouton dans les reglages
ouvre le consentement google dans le navigateur, google renvoie le code sur un
petit serveur local (127.0.0.1, port ephemere), et ava garde un refresh token
sur le disque. aucune dependance google : requests + la stdlib suffisent.

pourquoi pas de secret embarque : un client « desktop » n'a pas de secret
vraiment secret (google le dit lui-meme), et matheus doit de toute facon creer
son propre client oauth pour que son agenda ne transite par personne d'autre.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse

import requests

from ava import paths

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# lecture ET ecriture de l'agenda, plus l'adresse du compte pour l'afficher.
SCOPES = (
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "email",
)

TOKEN_PATH = paths.cache_dir("google_token.json")
CONNECT_TIMEOUT_S = 300


class GoogleAuthError(RuntimeError):
    pass


_HTML_OK = """<!doctype html><meta charset="utf-8"><title>Ava</title>
<body style="font:16px -apple-system,system-ui;background:#0d0f14;color:#e8ecf4;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:40px">✓</div>
<h1 style="font-size:18px;font-weight:600">Ava est connectée à Google</h1>
<p style="color:#8b93a7">Tu peux refermer cet onglet.</p></div>"""

_HTML_KO = """<!doctype html><meta charset="utf-8"><title>Ava</title>
<body style="font:16px -apple-system,system-ui;background:#0d0f14;color:#e8ecf4;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:40px">✕</div>
<h1 style="font-size:18px;font-weight:600">Connexion refusée</h1>
<p style="color:#8b93a7">Relance depuis les réglages d'Ava.</p></div>"""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Recoit le retour de google et le range dans le serveur."""

    def do_GET(self):  # noqa: N802 - impose par BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.result = {  # type: ignore[attr-defined]
            "code": (query.get("code") or [""])[0],
            "state": (query.get("state") or [""])[0],
            "error": (query.get("error") or [""])[0],
        }
        body = (_HTML_OK if query.get("code") else _HTML_KO).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence : ce serveur vit 30 secondes
        return


class GoogleAuth:
    """Etat de la connexion google, cote disque."""

    def __init__(self, token_path: Path | None = None) -> None:
        self.token_path = Path(token_path or TOKEN_PATH)
        self._lock = threading.RLock()
        self._pending: dict = {}

    # --- identifiants du client oauth --------------------------------------

    def credentials(self) -> tuple[str, str]:
        """client id/secret, depuis config.json puis .env en repli."""
        client_id = client_secret = ""
        try:
            from ava.config import STORE
            google = STORE.snapshot().get("google", {})
            client_id = str(google.get("client_id", "")).strip()
            client_secret = str(google.get("client_secret", "")).strip()
        except Exception:  # noqa: BLE001
            pass
        client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "").strip()
        client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        return client_id, client_secret

    # --- stockage du jeton --------------------------------------------------

    def _read(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        # a refresh token is worth a password: 0600 from the moment the file
        # exists, never a chmod after the fact.
        paths.write_private(self.token_path, json.dumps(data, indent=2))

    def status(self) -> dict:
        client_id, _ = self.credentials()
        data = self._read()
        connecting = bool(self._pending.get("thread") and self._pending["thread"].is_alive())
        return {
            "configured": bool(client_id),
            "connected": bool(data.get("refresh_token")),
            "email": data.get("email", ""),
            "connecting": connecting,
            "error": self._pending.get("error", ""),
        }

    def disconnect(self) -> dict:
        with self._lock:
            data = self._read()
            token = data.get("refresh_token") or data.get("access_token")
            if token:
                try:
                    requests.post(REVOKE_URL, data={"token": token}, timeout=10)
                except requests.RequestException:
                    pass  # revoquer est un bonus, oublier localement suffit
            try:
                self.token_path.unlink()
            except OSError:
                pass
            self._pending = {}
        return {"connected": False}

    # --- le flux oauth ------------------------------------------------------

    def begin_connect(self) -> dict:
        """Ouvre le consentement google. Rend la main tout de suite : la suite
        se joue dans un thread, les reglages n'ont qu'a repasser sur status()."""
        client_id, client_secret = self.credentials()
        if not client_id or not client_secret:
            return {"started": False,
                    "error": "Ajoute d'abord l'ID et le secret du client OAuth Google."}
        with self._lock:
            thread = self._pending.get("thread")
            if thread and thread.is_alive():
                return {"started": True, "url": self._pending.get("url", "")}

            verifier = _b64url(secrets.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            state = secrets.token_urlsafe(24)
            port = _free_port()
            redirect_uri = f"http://127.0.0.1:{port}/"
            url = AUTH_URL + "?" + urllib.parse.urlencode({
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                # sans ces deux-la, google ne redonne pas de refresh token.
                "access_type": "offline",
                "prompt": "consent",
            })

            worker = threading.Thread(
                target=self._await_callback,
                args=(port, redirect_uri, verifier, state, client_id, client_secret),
                daemon=True,
            )
            self._pending = {"thread": worker, "url": url, "error": ""}
            worker.start()

        subprocess.run(["open", url], check=False, timeout=10)
        return {"started": True, "url": url}

    def _await_callback(self, port, redirect_uri, verifier, state, client_id, client_secret) -> None:
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
        server.result = None  # type: ignore[attr-defined]
        server.timeout = 1.0
        deadline = time.time() + CONNECT_TIMEOUT_S
        try:
            while time.time() < deadline and server.result is None:  # type: ignore[attr-defined]
                server.handle_request()
        finally:
            server.server_close()

        result = server.result  # type: ignore[attr-defined]
        if not result:
            self._pending["error"] = "Connexion Google expirée."
            return
        if result["error"] or not result["code"]:
            self._pending["error"] = f"Google a refusé : {result['error'] or 'aucun code'}."
            return
        if not secrets.compare_digest(result["state"], state):
            # protection csrf : un state qui ne colle pas = requete etrangere.
            self._pending["error"] = "Réponse Google inattendue (state invalide)."
            return
        try:
            self._exchange(result["code"], verifier, redirect_uri, client_id, client_secret)
            self._pending["error"] = ""
        except Exception as exc:  # noqa: BLE001
            self._pending["error"] = str(exc)

    def _exchange(self, code, verifier, redirect_uri, client_id, client_secret) -> None:
        response = requests.post(TOKEN_URL, timeout=20, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if response.status_code != 200:
            raise GoogleAuthError(f"Échange du code refusé ({response.status_code}).")
        payload = response.json()
        record = {
            "refresh_token": payload.get("refresh_token", ""),
            "access_token": payload.get("access_token", ""),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
            "scope": payload.get("scope", ""),
            "email": "",
        }
        if not record["refresh_token"]:
            raise GoogleAuthError("Google n'a pas renvoyé de refresh token.")
        record["email"] = self._fetch_email(record["access_token"])
        with self._lock:
            self._write(record)

    def _fetch_email(self, access_token: str) -> str:
        try:
            response = requests.get(
                USERINFO_URL, timeout=10,
                headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code == 200:
                return str(response.json().get("email", ""))
        except requests.RequestException:
            pass
        return ""

    # --- jeton d'acces utilisable ------------------------------------------

    def access_token(self) -> str:
        """Un access token frais, rafraichi a la volee. Vide si non connecte."""
        with self._lock:
            data = self._read()
            if not data.get("refresh_token"):
                return ""
            if data.get("access_token") and time.time() < float(data.get("expires_at", 0)):
                return str(data["access_token"])
            client_id, client_secret = self.credentials()
            if not client_id:
                return ""
            response = requests.post(TOKEN_URL, timeout=20, data={
                "refresh_token": data["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            })
            if response.status_code != 200:
                # refresh revoque cote google : on oublie, l'ui redemandera.
                if response.status_code in (400, 401):
                    try:
                        self.token_path.unlink()
                    except OSError:
                        pass
                return ""
            payload = response.json()
            data["access_token"] = payload.get("access_token", "")
            data["expires_at"] = time.time() + int(payload.get("expires_in", 3600)) - 60
            self._write(data)
            return str(data["access_token"])


AUTH = GoogleAuth()
