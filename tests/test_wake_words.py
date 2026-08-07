import unittest

from wake_words import PartialWakeGate, extract_wake


class WakeWordTests(unittest.TestCase):
    def test_bonjour_ava(self):
        result = extract_wake("Bonjour Ava")
        self.assertTrue(result.detected)
        self.assertEqual(result.trailing_command, "")

    def test_ok_ava_with_command(self):
        result = extract_wake("OK Ava ouvre Spotify")
        self.assertTrue(result.detected)
        self.assertEqual(result.trailing_command, "ouvre spotify")

    def test_rejects_ava_alone(self):
        self.assertFalse(extract_wake("Ava est un joli nom").detected)

    def test_stable_partial_wakes_without_waiting_for_final_result(self):
        gate = PartialWakeGate(hold_seconds=0.2)
        self.assertFalse(gate.feed("bonjour ava", now=10.0).detected)
        self.assertFalse(gate.feed("bonjour ava", now=10.1).detected)
        self.assertTrue(gate.feed("bonjour ava", now=10.21).detected)

    def test_partial_command_waits_for_final_result(self):
        gate = PartialWakeGate(hold_seconds=0.2)
        self.assertFalse(gate.feed("bonjour ava ouvre spotify", now=10.0).detected)
        self.assertFalse(gate.feed("bonjour ava ouvre spotify", now=10.3).detected)


if __name__ == "__main__":
    unittest.main()
