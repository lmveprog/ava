"""one system: local, measured, with no cascade of fallbacks.

the switch wasn't decided on a principle ("local is better") but on numbers
taken on this machine, same text, same day:

    engine                 "Oui ?"      31 s briefing    leaves the mac
    chatterbox (local)     3-4 s        ~45 s            no
    mistral (network)      0.51 s       2.86 s           yes
    kokoro (local, mlx)    0.065 s      1.70 s           no

    transcription          an 11 s clip of french
    faster-whisper small   1.85 s   "c'est **Hava**"
    whisper-turbo (mlx)    0.32 s   "c'est **Ava**"

local stopped being the degraded fallback you put up with when the network dies:
it became the fastest path *and* the most accurate. that is what licences
deleting the stack of fallbacks — not a preference about architecture.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import app as ava
from ava.audio import voice_tts as V


class SingleEngineTest(unittest.TestCase):
    def test_the_default_depends_on_nothing(self):
        """No key, no quota, no network: that is the whole point of the switch.

        The timbre was decided by ear (chatterbox); the dependency isn't up for
        negotiation — whichever engine wins, it runs on the mac.
        """
        with patch.object(V, "_settings", return_value={}):
            self.assertIn(V.engine_name(), {"chatterbox", "kokoro"})
        self.assertNotIn(V.DEFAULT_ENGINE, {"mistral", "elevenlabs"})

    def test_the_fallback_cascade_is_gone(self):
        """Four chained engines meant four possible timbres for one sentence and
        four timeouts to pay before reaching the last one."""
        with patch.object(V, "_settings", return_value={"engine": "kokoro"}), \
                patch.object(V, "_kokoro_audio", return_value=None), \
                patch.object(V, "_mistral_audio") as distant, \
                patch.object(V, "_chatterbox_audio") as chatter, \
                patch.object(V, "_eleven_audio") as eleven:
            self.assertIsNone(V.synthesize("Bonjour"))
        for moteur in (distant, chatter, eleven):
            moteur.assert_not_called()

    def test_long_text_is_split_before_synthesis(self):
        """⚠️ kokoro's french g2p goes through espeak, which **truncates** long
        text for lack of internal splitting. without this split, the end of the
        briefing simply vanished."""
        long_texte = " ".join(f"Phrase numéro {n} du briefing." for n in range(1, 21))
        unites = V.split_speech_units(long_texte)
        self.assertGreater(len(unites), 1)
        self.assertTrue(all(len(u) <= V.MAX_CHUNK_CHARS for u in unites))


class LocalTranscriptionTest(unittest.TestCase):
    def test_the_gpu_comes_before_the_network(self):
        with patch.object(ava, "mlx_transcribe", return_value="ouvre spotify") as local, \
                patch.object(ava, "voxtral_transcribe") as distant:
            self.assertEqual(ava.transcribe(b"\x00\x01"), "ouvre spotify")
        local.assert_called_once()
        distant.assert_not_called()

    def test_voxtral_only_runs_when_asked_for(self):
        """It leaves the mac, so it must never switch itself back on."""
        with patch.object(ava, "mlx_transcribe", return_value=""), \
                patch.object(ava, "voxtral_transcribe") as distant, \
                patch.object(ava, "get_whisper") as whisper, \
                patch.dict(ava.os.environ, {}, clear=False):
            ava.os.environ.pop("AVA_USE_VOXTRAL", None)
            whisper.return_value.transcribe.return_value = ([], None)
            ava.transcribe(b"\x00\x01")
        distant.assert_not_called()

    def test_a_missing_mlx_is_not_retried_on_every_sentence(self):
        """On a machine without apple silicon, retrying the import on every
        command paid the cost of failing, every single time."""
        ava._mlx_whisper_ok = True
        self.addCleanup(setattr, ava, "_mlx_whisper_ok", True)
        with patch.dict(sys.modules, {"mlx_whisper": None}):
            self.assertIsNone(ava.mlx_transcribe(b"\x00\x01"))
        self.assertFalse(ava._mlx_whisper_ok)


class BargeInTest(unittest.TestCase):
    """You have to be able to cut her off: a 30 s briefing was indivisible."""

    def setUp(self):
        ava._player = None
        self.addCleanup(setattr, ava, "_player", None)

    def test_nothing_to_cut_when_she_is_silent(self):
        self.assertFalse(ava.stop_speaking())
        self.assertFalse(ava.speaking())

    def test_cutting_in_stops_the_player(self):
        class Lecteur:
            def __init__(self):
                self.tue = False

            def poll(self):
                return None if not self.tue else 0

            def terminate(self):
                self.tue = True

        lecteur = Lecteur()
        ava._player = lecteur
        self.assertTrue(ava.speaking())
        self.assertTrue(ava.stop_speaking())
        self.assertTrue(lecteur.tue)

    def test_the_word_stop_shuts_her_up_instead_of_running_a_command(self):
        with patch.object(ava, "stop_speaking", return_value=True) as couper, \
                patch.object(ava, "_reserve_interaction") as reserver:
            reponse = ava.submit_text_command("stop")
        couper.assert_called_once()
        reserver.assert_not_called()
        self.assertTrue(reponse.get("interrupted"))

    def test_clicking_the_mic_while_she_talks_cuts_her_off(self):
        with patch.object(ava, "stop_speaking") as couper, \
                patch.object(ava, "_reserve_interaction", return_value=False):
            ava.start_voice_interaction()
        couper.assert_called_once()


class OverlayContractTest(unittest.TestCase):
    """The python -> js bridge has to stay complete on both sides."""

    def test_l_interruption_existe_de_bout_en_bout(self):
        from ava.ui import overlay as overlay
        self.assertTrue(hasattr(overlay, "interrupted"))
        page = (Path(__file__).resolve().parents[1] / "src" / "ava" / "ui" / "web" / "ava.html").read_text(
            encoding="utf-8")
        self.assertIn("window.avaInterrupted", page)
        # what wasn't spoken has to look different from what was.
        self.assertIn("unsaid", page)


if __name__ == "__main__":
    unittest.main()
