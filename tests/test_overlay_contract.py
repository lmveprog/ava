from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from ava.ui import overlay as overlay


HTML = Path(__file__).parents[1] / "src" / "ava" / "ui" / "web" / "ava.html"


class OverlayContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_backend_entry_points_exist(self):
        for name in (
            "avaReady", "avaSetState", "avaTranscript", "avaMessage",
            "avaChoices", "avaClearChoices", "avaLevel", "avaBoot",
            "avaSources", "avaPreview", "avaStartup", "avaStartupDone",
        ):
            self.assertIn(f"window.{name}", self.source)

    def test_text_micro_and_yes_no_controls_exist(self):
        for identifier in ('id="input"', 'id="mic"', 'id="choiceYes"', 'id="choiceNo"'):
            self.assertIn(identifier, self.source)

    def test_startup_scene_contains_personal_news_and_quote_slots(self):
        for identifier in (
            'id="startupStage"', 'id="startupName"', 'id="startupCity"',
            'id="startupNews"', 'id="startupSource"', 'id="startupQuote"',
            'id="startupVoiceOrb"', 'aria-label="Ava"',
        ):
            self.assertIn(identifier, self.source)

    def test_startup_voice_orb_has_distinct_speaking_animation(self):
        self.assertIn("function initStartupVoiceOrb", self.source)
        self.assertIn('currentState==="speaking"', self.source)

    def test_runtime_ritual_can_hold_the_startup_scene_until_python_finishes(self):
        self.assertIn("data.auto_finish!==false", self.source)
        self.assertIn("!loading&&autoFinish", self.source)

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

    def test_orb_is_a_local_fluid_canvas_with_offline_fallback(self):
        # l'orbe du topbar est un canvas fluide local (meme rendu que l'accueil),
        # avec le fallback css. plus de mascotte Rive distante -> marche hors-ligne.
        self.assertIn("fallback-orb", self.source)
        self.assertIn("function initAvatarOrb", self.source)
        self.assertNotIn("rive.app", self.source)


if __name__ == "__main__":
    unittest.main()


class OrbGeometryTests(unittest.TestCase):
    """L'orbe se faisait trancher au carre par le bord de son canvas."""

    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / "src" / "ava" / "ui" / "web" / "ava.html").read_text(encoding="utf-8")

    def test_the_startup_orb_scales_with_its_canvas(self):
        # l'echelle doit venir du canvas, plus de rayons en pixels durs.
        self.assertIn("const k=Math.min(w,h)/2/120", self.html)
        self.assertIn("fluidPath(cx,cy,79*k,t,amplitude)", self.html)
        self.assertNotIn("fluidPath(cx,cy,79,t,amplitude)", self.html)

    def test_the_halo_never_reaches_the_canvas_edge(self):
        # forme (79 + 7.2 d'ondulation) + flou (34) = 120.2 unites, sur 125
        # disponibles : il reste toujours de la marge avant le bord.
        self.assertLessEqual((79 + 7.2 + 34) / 120, 1.005)

    def test_the_orb_box_reserves_the_room_for_its_halo(self):
        # le noyau porte le halo en inset:0, donc la mise en page en tient compte
        self.assertIn(".startup-orb-core{position:relative;width:min(212px,100%)", self.html)
        self.assertIn(".startup-orb-core::before{content:\"\";position:absolute;inset:0", self.html)
        self.assertNotIn("width:230px;height:230px", self.html)

    def test_the_left_column_clips_whatever_overflows(self):
        self.assertIn("overflow:clip", self.html)


class VoicePreviewTests(unittest.TestCase):
    """Bouton « écouter un extrait » : il ne doit jamais figer le panneau."""

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
        self.assertFalse(result["started"])
        self.assertIn("cassée", result["error"])

    def test_the_button_and_its_handler_exist(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "ava" / "ui" / "web" / "ava.html").read_text(encoding="utf-8")
        self.assertIn('id="voiceTest"', html)
        self.assertIn("api.test_voice(", html)
