"""The vendored file lock, including the failure it is supposed to have."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_action_ledger.filelock import (
    acquire_file_lock,
    file_lock,
    release_file_lock,
)


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-ledger-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "guard.lock"

    def test_the_lock_file_is_created_and_kept(self):
        with file_lock(self.path):
            self.assertTrue(self.path.exists())
        self.assertTrue(
            self.path.exists(),
            "deleting the lock file would let two processes lock two inodes",
        )

    def test_release_is_idempotent(self):
        handle = acquire_file_lock(self.path)
        self.assertTrue(handle.held)
        release_file_lock(handle)
        self.assertFalse(handle.held)
        release_file_lock(handle)
        release_file_lock(None)

    def test_the_lock_is_reusable_after_release(self):
        for _ in range(3):
            with file_lock(self.path, timeout=1):
                pass
        handle = acquire_file_lock(self.path)
        self.assertIn("guard.lock", repr(handle))
        release_file_lock(handle)

    def test_a_contended_lock_times_out(self):
        """Prove the lock can actually fail, so a success means something."""
        import subprocess
        import sys

        from .support import src_dir

        self.path.parent.mkdir(parents=True, exist_ok=True)
        code = """
import sys, time
from agent_action_ledger.filelock import acquire_file_lock
handle = acquire_file_lock(sys.argv[1], timeout=5)
print('locked', flush=True)
time.sleep(float(sys.argv[2]))
"""
        import os

        env = dict(os.environ, PYTHONPATH=src_dir())
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path), "10"],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("locked", child.stdout.readline().strip())
            with self.assertRaises(TimeoutError):
                acquire_file_lock(self.path, timeout=0.2)
        finally:
            child.kill()
            child.wait(timeout=30)
            if child.stdout is not None:
                child.stdout.close()
        # The kernel releases the lock when the holder dies.
        with file_lock(self.path, timeout=10):
            pass


if __name__ == "__main__":
    unittest.main()
