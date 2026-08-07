import unittest
from unittest.mock import patch

from computer_use import (
    ActionOutcome,
    ComputerUseEngine,
    MacComputerController,
    parse_computer_intent,
)


class FakeController:
    def __init__(self):
        self.executed = []

    def execute(self, intent):
        self.executed.append(intent)
        return ActionOutcome(True, True, "fait", intent=intent)


class ComputerUseTests(unittest.TestCase):
    def test_types_without_losing_accents(self):
        intent = parse_computer_intent("Écris déjà prêt")
        self.assertEqual(intent.kind, "type_text")
        self.assertEqual(intent.value, "déjà prêt")
        self.assertFalse(intent.requires_confirmation)

    def test_submit_requires_confirmation(self):
        controller = FakeController()
        engine = ComputerUseEngine(controller=controller)
        first = engine.handle("écris bonjour et envoie")
        self.assertTrue(first.needs_confirmation)
        self.assertEqual(controller.executed, [])
        second = engine.handle("confirme")
        self.assertTrue(second.ok)
        self.assertEqual(controller.executed[0].kind, "type_and_submit")

    def test_risky_click_requires_confirmation(self):
        intent = parse_computer_intent("clique sur supprimer")
        self.assertTrue(intent.requires_confirmation)

    def test_keeps_click_label_accents(self):
        intent = parse_computer_intent("clique sur Réglages avancés")
        self.assertEqual(intent.target, "Réglages avancés")

    def test_apostrophe_commands(self):
        self.assertEqual(parse_computer_intent("prends une capture d'écran").kind, "screenshot")
        self.assertEqual(parse_computer_intent("ferme l'onglet").kind, "shortcut")

    def test_focus_uses_app_resolver(self):
        controller = FakeController()
        engine = ComputerUseEngine(controller=controller)
        engine.handle("passe sur code", lambda _: "Visual Studio Code")
        self.assertEqual(controller.executed[0].target, "Visual Studio Code")

    def test_missing_accessibility_permission_is_explained(self):
        controller = MacComputerController()
        intent = parse_computer_intent("écris bonjour")
        with patch("computer_use.sys.platform", "darwin"), \
                patch.object(controller, "accessibility_enabled", return_value=False):
            outcome = controller.execute(intent)
        self.assertFalse(outcome.ok)
        self.assertIn("Accessibilite", outcome.message)


if __name__ == "__main__":
    unittest.main()
