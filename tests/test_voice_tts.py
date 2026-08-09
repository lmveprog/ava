import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava.audio import voice_tts as V  # noqa: E402


class EngineChoiceTests(unittest.TestCase):
    def test_default_engine_is_the_local_one(self):
        """Le defaut est local : ava parle sans reseau, sans quota, sans compte."""
        with patch.object(V, "_settings", return_value={}):
            self.assertEqual(V.engine_name(), "chatterbox")

    def test_an_unknown_engine_falls_back_to_the_default(self):
        with patch.object(V, "_settings", return_value={"engine": "siri"}):
            self.assertEqual(V.engine_name(), "chatterbox")

    def test_kokoro_ne_retombe_sur_aucun_autre(self):
        """Un seul chemin : si kokoro ne rend rien, on passe a `say`, pas a un
        troisieme moteur au timbre different."""
        with patch.object(V, "_settings", return_value={"engine": "kokoro"}), \
                patch.object(V, "_kokoro_audio", return_value=None) as local, \
                patch.object(V, "_mistral_audio") as distant, \
                patch.object(V, "_chatterbox_audio") as chatter, \
                patch.object(V, "_eleven_audio") as eleven:
            self.assertIsNone(V.synthesize("Bonjour"))
        local.assert_called_once()
        distant.assert_not_called()
        chatter.assert_not_called()
        eleven.assert_not_called()

    def test_system_engine_means_no_audio_file(self):
        with patch.object(V, "_settings", return_value={"engine": "system"}):
            self.assertIsNone(V.synthesize("Bonjour"))

    def test_mistral_falls_back_to_chatterbox_when_offline(self):
        with patch.object(V, "_settings", return_value={"engine": "mistral"}), \
                patch.object(V, "_mistral_audio", return_value=None) as remote, \
                patch.object(V, "_chatterbox_audio", return_value=Path("/tmp/x.wav")) as local:
            self.assertEqual(V.synthesize("Bonjour"), Path("/tmp/x.wav"))
        remote.assert_called_once()
        local.assert_called_once()

    def test_le_moteur_choisi_ne_saute_pas_sur_un_autre_timbre(self):
        """Une phrase ratee sortait avec une **autre voix**, et on croyait a un
        bug du timbre alors que c'etait un repli. Le moteur choisi est le
        moteur entendu ; si rien ne sort, `say` prend le relais."""
        with patch.object(V, "_settings", return_value={"engine": "chatterbox"}), \
                patch.object(V, "_chatterbox_audio", return_value=None) as local, \
                patch.object(V, "_mistral_audio") as distant, \
                patch.object(V, "_eleven_audio") as eleven:
            self.assertIsNone(V.synthesize("Bonjour"))
        local.assert_called_once()
        distant.assert_not_called()
        eleven.assert_not_called()

    def test_empty_text_is_never_synthesised(self):
        with patch.object(V, "_mistral_audio") as remote, \
                patch.object(V, "_chatterbox_audio") as local:
            self.assertIsNone(V.synthesize("   "))
        remote.assert_not_called()
        local.assert_not_called()

    def test_prewarm_never_loads_the_local_model_for_other_engines(self):
        with patch.object(V, "_settings", return_value={"engine": "elevenlabs"}), \
                patch.object(V, "synthesize"), \
                patch.object(V, "prune_cache"), \
                patch.object(V, "_load_model") as load:
            V.prewarm(phrases=())
            time.sleep(0.2)
        load.assert_not_called()

    def test_prewarm_on_mistral_skips_the_local_model_entirely(self):
        """Le gros gain au demarrage : plus de 7,5 s de chargement gpu pour rien."""
        with patch.object(V, "_settings", return_value={"engine": "mistral"}), \
                patch.object(V, "prune_cache"), \
                patch.object(V, "synthesize") as synth, \
                patch.object(V, "_load_model") as load:
            V.prewarm(phrases=("Oui ?",))
            time.sleep(0.3)
        load.assert_not_called()
        synth.assert_called_once_with("Oui ?")


