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

    def test_continuous_settings_are_bounded(self):
        config = normalize_config({
            "conversation": {"followup_timeout_seconds": 99, "max_continuous_turns": 100},
            "ui": {"startup_duration_seconds": 99},
        })
        self.assertEqual(config["conversation"]["followup_timeout_seconds"], 20)
        self.assertEqual(config["conversation"]["max_continuous_turns"], 30)
        self.assertEqual(config["ui"]["startup_duration_seconds"], 20)

    def test_apps_can_be_opened_on_process_start_explicitly(self):
        config = normalize_config({"morning": {"open_apps_on_start": True}})
        self.assertTrue(config["morning"]["open_apps_on_start"])

    def test_mini_plugin_is_visible_and_expanded_by_default(self):
        config = normalize_config({})
        self.assertFalse(config["ui"]["start_hidden"])
        self.assertTrue(config["ui"]["start_expanded"])
        self.assertTrue(config["ui"]["show_illustrations"])
        self.assertTrue(config["conversation"]["continuous_listening"])
        self.assertTrue(config["ui"]["startup_animation"])

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


class AdversarialInputTests(unittest.TestCase):
    """Trouve en fuzzant le pont des reglages depuis l'overlay."""

    def test_nan_falls_back_instead_of_becoming_the_maximum(self):
        # min(1.2, nan) rend 1.2 : un nan devenait silencieusement le maximum.
        clean = normalize_config({"voice": {"temperature": float("nan")}})
        self.assertEqual(clean["voice"]["temperature"], 0.65)

    def test_infinity_falls_back_too(self):
        clean = normalize_config({"voice": {"cfg_weight": float("inf")}})
        self.assertEqual(clean["voice"]["cfg_weight"], 0.35)

    def test_only_http_urls_reach_the_local_engine(self):
        for hostile in ("javascript:alert(1)", "file:///etc/passwd", "pas une url", ""):
            clean = normalize_config({"conversation": {"base_url": hostile}})
            self.assertEqual(clean["conversation"]["base_url"], "http://127.0.0.1:1234/v1")

    def test_a_real_url_survives(self):
        clean = normalize_config({"conversation": {"base_url": "https://api.local:8080/v1"}})
        self.assertEqual(clean["conversation"]["base_url"], "https://api.local:8080/v1")

    def test_saving_keeps_the_file_private(self):
        # config.json porte le secret oauth google.
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(_Path(folder) / "config.json")
            store.update({"identity": {"name": "Mathieu"}})
            self.assertEqual(store.path.stat().st_mode & 0o077, 0)
