from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ava.ui import overlay as overlay


HTML = Path(__file__).parents[1] / "src" / "ava" / "ui" / "web" / "ava.html"


class OverlayContractTests(unittest.TestCase):
    """The UI is the pill-and-cards design (the artifact matheus chose): a
    living orb in a glass pill, floating glass cards, no chat window. These
    tests pin the JS surface the backend drives, not pixel details."""

    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_backend_entry_points_exist(self):
        for name in (
            "avaReady", "avaSetState", "avaIdle", "avaError", "avaShow",
            "avaHide", "avaDormant", "avaOverlayMode", "avaLevel",
            "avaTranscript", "avaMessage", "avaInterrupted",
            "avaChoices", "avaClearChoices", "avaSources", "avaPreview",
            "avaBoot", "avaBootStep", "avaBootDone",
            "avaStartup", "avaStartupBrief", "avaStartupDone",
            "avaOpenSettings",
        ):
            self.assertIn(f"window.{name}", self.source)

    def test_text_micro_and_yes_no_controls_exist(self):
        for identifier in ('id="askInput"', 'id="micBtn"',
                           'id="choiceYes"', 'id="choiceNo"'):
            self.assertIn(identifier, self.source)

    def test_morning_card_has_the_personal_slots(self):
        for identifier in ('id="bDate"', 'id="bHi"', 'id="bNews"',
                           'id="bQuote"', 'id="bAuthor"', 'id="bSay"',
                           'id="bGlyph"'):
            self.assertIn(identifier, self.source)

    def test_spoken_briefing_is_karaoke_timed(self):
        # avaStartupBrief and avaMessage both honour per-word timing marks
        # measured on the real audio, instead of a flat cadence.
        self.assertIn("marks && marks[wi] != null", self.source)
        self.assertIn("delays && delays[wi] != null", self.source)

    def test_interruption_dims_what_was_not_said(self):
        self.assertIn("avaInterrupted", self.source)
        self.assertIn('classList.add("unsaid")', self.source)
        self.assertIn("dataset.at", self.source)

    def test_orb_is_a_local_canvas_no_remote_assets(self):
        self.assertIn('id="orbMain"', self.source)
        self.assertIn("class Orb", self.source)
        self.assertNotIn("rive.app", self.source)
        self.assertNotIn("https://", self.source.split("<script>")[1])

    def test_memory_card_renders_clickable_wikilinks(self):
        for needle in ('id="memoryPanel"', 'id="memoryBtn"',
                       "memory_snapshot", "open_note", "open_vault",
                       'class="wl"'):
            self.assertIn(needle, self.source)

    def test_state_palette_is_the_artifact_one(self):
        # rest is steel; colour only arrives with a state.
        self.assertIn("122,134,150", self.source)   # idle
        self.assertIn("46,208,224", self.source)    # listening
        self.assertIn("139,124,246", self.source)   # thinking
        self.assertIn("67,224,160", self.source)    # speaking
        self.assertIn("245,166,35", self.source)    # action

    def test_runtime_ritual_resizes_and_centers_the_window(self):
        previous_window = overlay._window
        previous_payload = overlay._startup_payload
        window = MagicMock()
        window.native = None
        overlay._window = window
        try:
            with patch.object(overlay, "_screen_size", return_value=(1440, 900)):
                overlay.startup({"auto_finish": False})
        finally:
            overlay._window = previous_window
            overlay._startup_payload = previous_payload
        window.resize.assert_called_once_with(overlay.START_WIDTH, overlay.START_HEIGHT)
        window.move.assert_called_once_with(360, 190)

    def test_native_center_uses_the_visible_screen_origin(self):
        self.assertEqual(
            overlay._layout_origin(1512, 25, 1920, 1055, 720, 520, "center"),
            (2112, 292),
        )


class VoicePreviewTests(unittest.TestCase):
    """The api's voice test must never freeze the panel."""

    def test_it_returns_before_the_synthesis_finishes(self):
        started = []
        with patch("ava.config.STORE") as store, \
                patch("threading.Thread") as thread:
            thread.side_effect = lambda target, daemon: started.append(target) or MagicMock()
            result = overlay._Api().test_voice({"engine": "chatterbox"})
        self.assertTrue(result["started"])
        self.assertEqual(len(started), 1)
        store.update.assert_called_once_with({"voice": {"engine": "chatterbox"}})

    def test_a_broken_engine_is_reported_not_raised(self):
        with patch("ava.config.STORE") as store:
            store.update.side_effect = RuntimeError("config cassée")
            result = overlay._Api().test_voice({"engine": "chatterbox"})
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
