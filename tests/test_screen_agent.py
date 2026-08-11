import unittest

from ava.mac.screen_agent import extract_json, looks_risky


class ScreenAgentTests(unittest.TestCase):
    def test_extract_json_from_a_markdown_fence(self):
        raw = ('voilà :\n```json\n{"action": "click", "x": 10, "y": 20, '
               '"say": "je clique"}\n```')
        step = extract_json(raw)
        self.assertEqual(step["action"], "click")
        self.assertEqual(step["x"], 10)

    def test_extract_json_rejects_prose(self):
        self.assertIsNone(extract_json("je ne sais pas quoi faire"))

    def test_risky_actions_are_flagged(self):
        self.assertTrue(looks_risky({"say": "je clique sur Envoyer", "text": ""}))
        self.assertTrue(looks_risky({"say": "", "text": "confirmer le paiement"}))
        self.assertFalse(looks_risky({"say": "j'ouvre les réglages", "text": ""}))


if __name__ == "__main__":
    unittest.main()
