"""the intent router must never trust what the model hands back."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava.brain import understanding as U  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_a_normal_answer_is_read(self):
        result = U.parse_understanding(
            '{"intent":"volume","cible":"baisser","valeur":null,"confiance":0.93}')
        self.assertEqual(result.intent, "volume")
        self.assertEqual(result.target, "baisser")
        self.assertIsNone(result.value)
        self.assertAlmostEqual(result.confidence, 0.93)
        self.assertTrue(result.usable)

    def test_an_invented_intent_is_refused(self):
        # the model isn't allowed to promise an action that doesn't exist.
        result = U.parse_understanding(
            '{"intent":"commander_pizza","cible":"4 fromages","confiance":0.99}')
        self.assertEqual(result.intent, "inconnu")
        self.assertFalse(result.usable)

    def test_broken_json_is_not_an_error(self):
        self.assertFalse(U.parse_understanding("je dirais du volume").usable)
        self.assertFalse(U.parse_understanding("").usable)
        self.assertFalse(U.parse_understanding("[1,2,3]").usable)

    def test_a_target_that_is_an_object_is_dropped(self):
        result = U.parse_understanding(
            '{"intent":"minuteur","cible":{"unite":"minutes"},"valeur":600,"confiance":0.9}')
        self.assertEqual(result.target, "")
        self.assertEqual(result.value, 600)

    def test_nan_confidence_never_becomes_certainty(self):
        result = U.parse_understanding('{"intent":"volume","confiance":NaN}')
        self.assertEqual(result.confidence, 0.0)
        self.assertFalse(result.usable)

    def test_confidence_is_clamped(self):
        self.assertEqual(
            U.parse_understanding('{"intent":"heure","confiance":7}').confidence, 1.0)
        self.assertEqual(
            U.parse_understanding('{"intent":"heure","confiance":-3}').confidence, 0.0)

    def test_an_infinite_value_is_dropped(self):
        result = U.parse_understanding('{"intent":"minuteur","valeur":Infinity,"confiance":0.9}')
        self.assertIsNone(result.value)


class ThresholdTests(unittest.TestCase):
    """Getting "verrouiller" wrong costs far more than a skipped track."""

    def test_a_disruptive_action_needs_real_certainty(self):
        tepid = U.Understanding("verrouiller", confidence=0.7)
        self.assertFalse(tepid.usable)
        sure = U.Understanding("verrouiller", confidence=0.95)
        self.assertTrue(sure.usable)

    def test_a_harmless_action_passes_more_easily(self):
        self.assertTrue(U.Understanding("musique_suivant", confidence=0.6).usable)


class RouterTests(unittest.TestCase):
    def setUp(self):
        # never against the real cache: we neither pollute nor depend on it.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.router = U.IntentRouter(cache_path=Path(self.tmp.name) / "intents.json")

    def test_no_key_means_no_call(self):
        with patch.dict("os.environ", {"MISTRAL_API_KEY": ""}, clear=False), \
                patch.object(self.router, "_classify") as classify:
            self.assertFalse(self.router.understand("baisse le son").usable)
        classify.assert_not_called()

    def test_the_same_phrase_is_only_classified_once(self):
        answer = U.Understanding("volume", "baisser", None, 0.9)
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(self.router, "_classify", return_value=answer) as classify:
            self.router.understand("Baisse le son")
            self.router.understand("baisse le son")   # meme phrase, autre casse
        classify.assert_called_once()

    def test_a_network_failure_opens_the_breaker(self):
        # otherwise every command after would pay the full timeout.
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(self.router, "_classify", side_effect=OSError("réseau")):
            self.assertFalse(self.router.understand("baisse le son").usable)
            self.assertFalse(self.router.available())

    def test_an_empty_phrase_is_never_sent(self):
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(self.router, "_classify") as classify:
            self.assertFalse(self.router.understand("   ").usable)
        classify.assert_not_called()


class PersistentCacheTests(unittest.TestCase):
    """What ava understood once, she still knows after a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "intents.json"

    def router(self):
        return U.IntentRouter(cache_path=self.path)

    def test_a_learned_phrase_survives_a_restart(self):
        answer = U.Understanding("ouvrir_app", "Spotify", None, 0.95)
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(U.IntentRouter, "_classify", return_value=answer):
            self.router().understand("il me faudrait spotify")

        revived = self.router()
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(revived, "_classify") as classify:
            result = revived.understand("il me faudrait spotify")
        classify.assert_not_called()          # plus aucun appel reseau
        self.assertEqual(result.intent, "ouvrir_app")
        self.assertEqual(result.target, "Spotify")

    def test_a_tampered_cache_cannot_smuggle_an_action(self):
        # the file sits in clear text in .cache, so it goes back through validation.
        self.path.write_text(
            '{"coucou": {"intent": "verrouiller", "confiance": 0.6},'
            ' "salut": {"intent": "formater_le_disque", "confiance": 1.0}}',
            encoding="utf-8")
        cache = self.router()._cache
        self.assertNotIn("coucou", cache)     # sous le seuil de « verrouiller »
        self.assertNotIn("salut", cache)      # intention inexistante

    def test_a_corrupt_cache_file_is_ignored(self):
        self.path.write_text("{ ceci n'est pas du json", encoding="utf-8")
        self.assertEqual(self.router()._cache, {})


if __name__ == "__main__":
    unittest.main()