class MoodTests(unittest.TestCase):
    """La voix prend la couleur de ce qu'elle dit — mais sobrement."""

    def test_a_greeting_sounds_happy(self):
        self.assertEqual(V.mood_for("Bonjour Mathieu, on est samedi."), "happy")

    def test_a_question_sounds_curious(self):
        self.assertEqual(V.mood_for("Tu confirmes cette action ?"), "curious")

    def test_an_apology_sounds_sad(self):
        self.assertEqual(V.mood_for("Désolée, je n'ai pas réussi."), "sad")

    def test_anything_else_stays_neutral(self):
        self.assertEqual(V.mood_for("J'ouvre Spotify."), "neutral")

    def test_empty_text_stays_neutral(self):
        self.assertEqual(V.mood_for(""), "neutral")

    def test_the_mood_picks_a_real_french_voice(self):
        with patch.object(V, "_settings", return_value={"expressive": True}):
            self.assertEqual(V.mistral_voice("happy"), "fr_marie_happy")

    def test_expressiveness_can_be_switched_off(self):
        with patch.object(V, "_settings",
                          return_value={"expressive": False,
                                        "mistral_voice": "fr_marie_neutral"}):
            self.assertEqual(V.mistral_voice("happy"), "fr_marie_neutral")


class SpeechUnitTests(unittest.TestCase):
    """Le decoupage qui permet d'envoyer les phrases en parallele."""

    def test_sentences_are_not_regrouped(self):
        units = V.split_speech_units(
            "Bonjour Mathieu, on est samedi. Il fait vingt-six degrés à Marseille. "
            "Ton agenda est chargé aujourd'hui.")
        self.assertEqual(len(units), 3)

    def test_a_tiny_fragment_is_glued_to_the_previous_one(self):
        # « Voilà. » ne vaut pas son propre aller-retour reseau.
        units = V.split_speech_units(
            "Ton agenda du jour tient en une seule ligne cette fois-ci. Voilà.")
        self.assertEqual(len(units), 1)
        self.assertTrue(units[0].endswith("Voilà."))

    def test_empty_text_gives_no_unit(self):
        self.assertEqual(V.split_speech_units("   "), [])


class LazyImportRaceTests(unittest.TestCase):
    """transformers n'aime pas etre importe depuis deux threads a la fois."""

    def test_a_transient_import_error_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ImportError("cannot import name 'LlamaModel' from 'transformers'")
            return "modele"

        self.assertEqual(V._import_chatterbox(flaky, pause=0), "modele")
        self.assertEqual(calls["n"], 3)

    def test_a_permanent_import_error_still_raises(self):
        def broken():
            raise ImportError("chatterbox pas installe")

        with self.assertRaises(ImportError):
            V._import_chatterbox(broken, attempts=2, pause=0)

    def test_a_working_import_is_not_retried(self):
        calls = {"n": 0}

        def fine():
            calls["n"] += 1
            return "modele"

        self.assertEqual(V._import_chatterbox(fine, pause=0), "modele")
        self.assertEqual(calls["n"], 1)


