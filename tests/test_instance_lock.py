from pathlib import Path
import tempfile
import unittest

from instance_lock import SingleInstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_prevents_a_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ava.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
