import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ava import doctor as doctor  # noqa: E402


class GoogleCheckTests(unittest.TestCase):
    """The house special: google hands over a token WITHOUT the calendar scope
    when the api is disabled. Ava looks connected and reads nothing."""

    def _statuses(self, checks):
        return {check.name: check for check in checks}

    def test_a_token_without_the_calendar_scope_is_an_error(self):
        from ava.services.google_auth import AUTH
        with patch.object(AUTH, "status", return_value={
                "configured": True, "connected": True, "email": "moi@gmail.com"}), \
                patch.object(Path, "read_text",
                             return_value='{"scope": "openid https://www.googleapis.com/auth/userinfo.email"}'):
            checks = self._statuses(doctor._google_checks())
        self.assertEqual(checks["google:scope"].status, "error")
        self.assertIn("Calendar", checks["google:scope"].detail)

    def test_a_complete_token_passes(self):
        from ava.services.google_auth import AUTH
        with patch.object(AUTH, "status", return_value={
                "configured": True, "connected": True, "email": "moi@gmail.com"}), \
                patch.object(Path, "read_text",
                             return_value='{"scope": "https://www.googleapis.com/auth/calendar openid"}'):
            checks = self._statuses(doctor._google_checks())
        self.assertEqual(checks["google:scope"].status, "ok")

    def test_no_credentials_is_only_a_warning(self):
        from ava.services.google_auth import AUTH
        with patch.object(AUTH, "status", return_value={"configured": False, "connected": False}):
            checks = self._statuses(doctor._google_checks())
        self.assertEqual(checks["google:agenda"].status, "warning")

    def test_a_broken_connector_never_crashes_the_doctor(self):
        from ava.services.google_auth import AUTH
        with patch.object(AUTH, "status", side_effect=RuntimeError("boum")):
            checks = self._statuses(doctor._google_checks())
        self.assertEqual(checks["google:agenda"].status, "warning")


class VoiceCheckTests(unittest.TestCase):
    def test_another_engine_skips_the_local_checks(self):
        with patch.object(doctor.STORE, "snapshot", return_value={"voice": {"engine": "system"}}):
            names = [check.name for check in doctor._voice_checks()]
        self.assertEqual(names, ["voix:moteur"])

    def test_the_local_engine_checks_perth_and_the_device(self):
        with patch.object(doctor.STORE, "snapshot", return_value={"voice": {"engine": "chatterbox"}}):
            names = [check.name for check in doctor._voice_checks()]
        for expected in ("voix:chatterbox", "voix:perth", "voix:calcul", "voix:reference"):
            self.assertIn(expected, names)


class PrometheeCheckTests(unittest.TestCase):
    def test_a_missing_app_is_only_a_warning(self):
        with patch.object(doctor.Path, "exists", return_value=False):
            checks = doctor._promethee_checks()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "warning")


if __name__ == "__main__":
    unittest.main()
