"""the log book: measuring without ever recording what was said."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import traces as traces  # noqa: E402


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "traces.jsonl"
        patcher = patch.object(traces, "TRACE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self):
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_span_records_a_duration_and_its_fields(self):
        with traces.span("commande", route="integre") as trace:
            trace["network"] = False
        entry = self.lines()[0]
        self.assertEqual(entry["event"], "commande")
        self.assertEqual(entry["route"], "integre")
        self.assertFalse(entry["network"])
        self.assertGreaterEqual(entry["ms"], 0)

    def test_a_span_records_even_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with traces.span("commande", route="echec"):
                raise ValueError("boum")
        self.assertEqual(self.lines()[0]["route"], "echec")

    def test_nothing_spoken_can_be_written_even_by_mistake(self):
        """A log of everything you say to your assistant has no business here.

        The guarantee is enforced, not just promised: a distracted caller
        passing the user's command in gets it refused.
        """
        traces.record("commande", 0.1, route="integre",
                      text="ouvre mon relevé bancaire",
                      phrase="mon mot de passe est 1234")
        entry = self.lines()[0]
        self.assertNotIn("text", entry)
        self.assertNotIn("phrase", entry)
        self.assertNotIn("bancaire", self.path.read_text(encoding="utf-8"))

    def test_a_field_of_an_odd_type_is_dropped(self):
        traces.record("commande", 0.1, route="integre", count={"secret": 1})
        self.assertNotIn("count", self.lines()[0])

    def test_recording_never_raises_when_the_disk_refuses(self):
        with patch.object(Path, "open", side_effect=OSError("disque plein")):
            traces.record("commande", 0.1, route="integre")   # ne doit pas lever

    def test_it_can_be_switched_off(self):
        traces.set_enabled(False)
        self.addCleanup(traces.set_enabled, True)
        traces.record("commande", 0.1, route="integre")
        self.assertFalse(self.path.exists())


class SummaryTests(unittest.TestCase):
    def rows(self):
        return [
            {"event": "commande", "ms": 100, "route": "integre", "network": False},
            {"event": "commande", "ms": 300, "route": "integre", "network": False},
            {"event": "commande", "ms": 500, "route": "comprehension", "network": True},
            {"event": "commande", "ms": 900, "route": "comprehension", "network": False},
            {"event": "voix", "ms": 40, "route": "cache"},
        ]

    def test_routes_are_counted_separately(self):
        stats = {stat.label: stat for stat in traces.summary("commande", self.rows())}
        self.assertEqual(stats["integre"].count, 2)
        self.assertEqual(stats["comprehension"].count, 2)

    def test_the_network_share_is_computed(self):
        stats = {stat.label: stat for stat in traces.summary("commande", self.rows())}
        self.assertEqual(stats["comprehension"].network_share, 0.5)
        self.assertEqual(stats["integre"].network_share, 0.0)

    def test_another_event_is_not_mixed_in(self):
        labels = [stat.label for stat in traces.summary("commande", self.rows())]
        self.assertNotIn("cache", labels)

    def test_the_busiest_route_comes_first(self):
        rows = self.rows() + [
            {"event": "commande", "ms": 10, "route": "integre", "network": False}]
        self.assertEqual(traces.summary("commande", rows)[0].label, "integre")

    def test_an_empty_journal_reads_cleanly(self):
        self.assertEqual(traces.summary("commande", []), [])


if __name__ == "__main__":
    unittest.main()
