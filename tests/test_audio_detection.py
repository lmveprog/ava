import unittest

import numpy as np

import ava as jarvis


class AudioDetectionTests(unittest.TestCase):
    def test_resample_44100_to_16000(self):
        block = np.zeros(441, dtype=np.float32)
        pcm = jarvis.resample_for_wake(block, 44100)
        self.assertEqual(len(pcm), 160 * 2)

    def test_two_short_impulses_make_double_clap(self):
        detector = jarvis.ClapDetector()
        self.assertFalse(detector.feed(1.00, 0.60))
        self.assertFalse(detector.feed(1.05, 0.05))
        self.assertFalse(detector.feed(1.20, 0.62))
        self.assertTrue(detector.feed(1.25, 0.05))

    def test_sustained_voice_must_return_to_quiet(self):
        detector = jarvis.ClapDetector()
        detector.feed(1.00, 0.50)
        detector.feed(1.31, 0.48)
        self.assertTrue(detector.waiting_for_quiet)
        self.assertFalse(detector.feed(1.34, 0.05))
        self.assertFalse(detector.waiting_for_quiet)
        self.assertFalse(detector.feed(1.50, 0.60))
        self.assertFalse(detector.feed(1.55, 0.05))


if __name__ == "__main__":
    unittest.main()
