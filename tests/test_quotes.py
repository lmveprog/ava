"""les citations : attribuées, et pas deux fois la même de suite."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quotes  # noqa: E402


class FondsTests(unittest.TestCase):
    def test_every_quote_has_an_author(self):
        for quote in quotes.QUOTES:
            self.assertTrue(quote.author.strip(), quote.text)
            self.assertTrue(quote.text.strip())

    def test_no_duplicates(self):
        texts = [quote.text for quote in quotes.QUOTES]
        self.assertEqual(len(texts), len(set(texts)))

    def test_the_accents_are_there(self):
        """Exigence de Matheus : la synthèse écorche les mots sans accent."""
        suspects = [q.text for q in quotes.QUOTES
                    if " a " in f" {q.text} " and "à" not in q.text and "é" not in q.text]
        self.assertEqual(suspects, [])

    def test_the_spoken_form_names_the_author(self):
        quote = quotes.Quote("Le doute est le commencement de la sagesse", "Aristote")
        self.assertEqual(quote.spoken(),
                         "Le doute est le commencement de la sagesse. Aristote.")

    def test_the_spoken_form_does_not_double_the_full_stop(self):
        quote = quotes.Quote("Connais-toi toi-même.", "Socrate")
        self.assertNotIn("..", quote.spoken())

    def test_a_question_keeps_its_mark(self):
        quote = quotes.Quote("Pourquoi pas ?", "Personne")
        self.assertIn("Pourquoi pas ?", quote.spoken())


class RotationTests(unittest.TestCase):
    def test_a_recently_said_quote_is_avoided(self):
        pool = (quotes.Quote("A", "Un"), quotes.Quote("B", "Deux"))
        chosen = quotes.pick(pool, history=["A"], chooser=lambda items: items[0])
        self.assertEqual(chosen.text, "B")

    def test_when_everything_was_said_the_pool_reopens(self):
        pool = (quotes.Quote("A", "Un"), quotes.Quote("B", "Deux"))
        chosen = quotes.pick(pool, history=["A", "B"], chooser=lambda items: items[0])
        self.assertEqual(chosen.text, "A")

    def test_the_history_stays_bounded(self):
        self.assertLessEqual(quotes.HISTORY_SIZE, len(quotes.QUOTES))



class QuoteOfTheDayTests(unittest.TestCase):
    """« La citation du jour » doit être la même toute la journée."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(quotes, "TODAY_PATH", Path(self.tmp.name) / "today.json")
        patcher.start(); self.addCleanup(patcher.stop)
        history = patch.object(quotes, "HISTORY_PATH", Path(self.tmp.name) / "seen.json")
        history.start(); self.addCleanup(history.stop)
        quotes._today.update(day=None, quote=None)
        self.addCleanup(quotes._today.update, day=None, quote=None)

    def test_it_does_not_change_between_two_calls(self):
        # le briefing est refabriqué toutes les 15 min : sans ça, la « citation
        # du jour » changeait quatre fois par heure.
        self.assertEqual(quotes.of_the_day("2026-08-08").text,
                         quotes.of_the_day("2026-08-08").text)

    def test_it_survives_a_restart(self):
        first = quotes.of_the_day("2026-08-08")
        quotes._today.update(day=None, quote=None)      # simule un redémarrage
        self.assertEqual(quotes.of_the_day("2026-08-08").text, first.text)

    def test_a_new_day_brings_a_new_pick(self):
        quotes.of_the_day("2026-08-08")
        quotes._today.update(day=None, quote=None)
        self.assertEqual(quotes.of_the_day("2026-08-09").text,
                         quotes.of_the_day("2026-08-09").text)

    def test_a_corrupt_file_is_not_fatal(self):
        quotes.TODAY_PATH.write_text("{pas du json", encoding="utf-8")
        self.assertTrue(quotes.of_the_day("2026-08-08").author)

if __name__ == "__main__":
    unittest.main()
