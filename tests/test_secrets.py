"""anything that touches secrets: file permissions, and urls coming in.

three things already went wrong here and must not again — the config file
dropping back to 644 on every save, .cache being readable by other accounts
on the mac while it holds the google refresh token, and a source url landing
in the panel exactly as the web handed it over.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import paths  # noqa: E402
from ava.config import ConfigStore  # noqa: E402
from ava.services.google_auth import GoogleAuth  # noqa: E402
from ava.ui import overlay  # noqa: E402


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class PrivateWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_file_is_born_at_0600(self):
        target = self.root / "secret.json"
        paths.write_private(target, '{"token": "x"}')
        self.assertEqual(mode(target), 0o600)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"token": "x"}')

    def test_rewriting_does_not_loosen_the_mode(self):
        """The original bug: every save put the mode back to 644."""
        target = self.root / "secret.json"
        paths.write_private(target, "{}")
        os.chmod(target, 0o644)
        paths.write_private(target, '{"token": "y"}')
        self.assertEqual(mode(target), 0o600)

    def test_nothing_is_left_behind_when_the_write_fails(self):
        target = self.root / "secret.json"
        with patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                paths.write_private(target, "{}")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_config_and_google_token_both_go_through_it(self):
        config = self.root / "config.json"
        ConfigStore(config).update({"identity": {"city": "Lyon"}})
        self.assertEqual(mode(config), 0o600)

        token = self.root / "google_token.json"
        GoogleAuth(token)._write({"refresh_token": "1//secret"})
        self.assertEqual(mode(token), 0o600)
        self.assertEqual(json.loads(token.read_text())["refresh_token"], "1//secret")


class CacheDirTest(unittest.TestCase):
    def test_the_cache_folder_is_mine_alone(self):
        """It holds the refresh token, so no other account on the mac gets in."""
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(paths, "_CACHE_DIR", Path(folder) / ".cache"):
                target = paths.cache_dir("google_token.json")
                self.assertEqual(mode(target.parent), 0o700)
                self.assertEqual(target.name, "google_token.json")


class SourceUrlTest(unittest.TestCase):
    """Sources come off the open web and become hrefs in the panel."""

    def sent(self, items) -> list[dict]:
        with patch.object(overlay, "_eval") as evaluate:
            overlay.sources(items)
        payload = evaluate.call_args.args[0]
        return json.loads(payload[payload.index("([") + 1:payload.rindex(")")])

    def test_only_http_and_https_reach_the_panel(self):
        kept = self.sent([
            {"title": "ok", "url": "https://example.org/a"},
            {"title": "xss", "url": "javascript:alert(1)"},
            {"title": "local", "url": "file:///etc/passwd"},
            {"title": "inline", "url": "data:text/html,<script>"},
            {"title": "empty", "url": ""},
        ])
        self.assertEqual([item["url"] for item in kept], ["https://example.org/a"])

    def test_five_sources_at_most(self):
        kept = self.sent([{"title": str(i), "url": f"https://e.org/{i}"} for i in range(9)])
        self.assertEqual(len(kept), 5)


if __name__ == "__main__":
    unittest.main()
