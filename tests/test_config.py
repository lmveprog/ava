import json
from pathlib import Path
import tempfile
import unittest

from ava_config import ConfigStore, clap_min_rms, normalize_config


class ConfigTests(unittest.TestCase):
    def test_clamps_values_and_rejects_bad_apps(self):
        config = normalize_config({
            "wake": {"clap": {"sensitivity": 999}},
            "morning": {"apps": [{"name": "Safari", "position": "ailleurs"}]},
        })
        self.assertEqual(config["wake"]["clap"]["sensitivity"], 100)
        self.assertGreater(len(config["morning"]["apps"]), 0)

    def test_clap_gap_remains_ordered(self):
        config = normalize_config({
            "wake": {"clap": {"min_gap_ms": 500, "max_gap_ms": 150}},
        })
        clap = config["wake"]["clap"]
        self.assertGreater(clap["max_gap_ms"], clap["min_gap_ms"])

    def test_sensitivity_maps_to_lower_threshold(self):
        self.assertLess(clap_min_rms(80), clap_min_rms(20))

    def test_startup_hint_is_bounded(self):
        config = normalize_config({"ui": {"startup_hint_seconds": 999}})
        self.assertEqual(config["ui"]["startup_hint_seconds"], 15)

    def test_patch_is_atomic_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)
            received = []
            store.subscribe(received.append)
            result = store.update({"identity": {"city": "Paris"}})
            self.assertEqual(result["identity"]["city"], "Paris")
            self.assertEqual(received[0]["identity"]["city"], "Paris")
            with path.open(encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["identity"]["city"], "Paris")
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
