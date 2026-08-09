"""l'extension de barre de menus et l'ancrage du panneau sous son icone."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import menubar  # noqa: E402
import overlay  # noqa: E402
from assistant_state import AvaState  # noqa: E402


class IconTests(unittest.TestCase):
    def test_every_state_has_its_icon(self):
        # sinon l'icone se figerait sur l'etat precedent sans qu'on le voie.
        for state in AvaState:
            self.assertIn(state.value, menubar.STATE_SYMBOLS, state.value)
            self.assertIn(state.value, menubar.STATE_LABELS, state.value)

    def test_an_unknown_state_falls_back(self):
        bar = menubar.MenuBar()
        bar._apply_state("etat-inconnu")     # sans icone installee : ne doit pas lever
        self.assertEqual(bar._state, "etat-inconnu")


class AnchorLayoutTests(unittest.TestCase):
    """Le panneau doit tomber sous l'icone, jamais dans un coin arbitraire."""

    SCREEN = (0.0, 0.0, 1512.0, 949.0)      # ecran integre, zone visible

    def origin(self, anchor, width=420, height=590, placement="top_right"):
        return overlay._layout_origin(*self.SCREEN, width, height, placement, anchor)

    def test_the_panel_is_centred_under_the_icon(self):
        # icone de 32 px dont le bord gauche est a x=963 -> centre a 979.
        x, _y = self.origin((963.0, 949.0, 32.0, 29.0))
        self.assertEqual(x, 979 - 420 // 2)

    def test_the_panel_hangs_just_below_the_icon(self):
        _x, y = self.origin((963.0, 949.0, 32.0, 29.0))
        self.assertEqual(y, 949 - 590 - overlay.ANCHOR_GAP)

    def test_an_icon_at_the_very_edge_stays_on_screen(self):
        # avec beaucoup d'extensions, l'icone d'ava finit collee au bord droit.
        x, _y = self.origin((1500.0, 949.0, 32.0, 29.0))
        self.assertLessEqual(x + 420, 1512 - overlay.MARGIN + 1)
        self.assertGreaterEqual(x, overlay.MARGIN - 1)

    def test_without_an_anchor_the_old_corner_is_used(self):
        x, y = self.origin(None)
        self.assertEqual(x, 1512 - 420 - overlay.MARGIN)
        self.assertEqual(y, 949 - 590 - overlay.MARGIN)

    def test_the_startup_scene_ignores_the_anchor(self):
        x, y = overlay._layout_origin(*self.SCREEN, 720, 520, "center", None)
        self.assertEqual(x, (1512 - 720) // 2)
        self.assertEqual(y, (949 - 520) // 2)


class PanelVisibilityTests(unittest.TestCase):
    def setUp(self):
        overlay._window = None
        overlay._panel_visible = True

    def test_the_panel_can_be_removed_and_brought_back(self):
        self.assertFalse(overlay.set_panel_visible(False))
        self.assertFalse(overlay.panel_visible())
        self.assertTrue(overlay.toggle_panel())
        self.assertTrue(overlay.panel_visible())


if __name__ == "__main__":
    unittest.main()
