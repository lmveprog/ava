import unittest

import numpy as np

from ava import app as jarvis


class AudioDetectionTests(unittest.TestCase):
    def test_resample_44100_to_16000(self):
        block = np.zeros(441, dtype=np.float32)
        pcm = jarvis.resample_for_wake(block, 44100)
        self.assertEqual(len(pcm), 160 * 2)

    # niveaux mesures le 08/08 : mains 0,69-2,08 ; frappe clavier 0,28-0,44.
    def test_two_short_impulses_make_double_clap(self):
        detector = jarvis.ClapDetector()
        self.assertFalse(detector.feed(1.00, 0.90))
        self.assertFalse(detector.feed(1.05, 0.05))
        self.assertFalse(detector.feed(1.20, 0.95))
        self.assertTrue(detector.feed(1.25, 0.05))

    def test_typing_never_makes_a_clap(self):
        # deux touches a 200 ms d'intervalle : c'est ce qui reveillait Ava
        # toute seule pendant que Matheus travaillait.
        detector = jarvis.ClapDetector()
        for moment, level in ((1.00, 0.33), (1.05, 0.02), (1.20, 0.41), (1.25, 0.02),
                              (1.44, 0.29), (1.49, 0.02)):
            self.assertFalse(detector.feed(moment, level))

    def test_a_lopsided_pair_is_not_a_double_clap(self):
        # une main + un choc quelconque : les deux coups doivent se ressembler.
        detector = jarvis.ClapDetector()
        self.assertFalse(detector.feed(1.00, 2.00))
        self.assertFalse(detector.feed(1.05, 0.05))
        self.assertFalse(detector.feed(1.20, 0.60))
        self.assertFalse(detector.feed(1.25, 0.05))

    def test_sustained_voice_must_return_to_quiet(self):
        detector = jarvis.ClapDetector()
        detector.feed(1.00, 0.80)
        detector.feed(1.31, 0.78)
        self.assertTrue(detector.waiting_for_quiet)
        self.assertFalse(detector.feed(1.34, 0.05))
        self.assertFalse(detector.waiting_for_quiet)
        self.assertFalse(detector.feed(1.50, 0.90))
        self.assertFalse(detector.feed(1.55, 0.05))

    def test_adaptive_gate_ignores_room_tone_and_ends_after_silence(self):
        gate = jarvis.AdaptiveSpeechGate(silence_s=0.30, min_speech_s=0.10)
        for _ in range(30):
            self.assertFalse(gate.feed(0.004, 0.01))
        self.assertFalse(gate.started)
        for _ in range(16):
            self.assertFalse(gate.feed(0.035, 0.01))
        self.assertTrue(gate.started)
        complete = False
        for _ in range(35):
            complete = gate.feed(0.002, 0.01)
        self.assertTrue(complete)

    def test_capture_uses_a_dedicated_queue(self):
        jarvis._drain_command_queue()
        jarvis._capture_audio.set()
        try:
            jarvis.push_wake_audio(np.full(480, 0.1, dtype=np.float32), 48000)
            pcm = jarvis._command_q.get_nowait()
        finally:
            jarvis._capture_audio.clear()
            jarvis._drain_command_queue()
        self.assertEqual(len(pcm), 160 * 2)

    def test_clean_transcript_removes_wake_word_and_hallucinations(self):
        self.assertEqual(
            jarvis.clean_transcript("OK Ava ouvre Spotify"),
            "ouvre spotify",
        )
        self.assertEqual(
            jarvis.clean_transcript("Merci d'avoir regardé cette vidéo !"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
