"""l'actualité du briefing : fraîche, lisible à voix haute, jamais redondante."""

import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_news  # noqa: E402


def item(title, source="OpenAI", hours=1.0):
    published = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours))
    return ai_news.NewsItem(title, source, "https://exemple.test/a", published)


class FreshnessTests(unittest.TestCase):
    def test_something_too_old_is_never_picked(self):
        """Sans ça, ava annonçait une annonce vieille de plusieurs jours."""
        old = [item("Introducing a new model", hours=24 * 30)]
        self.assertIsNone(ai_news.pick(old))

    def test_the_fresh_window_wins_over_raw_relevance(self):
        # une annonce majeure d'il y a 5 jours ne passe pas devant hier.
        candidates = [
            item("Introducing our new frontier model", hours=24 * 5),
            item("AI tutors and classroom research", source="Hugging Face", hours=20),
        ]
        self.assertEqual(ai_news.pick(candidates).title, candidates[1].title)

    def test_corporate_fluff_loses_to_a_real_announcement(self):
        candidates = [
            item("How HSP GRUPPE builds AI capabilities for tax advisory", hours=3),
            item("Introducing a new reasoning model", hours=10),
        ]
        self.assertIn("Introducing", ai_news.pick(candidates).title)

    def test_the_story_already_told_is_skipped(self):
        candidates = [
            item("Introducing Shieldstral the guardrail model", hours=2),
            item("A new benchmark for coding agents", hours=6),
        ]
        chosen = ai_news.pick(candidates, "Introducing Shieldstral the guardrail model")
        self.assertIn("benchmark", chosen.title)

    def test_an_empty_feed_gives_nothing(self):
        self.assertIsNone(ai_news.pick([]))


class FreshnessPhraseTests(unittest.TestCase):
    """Sans repère de temps, une annonce de mardi sonne comme celle du jour."""

    def setUp(self):
        self.now = datetime.datetime(2026, 8, 8, 10, 0, tzinfo=datetime.timezone.utc)

    def phrase(self, hours):
        return ai_news.freshness_phrase(self.now - datetime.timedelta(hours=hours), self.now)

    def test_this_morning(self):
        self.assertEqual(self.phrase(2), "ce matin")

    def test_yesterday(self):
        self.assertEqual(self.phrase(24), "hier")

    def test_the_day_before(self):
        self.assertEqual(self.phrase(48), "avant-hier")

    def test_further_back_is_counted_in_days(self):
        self.assertEqual(self.phrase(24 * 4), "il y a 4 jours")

    def test_no_date_means_no_claim(self):
        self.assertEqual(ai_news.freshness_phrase(None), "")


class SentenceTests(unittest.TestCase):
    def test_the_source_is_not_repeated_when_the_title_carries_it(self):
        # « Mistral AI présente Shieldstral, selon Mistral AI. »
        sentence = ai_news.sentence({"title": "Mistral AI présente Shieldstral",
                                     "source": "Mistral AI", "freshness": "hier"})
        self.assertEqual(sentence.count("Mistral AI"), 1)

    def test_a_question_title_keeps_its_question_mark_and_nothing_else(self):
        sentence = ai_news.sentence({"title": "Les tuteurs IA savent-ils s'abstenir ?",
                                     "source": "Hugging Face", "freshness": "ce matin"})
        self.assertTrue(sentence.endswith("?"))
        self.assertNotIn("?.", sentence)

    def test_a_plain_title_gets_a_full_stop(self):
        sentence = ai_news.sentence({"title": "Un nouveau modèle de prévision",
                                     "source": "Google DeepMind", "freshness": ""})
        self.assertTrue(sentence.endswith("."))

    def test_no_title_means_no_sentence(self):
        self.assertEqual(ai_news.sentence({"title": "", "source": "OpenAI"}), "")

    def test_the_sentence_never_holds_two_colons(self):
        sentence = ai_news.sentence({"title": "TutorMoments : les tuteurs IA",
                                     "source": "Hugging Face", "freshness": "hier"})
        self.assertLessEqual(sentence.count(":"), 1)


class TranslationTests(unittest.TestCase):
    def test_un_titre_francais_est_reformule_lui_aussi(self):
        """⚠️ un titre de presse francais n'est pas plus dicible qu'un anglais :
        « Nouveau modele pour les agents » n'a pas de verbe. On ne traduit plus,
        on **reecrit** — donc le francais passe aussi par le modele."""
        reponse = {"choices": [{"message": {"content": "Mistral publie un nouveau modèle pour les agents"}}]}
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(ai_news, "_remember_translation"), \
                patch.object(ai_news, "_load_translations", return_value={}), \
                patch.object(ai_news.requests, "post") as post:
            post.return_value = Mock(status_code=200, raise_for_status=Mock(),
                                     json=Mock(return_value=reponse))
            dit = ai_news.translate_title("Un nouveau modèle pour les agents")
        post.assert_called_once()
        self.assertIn("publie", dit)

    def test_hors_ligne_le_titre_brut_vaut_mieux_que_rien(self):
        import net
        net.note_failure("actu:traduction", ConnectionError("coupé"))
        self.addCleanup(net.reset)
        with patch.object(ai_news.requests, "post") as post:
            self.assertEqual(ai_news.translate_title("A brand new model"),
                             "A brand new model")
        post.assert_not_called()

    def test_an_english_title_is_detected(self):
        self.assertTrue(ai_news.looks_english("Continuous voice interaction with GPT Live"))
        self.assertFalse(ai_news.looks_english("Mistral présente un modèle pour les agents"))

    def test_a_failed_translation_keeps_the_original(self):
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "x"}, clear=False), \
                patch.object(ai_news, "_load_translations", return_value={}), \
                patch.object(ai_news.requests, "post", side_effect=OSError("réseau")):
            self.assertEqual(ai_news.translate_title("Introducing the new model for agents"),
                             "Introducing the new model for agents")


if __name__ == "__main__":
    unittest.main()
