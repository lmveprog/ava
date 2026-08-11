import unittest

from ava.mac.screen_agent import extract_json, looks_risky, parse_holo_action


class ScreenAgentTests(unittest.TestCase):
    def test_extract_json_from_a_markdown_fence(self):
        raw = ('voilà :\n```json\n{"action": "click", "x": 10, "y": 20, '
               '"say": "je clique"}\n```')
        step = extract_json(raw)
        self.assertEqual(step["action"], "click")
        self.assertEqual(step["x"], 10)

    def test_extract_json_rejects_prose(self):
        self.assertIsNone(extract_json("je ne sais pas quoi faire"))

    def test_holo_dialect_click_is_translated(self):
        step = parse_holo_action("Click(x=512, y=300)")
        self.assertEqual((step["action"], step["x"], step["y"]), ("click", 512, 300))
        step = parse_holo_action("CLICK <point>[[981, 44]]</point>")
        self.assertEqual((step["action"], step["x"], step["y"]), ("click", 981, 44))
        step = parse_holo_action("double_click(200, 100)")
        self.assertEqual(step["action"], "double_click")

    def test_holo_dialect_type_press_scroll(self):
        self.assertEqual(parse_holo_action('type(content="bonjour")')["text"], "bonjour")
        self.assertEqual(parse_holo_action('press("enter")')["text"], "return")
        self.assertEqual(parse_holo_action("scroll(down)")["action"], "scroll_down")

    def test_holo_dialect_done_and_prose(self):
        self.assertEqual(parse_holo_action("Done. The folder is open.")["action"], "done")
        self.assertIsNone(parse_holo_action("je réfléchis encore un peu"))

    def test_truncated_json_is_salvaged(self):
        step = parse_holo_action('{"action": "click", "x": 27, "y": 961, "text": "",')
        self.assertEqual((step["action"], step["x"], step["y"]), ("click", 27, 961))

    def test_risky_actions_are_flagged(self):
        self.assertTrue(looks_risky({"say": "je clique sur Envoyer", "text": ""}))
        self.assertTrue(looks_risky({"say": "", "text": "confirmer le paiement"}))
        self.assertFalse(looks_risky({"say": "j'ouvre les réglages", "text": ""}))


if __name__ == "__main__":
    unittest.main()
