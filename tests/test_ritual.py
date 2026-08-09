"""the big morning ritual only plays once a day."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import app as ava  # noqa: E402


class RitualOncePerDayTests(unittest.TestCase):
    def setUp(self):
        ava._ritual["day"] = None
        ava._last_trigger["ts"] = 0.0
        ava._flow_active.clear()

    def tearDown(self):
        ava._ritual["day"] = None
        ava._flow_active.clear()

    def test_the_first_greeting_of_the_day_plays_the_whole_ritual(self):
        with patch.object(ava.threading, "Thread") as thread:
            ava.trigger_welcome("bonjour ava")
        self.assertIs(thread.call_args.kwargs["target"], ava.run_welcome_flow)

    def test_the_second_one_only_says_hello(self):
        """a "bonjour ava" at 6 pm must not restart music + apps + 45 s."""
        ava.mark_ritual_done()
        with patch.object(ava.threading, "Thread") as thread:
            ava.trigger_welcome("bonjour ava")
        self.assertIs(thread.call_args.kwargs["target"], ava.run_short_greeting)

    def test_a_new_day_earns_a_new_ritual(self):
        ava.mark_ritual_done("2026-08-07")
        self.assertFalse(ava.ritual_done_today("2026-08-08"))

    def test_a_burst_of_triggers_only_starts_one_flow(self):
        # two claps back to back, or a clap doubled by a wake word.
        with patch.object(ava.threading, "Thread") as thread:
            ava.trigger_welcome("double clap")
            ava.trigger_welcome("bonjour ava")
        self.assertEqual(thread.call_count, 1)


class ShortGreetingTests(unittest.TestCase):
    def setUp(self):
        ava._flow_active.set()

    def tearDown(self):
        ava._flow_active.clear()

    def test_it_greets_by_name_and_releases_the_flow(self):
        with patch.object(ava, "speak") as speak, \
                patch.object(ava, "return_to_idle"), \
                patch.object(ava, "_drain_wake_queue"):
            ava.run_short_greeting()
        self.assertIn(ava.USER_NAME, speak.call_args.args[0])
        self.assertFalse(ava._flow_active.is_set())

    def test_it_still_releases_the_flow_when_the_voice_fails(self):
        with patch.object(ava, "speak", side_effect=RuntimeError("voix morte")), \
                patch.object(ava, "return_to_idle"), \
                patch.object(ava, "_drain_wake_queue"):
            with self.assertRaises(RuntimeError):
                ava.run_short_greeting()
        self.assertFalse(ava._flow_active.is_set())


if __name__ == "__main__":
    unittest.main()