class ChunkingTests(unittest.TestCase):
    def test_short_text_stays_in_one_piece(self):
        self.assertEqual(V.split_sentences("Bonjour Mathieu."), ["Bonjour Mathieu."])

    def test_sentences_are_grouped_up_to_the_limit(self):
        chunks = V.split_sentences("Un. Deux. Trois.", limit=10)
        self.assertEqual(chunks, ["Un. Deux.", "Trois."])

    def test_a_very_long_sentence_is_cut_on_a_comma(self):
        text = "a" * 40 + ", " + "b" * 40
        chunks = V.split_sentences(text, limit=50)
        self.assertTrue(all(len(chunk) <= 50 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(V.split_sentences("   "), [])

    def test_accents_survive_the_split(self):
        chunks = V.split_sentences("On est samedi 8 août. J'espère que ça va.")
        self.assertIn("août", chunks[0])
        self.assertIn("espère", chunks[0])



class GenerationGuardTests(unittest.TestCase):
    """Chatterbox part en roue libre sur les textes courts : « Oui ? » a donne
    3,3 s de babillage la ou on en attend 0,9."""

    def test_expected_duration_grows_with_the_words(self):
        short = V.expected_seconds("Oui ?")
        long_ = V.expected_seconds("Au programme aujourd'hui : à 9 heures, point produit.")
        self.assertLess(short, long_)

    def test_expected_duration_has_a_floor(self):
        self.assertGreaterEqual(V.expected_seconds(""), 0.55)

    def test_word_delays_follow_the_measured_sentences(self):
        marks = [{"text": "Un deux", "start_ms": 0, "ms": 1000},
                 {"text": "trois", "start_ms": 1140, "ms": 500}]
        with patch.object(Path, "read_text", return_value=json.dumps(marks)):
            delays = V.word_delays(Path("/tmp/x.wav"), "Un deux trois", 1640)
        self.assertEqual(delays, [0, 500, 1140])

    def test_word_delays_fall_back_to_a_linear_spread(self):
        delays = V.word_delays(None, "un deux trois quatre", 4000)
        self.assertEqual(len(delays), 4)
        self.assertEqual(delays[0], 0)
        self.assertLess(delays[1], delays[2])

    def test_word_delays_never_run_short_of_words(self):
        marks = [{"text": "Un", "start_ms": 0, "ms": 400}]
        with patch.object(Path, "read_text", return_value=json.dumps(marks)):
            delays = V.word_delays(Path("/tmp/x.wav"), "Un deux trois", 1200)
        self.assertEqual(len(delays), 3)

    def test_no_words_no_delays(self):
        self.assertEqual(V.word_delays(None, "   ", 1000), [])


class CacheHousekeepingTests(unittest.TestCase):
    """Un fichier par phrase jamais redite : sans menage, ca finit en gigaoctets."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _make(self, name, size=1024, age_days=0):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    def test_a_fresh_small_cache_is_left_alone(self):
        keep = self._make("a.wav")
        with patch.object(V, "CACHE_DIR", self.dir):
            self.assertEqual(V.prune_cache(), 0)
        self.assertTrue(keep.exists())

    def test_old_files_go_away(self):
        old = self._make("old.wav", age_days=90)
        fresh = self._make("new.wav", age_days=1)
        with patch.object(V, "CACHE_DIR", self.dir):
            V.prune_cache(max_age_days=30)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_the_timing_sidecar_follows_its_audio(self):
        self._make("old.wav", age_days=90)
        sidecar = self._make("old.timing.json", age_days=90)
        with patch.object(V, "CACHE_DIR", self.dir):
            V.prune_cache(max_age_days=30)
        self.assertFalse(sidecar.exists())

    def test_the_oldest_go_first_when_the_folder_is_too_big(self):
        oldest = self._make("1.wav", size=2000, age_days=5)
        newest = self._make("2.wav", size=2000, age_days=1)
        with patch.object(V, "CACHE_DIR", self.dir):
            V.prune_cache(max_bytes=2500, max_age_days=365)
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())

    def test_a_missing_folder_is_not_an_error(self):
        with patch.object(V, "CACHE_DIR", self.dir / "nulle-part"):
            self.assertEqual(V.prune_cache(), 0)


class RetryBudgetTests(unittest.TestCase):
    """Le garde-fou anti-derive ne doit pas tripler la latence pour rien."""

    def test_short_lines_get_a_wide_window(self):
        low, high = V.acceptable_ratio("Oui ?")
        self.assertLess(low, 0.4)
        self.assertGreater(high, 2.0)

    def test_long_lines_get_a_tight_window(self):
        low, high = V.acceptable_ratio(" ".join(["mot"] * 25))
        self.assertGreaterEqual(low, 0.55)
        self.assertLessEqual(high, 1.8)

    def test_a_runaway_short_line_is_still_caught(self):
        # « Oui ? » a 3,28 s pour 0,9 attendu = x3,6 : au-dela de la fenetre.
        low, high = V.acceptable_ratio("Oui ?")
        self.assertGreater(3.28 / V.expected_seconds("Oui ?"), high)

    def test_long_lines_are_replayed_at_most_once(self):
        self.assertEqual(V._tries_for(" ".join(["mot"] * 30)), 2)
        self.assertEqual(V._tries_for("Oui ?"), V.MAX_TRIES)

if __name__ == "__main__":
    unittest.main()


class ToolPathTests(unittest.TestCase):
    """Lancée par le launchagent, ava n'hérite que de /usr/bin:/bin:/usr/sbin:/sbin."""

    def setUp(self):
        V._tools.clear()
        self.addCleanup(V._tools.clear)

    def test_a_homebrew_tool_is_found_without_it_being_on_the_path(self):
        # sans ça, ffmpeg était introuvable une fois ava démarrée automatiquement,
        # le recollage échouait en silence et TOUT briefing de plus d'une phrase
        # devenait muet — alors que tout marchait lancé depuis un terminal.
        with patch("shutil.which", return_value=None), \
                patch.object(Path, "is_file", return_value=True), \
                patch("os.access", return_value=True):
            self.assertEqual(V.tool_path("ffmpeg"), "/opt/homebrew/bin/ffmpeg")

    def test_the_path_is_preferred_when_it_answers(self):
        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            self.assertEqual(V.tool_path("ffmpeg"), "/usr/local/bin/ffmpeg")

    def test_an_unknown_tool_is_left_to_the_system(self):
        with patch("shutil.which", return_value=None), \
                patch.object(Path, "is_file", return_value=False):
            self.assertEqual(V.tool_path("inconnu"), "inconnu")

    def test_the_answer_is_cached(self):
        with patch("shutil.which", return_value="/usr/bin/say") as which:
            V.tool_path("say")
            V.tool_path("say")
        which.assert_called_once()
