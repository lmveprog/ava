import unittest

from ava.audio import turn


class TurnDetectionTests(unittest.TestCase):
    def test_probability_is_none_without_audio(self):
        self.assertIsNone(turn.probability(b""))

    def test_gate_defers_when_model_is_absent(self):
        gate = turn.TurnGate()
        if turn.available():
            self.skipTest("model installed: covered by the live checks")
        self.assertEqual(gate.decision(0.5, [b"\x00" * 320]), "unknown")

    @unittest.skipUnless(turn.available(), "smart-turn model not downloaded")
    def test_model_answers_a_probability_quickly(self):
        import time
        import numpy as np
        pcm = (np.zeros(16000, dtype=np.int16)).tobytes()
        turn.probability(pcm)                      # load once
        started = time.monotonic()
        score = turn.probability(pcm)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsInstance(score, float)
        self.assertTrue(0.0 <= score <= 1.0)

    @unittest.skipUnless(turn.available(), "smart-turn model not downloaded")
    def test_gate_fast_path_completes_on_finished_audio(self):
        gate = turn.TurnGate()
        frames = [(b"\x00\x00") * 16000]           # 1 s of silence "audio"
        verdict = gate.decision(0.3, frames)
        self.assertIn(verdict, ("complete", "wait", "unknown"))


if __name__ == "__main__":
    unittest.main()
