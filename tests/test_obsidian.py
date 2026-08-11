import tempfile
import unittest
from pathlib import Path

from ava.services.obsidian import ObsidianMemory, slugify


class ObsidianMemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mem = ObsidianMemory(Path(self._tmp.name) / "Vault")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ensure_creates_the_vault_structure(self):
        self.mem.ensure()
        self.assertTrue((self.mem.vault / ".obsidian").is_dir())
        self.assertTrue((self.mem.vault / "Ava.md").is_file())
        self.assertTrue(self.mem.memory_path.is_file())
        self.assertTrue(self.mem.journal_dir.is_dir())
        self.assertTrue(self.mem.notes_dir.is_dir())

    def test_remember_links_the_fact_to_the_daily_note(self):
        fact = self.mem.remember("  matheus préfère le café  sans sucre . ")
        self.assertEqual(fact, "matheus préfère le café sans sucre")
        memory = self.mem.memory_path.read_text(encoding="utf-8")
        day = self.mem.daily_name()
        self.assertIn(f"- {fact} — retenu le [[{day}]]", memory)
        daily = self.mem.daily_path().read_text(encoding="utf-8")
        self.assertIn("[[Mémoire]]", daily)
        self.assertIn(fact, daily)

    def test_facts_strip_the_daily_suffix(self):
        self.mem.remember("le vps tourne sous pm2")
        self.assertEqual(self.mem.facts(), ["le vps tourne sous pm2"])

    def test_recall_matches_on_words_and_folds_accents(self):
        self.mem.remember("le mot de passe wifi est dans le tiroir")
        self.mem.remember("le hackathon commence vendredi")
        found = self.mem.recall("c'est quoi déjà pour le wifi ?")
        self.assertEqual(len(found), 1)
        self.assertIn("wifi", found[0])
        self.assertEqual(self.mem.recall("xyzabc"), [])

    def test_quick_note_creates_a_linked_file(self):
        path = self.mem.quick_note("Acheter du lait demain matin")
        self.assertTrue(path.is_file())
        self.assertIn("Acheter du lait", path.read_text(encoding="utf-8"))
        daily = self.mem.daily_path().read_text(encoding="utf-8")
        self.assertIn(f"[[{path.stem}]]", daily)

    def test_quick_note_never_overwrites(self):
        first = self.mem.quick_note("même contenu")
        second = self.mem.quick_note("même contenu")
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_morning_briefing_lands_in_the_daily_note(self):
        self.mem.morning_briefing("Bonjour Matheus, il fait 28 degrés à Marseille.")
        daily = self.mem.daily_path().read_text(encoding="utf-8")
        self.assertIn("briefing du matin", daily)
        self.assertIn("28 degrés", daily)

    def test_context_for_llm(self):
        self.assertEqual(self.mem.context_for_llm(), "")
        self.mem.remember("il code en python")
        self.assertIn("il code en python", self.mem.context_for_llm())

    def test_slugify(self):
        self.assertEqual(slugify("Acheter du lait, demain !"), "acheter-du-lait-demain")
        self.assertEqual(slugify("éèçà"), "eeca")
        self.assertEqual(slugify("!!!"), "note")


if __name__ == "__main__":
    unittest.main()
