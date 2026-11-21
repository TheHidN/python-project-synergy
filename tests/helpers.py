import os
import sqlite3
import tempfile
import unittest

from project_synergy import registry as R

RULES_HASH = "a" * 64


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.db = os.path.join(self.dir, "registry.db")
        self.conn = R.open_registry(self.db)
        self.addCleanup(self.conn.close)
        R.set_setting(self.conn, "rules_hash", RULES_HASH)
        R.set_setting(self.conn, "require_tokens", "0")

    def active(self, op_id: str, role: str = "") -> None:
        R.register_operator(self.conn, op_id, role=role)
        R.ack_operator(self.conn, op_id, RULES_HASH)

    def raw(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(c.close)
        return c


def sandbox_works() -> bool:
    """True when bubblewrap can create a sandbox here (it cannot inside another sandbox)."""
    import shutil
    import subprocess

    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        return subprocess.run([bwrap, "--ro-bind", "/", "/", "--unshare-all", "true"], capture_output=True,
                              timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
