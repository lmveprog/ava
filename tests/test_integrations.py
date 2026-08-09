import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app_catalog import AppCatalog
from calendar_tools import MacCalendar
from screen_vision import ScreenVision
from web_research import WebResearch


class _Response:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class IntegrationUnitTests(unittest.TestCase):
    def test_calendar_summary_is_read_only_and_sorted(self):
        payload = [
            {"title": "Déjeuner", "start": "2026-08-07T10:30:00Z", "end": "2026-08-07T11:30:00Z", "calendar": "Perso", "location": "Vieux-Port", "allDay": False},
            {"title": "Anniversaire", "start": "2026-08-06T22:00:00Z", "end": "2026-08-07T22:00:00Z", "calendar": "Perso", "location": "", "allDay": True},
        ]
        result = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("calendar_tools.sys.platform", "darwin"), patch("calendar_tools.subprocess.run", return_value=result):
            reply = MacCalendar().summary(0)
        self.assertTrue(reply.available)
        self.assertIn("2 rendez-vous", reply.text)
        self.assertIn("toute la journée", reply.text)
        self.assertIn("Vieux-Port", reply.text)

    def test_official_om_calendar_selects_next_future_match(self):
        html = '''
        <time datetime="2026-08-01T18:00:00Z"><span>1 août</span></time><p>Marseille<br/>Nice</p>
        <time datetime="2026-08-21T18:45:00Z"><span>21 août</span></time><p>Marseille<br/>Strasbourg</p>
        <time datetime="2026-08-28T18:45:00Z"><span>28 août</span></time><p>Lille<br/>Marseille</p>
        '''
        session = Mock()
        session.get.return_value = _Response(text=html)
        research = WebResearch(session=session)
        reply = research.next_om_match(dt.datetime(2026, 8, 7, tzinfo=ZoneInfo("Europe/Paris")))
        self.assertTrue(reply.available)
        self.assertIn("Strasbourg", reply.answer)
        self.assertIn("21 août", reply.answer)
        self.assertEqual(reply.sources[0].url, WebResearch.OM_URL)

    def test_app_catalog_uses_safe_fuzzy_resolution(self):
        catalog = AppCatalog()
        catalog._apps = {
            "whatsapp": ("WhatsApp", "/Applications/WhatsApp.app"),
            "visual studio code": ("Visual Studio Code", "/Applications/Visual Studio Code.app"),
        }
        catalog._updated_at = float("inf")
        self.assertEqual(catalog.resolve("what sap")[0], "WhatsApp")
        self.assertIsNone(catalog.resolve("xyz totalement inconnu"))

    def test_vision_prefers_an_installed_multimodal_model(self):
        engine = ScreenVision()
        with patch.object(engine, "_models", return_value=["qwen3.5:9b", "gemma3:12b"]):
            self.assertEqual(engine._vision_model(), "gemma3:12b")

    def test_capture_failure_does_not_call_a_remote_service(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = ScreenVision(Path(directory))
            failed = Mock(returncode=1, stdout="", stderr="permission denied")
            with patch("screen_vision.subprocess.run", return_value=failed):
                reply = engine.capture_and_analyze("quel est le problème")
        self.assertFalse(reply.available)
        self.assertIn("Enregistrement de l'écran", reply.text)


if __name__ == "__main__":
    unittest.main()

