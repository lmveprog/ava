import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava.services import google_auth as ga  # noqa: E402


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = ga.GoogleAuth(Path(self.tmp.name) / "token.json")

    def test_nothing_stored_means_not_connected(self):
        with patch.object(self.auth, "credentials", return_value=("id", "secret")):
            status = self.auth.status()
        self.assertTrue(status["configured"])
        self.assertFalse(status["connected"])

    def test_missing_credentials_are_reported(self):
        with patch.object(self.auth, "credentials", return_value=("", "")):
            self.assertFalse(self.auth.status()["configured"])

    def test_a_stored_refresh_token_counts_as_connected(self):
        self.auth._write({"refresh_token": "r", "email": "moi@gmail.com"})
        with patch.object(self.auth, "credentials", return_value=("id", "secret")):
            status = self.auth.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["email"], "moi@gmail.com")

    def test_the_token_file_is_not_world_readable(self):
        self.auth._write({"refresh_token": "r"})
        self.assertEqual(self.auth.token_path.stat().st_mode & 0o077, 0)

    def test_connect_refuses_without_credentials(self):
        with patch.object(self.auth, "credentials", return_value=("", "")):
            result = self.auth.begin_connect()
        self.assertFalse(result["started"])

    def test_a_valid_access_token_is_reused(self):
        self.auth._write({"refresh_token": "r", "access_token": "still-good",
                          "expires_at": time.time() + 600})
        with patch.object(self.auth, "credentials", return_value=("id", "secret")), \
                patch.object(ga.requests, "post") as post:
            self.assertEqual(self.auth.access_token(), "still-good")
        post.assert_not_called()

    def test_an_expired_token_is_refreshed_and_kept(self):
        self.auth._write({"refresh_token": "r", "access_token": "old",
                          "expires_at": time.time() - 10})

        class Answer:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": "fresh", "expires_in": 3600}

        with patch.object(self.auth, "credentials", return_value=("id", "secret")), \
                patch.object(ga.requests, "post", return_value=Answer()):
            self.assertEqual(self.auth.access_token(), "fresh")
        stored = json.loads(self.auth.token_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], "fresh")

    def test_a_revoked_refresh_token_is_forgotten(self):
        self.auth._write({"refresh_token": "r", "expires_at": 0})

        class Answer:
            status_code = 400

            @staticmethod
            def json():
                return {}

        with patch.object(self.auth, "credentials", return_value=("id", "secret")), \
                patch.object(ga.requests, "post", return_value=Answer()):
            self.assertEqual(self.auth.access_token(), "")
        self.assertFalse(self.auth.token_path.exists())

    def test_disconnect_deletes_the_token(self):
        self.auth._write({"refresh_token": "r"})
        with patch.object(ga.requests, "post"):
            self.auth.disconnect()
        self.assertFalse(self.auth.token_path.exists())


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = ga.GoogleAuth(Path(self.tmp.name) / "token.json")

    class FakeServer:
        """Sert une seule reponse, comme le vrai serveur de callback."""

        def __init__(self, answer):
            self.answer = answer
            self.result = None
            self.timeout = 1.0

        def handle_request(self):
            self.result = self.answer

        def server_close(self):
            pass

    def _run_callback(self, answer, expected_state="le-vrai"):
        server = self.FakeServer(answer)
        with patch.object(ga.http.server, "HTTPServer", return_value=server), \
                patch.object(self.auth, "_exchange") as exchange:
            self.auth._pending = {}
            self.auth._await_callback(1234, "http://127.0.0.1:1234/", "v",
                                      expected_state, "id", "secret")
        return exchange

    def test_a_forged_state_is_rejected(self):
        # protection csrf : sans le bon state, aucun echange de code.
        exchange = self._run_callback({"code": "abc", "state": "pas-le-bon", "error": ""})
        exchange.assert_not_called()
        self.assertIn("state", self.auth._pending["error"])

    def test_a_refusal_from_google_is_reported(self):
        exchange = self._run_callback({"code": "", "state": "le-vrai", "error": "access_denied"})
        exchange.assert_not_called()
        self.assertIn("access_denied", self.auth._pending["error"])

    def test_a_matching_state_exchanges_the_code(self):
        exchange = self._run_callback({"code": "abc", "state": "le-vrai", "error": ""})
        exchange.assert_called_once()
        self.assertEqual(exchange.call_args.args[0], "abc")
        self.assertEqual(self.auth._pending["error"], "")


if __name__ == "__main__":
    unittest.main()
