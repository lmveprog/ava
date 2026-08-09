import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import promethee  # noqa: E402


def _make_db(path: Path, rows) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "create table sessions (id text primary key, user_id text, task text, "
        "started_at integer, ended_at integer, deleted integer not null default 0)"
    )
    con.executemany("insert into sessions values (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class ActiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "promethee.db"
        self.addCleanup(self.tmp.cleanup)

    def test_no_database_means_no_session(self):
        with patch.object(promethee, "DB_PATH", self.db):
            self.assertIsNone(promethee.active_session())

    def test_finished_sessions_are_ignored(self):
        _make_db(self.db, [("a", "u", "Fini", 1000, 2000, 0)])
        with patch.object(promethee, "DB_PATH", self.db):
            self.assertIsNone(promethee.active_session())

    def test_deleted_sessions_are_ignored(self):
        _make_db(self.db, [("a", "u", "Supprime", 1000, None, 1)])
        with patch.object(promethee, "DB_PATH", self.db):
            self.assertIsNone(promethee.active_session())

    def test_running_session_is_reported_with_its_task(self):
        _make_db(self.db, [
            ("a", "u", "Vieux", 1000, 2000, 0),
            ("b", "u", "Deep work", 3000, None, 0),
        ])
        with patch.object(promethee, "DB_PATH", self.db):
            running = promethee.active_session()
        self.assertEqual(running["task"], "Deep work")


class StartSessionTests(unittest.TestCase):
    def test_a_running_session_is_left_alone(self):
        # ava ne doit jamais relancer par-dessus une session en cours : ca
        # couperait le compteur de focus en deux.
        with patch.object(promethee, "active_session",
                          return_value={"id": "b", "task": "Deep work", "started_at": 0}), \
                patch.object(promethee, "_launch_and_wait") as launch:
            reply = promethee.start_session()
        launch.assert_not_called()
        self.assertTrue(reply.ok)
        self.assertTrue(reply.already)
        self.assertIn("Deep work", reply.text)

    def test_a_dead_app_is_reported_without_crashing(self):
        with patch.object(promethee, "active_session", return_value=None), \
                patch.object(promethee, "_launch_and_wait", return_value=None):
            reply = promethee.start_session()
        self.assertFalse(reply.ok)
        self.assertIn("Prométhée", reply.text)

    def test_sentence_stays_empty_when_the_launch_fails(self):
        with patch.object(promethee, "start_session",
                          return_value=promethee.SessionReply(False, "raté")):
            self.assertEqual(promethee.session_sentence(), "")


if __name__ == "__main__":
    unittest.main()
