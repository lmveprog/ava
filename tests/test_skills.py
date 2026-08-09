"""les compétences au format Agent Skills : découverte, garde-fous, exécution."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import skills  # noqa: E402


def write_skill(root: Path, folder_name: str, body: str = "Instructions.", **meta) -> Path:
    folder = root / folder_name
    (folder / "scripts").mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", body]
    (folder / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return folder


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_well_formed_skill_is_found(self):
        write_skill(self.root, "meteo-mer", name="meteo-mer",
                    description="Donne l'état de la mer.")
        found = skills.discover([self.root])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "meteo-mer")
        self.assertEqual(found[0].description, "Donne l'état de la mer.")

    def test_a_skill_without_description_is_ignored(self):
        # sans description, ava n'a aucun moyen de savoir quand s'en servir.
        write_skill(self.root, "muette", name="muette")
        self.assertEqual(skills.discover([self.root]), [])

    def test_a_folder_without_skill_file_is_ignored(self):
        (self.root / "pas-une-competence").mkdir()
        self.assertEqual(skills.discover([self.root]), [])

    def test_broken_yaml_does_not_break_discovery(self):
        folder = self.root / "cassee"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: [oups\n---\nCorps.", encoding="utf-8")
        write_skill(self.root, "saine", name="saine", description="Marche.")
        self.assertEqual([s.name for s in skills.discover([self.root])], ["saine"])

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(skills.discover([self.root / "absent"]), [])

    def test_the_user_folder_overrides_the_bundled_one(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        write_skill(self.root, "commune", name="commune", description="Version livrée.")
        write_skill(other, "commune", name="commune", description="Version perso.")
        found = skills.discover([self.root, other])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "Version perso.")


class ProgressiveDisclosureTests(unittest.TestCase):
    """La découverte ne lit que les métadonnées ; le corps vient à l'activation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_body_is_not_in_the_catalogue(self):
        write_skill(self.root, "s", "SECRET DE FABRICATION",
                    name="s", description="Une description.")
        catalogue = skills.catalogue(skills.discover([self.root]))
        self.assertIn("Une description.", catalogue)
        self.assertNotIn("SECRET DE FABRICATION", catalogue)

    def test_the_body_is_readable_on_activation(self):
        write_skill(self.root, "s", "Étape 1 : faire ceci.",
                    name="s", description="Une description.")
        skill = skills.discover([self.root])[0]
        self.assertIn("Étape 1", skill.instructions())


class SafetyTests(unittest.TestCase):
    """Une compétence exécute du code : elle reste dans son bac à sable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_script_outside_the_skill_folder_is_refused(self):
        # sans ce contrôle, `command: ../../../bin/rm` sortirait du bac à sable.
        write_skill(self.root, "evadee", name="evadee", description="Test.",
                    command="../../../bin/echo")
        skill = skills.discover([self.root])[0]
        self.assertIsNone(skill.script())

    def test_a_missing_script_is_not_run(self):
        write_skill(self.root, "vide", name="vide", description="Test.",
                    command="scripts/absent.py")
        skill = skills.discover([self.root])[0]
        self.assertIsNone(skill.script())
        self.assertEqual(skills.run_script(skill), (False, ""))

    def test_a_skill_without_command_has_no_script(self):
        write_skill(self.root, "sans", name="sans", description="Test.")
        self.assertIsNone(skills.discover([self.root])[0].script())


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make(self, code: str, command="scripts/run.py"):
        folder = write_skill(self.root, "essai", name="essai",
                             description="Test.", command=command)
        script = folder / command
        script.write_text(code, encoding="utf-8")
        return skills.discover([self.root])[0]

    def test_the_output_is_returned(self):
        skill = self.make("print('Tout va bien.')")
        self.assertEqual(skills.run_script(skill), (True, "Tout va bien."))

    def test_the_spoken_request_reaches_the_script(self):
        skill = self.make("import sys; print(sys.argv[1])")
        self.assertEqual(skills.run_script(skill, "lavalley"), (True, "lavalley"))

    def test_a_failing_script_reports_instead_of_raising(self):
        skill = self.make("import sys; sys.stderr.write('ça a cassé\\n'); sys.exit(1)")
        ok, message = skills.run_script(skill)
        self.assertFalse(ok)
        self.assertIn("cassé", message)

    def test_a_very_long_output_is_trimmed(self):
        skill = self.make("print('a' * 5000)")
        ok, output = skills.run_script(skill)
        self.assertTrue(ok)
        self.assertLessEqual(len(output), skills.MAX_SPOKEN_CHARS)


if __name__ == "__main__":
    unittest.main()
