import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prepublish_check.py"

PLANTED_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
PLANTED_PASSWORD = "hunter2" * 3


class PrepublishCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",  # prepublish: ignore
                    "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": self._tmp.name, "PATH": os.environ["PATH"],  # prepublish: ignore
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null", *args], env=self.env,
                       check=True, capture_output=True)

    def commit(self, message: str = "chore: files") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def check(self, *extra: str, identifiers: str = "") -> subprocess.CompletedProcess[str]:
        env = {**self.env, "PREPUBLISH_IDENTIFIERS": identifiers}
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), *extra], env=env,
                              capture_output=True, text=True, check=False, timeout=60)


class TestPrepublish(PrepublishCase):
    def test_a_clean_repository_passes(self) -> None:
        (self.repo / "README.md").write_text("Hello. Use 192.0.2.10 and user@example.com as examples.\n")
        (self.repo / "app.py").write_text("print('hi')\n")
        self.commit()
        done = self.check()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("no findings", done.stdout)

    def test_planted_secrets_and_identifiers_are_caught_and_never_printed(self) -> None:
        (self.repo / "key.txt").write_text(PLANTED_KEY + "\n")
        (self.repo / "cfg.py").write_text(f'password = "{PLANTED_PASSWORD}"\nhost = "10.1.2.3"\n')  # prepublish: ignore
        (self.repo / "notes.md").write_text("built on zebra-host by someone\ncontact: person@corp.test\n")  # prepublish: ignore
        (self.repo / "registry.db").write_bytes(b"SQLite format 3\x00")
        (self.repo / "tokens").mkdir()
        (self.repo / "tokens" / "OPS1.token").write_text("abc\n")
        self.commit()
        done = self.check(identifiers="zebra-host")
        self.assertEqual(done.returncode, 1, done.stdout)
        out = done.stdout
        for rule in ("private-key", "literal-secret", "ip-address", "identifier", "email", "forbidden-file"):
            self.assertIn(rule, out)
        for value in (PLANTED_PASSWORD, "10.1.2.3", "zebra-host", "person@corp.test"):  # prepublish: ignore
            self.assertNotIn(value, out)
        self.assertIn("key.txt:1", out)

    def test_a_secret_removed_later_is_still_found_in_history(self) -> None:
        (self.repo / "cfg.py").write_text(f'password = "{PLANTED_PASSWORD}"\n')
        self.commit("chore: add")
        (self.repo / "cfg.py").write_text("password = os.environ['PW']\n")
        self.commit("fix: remove")
        self.assertEqual(self.check().returncode, 0)
        done = self.check("--history")
        self.assertEqual(done.returncode, 1)
        self.assertIn("history", done.stdout)
        self.assertNotIn(PLANTED_PASSWORD, done.stdout)

    def test_documentation_addresses_and_example_emails_are_allowed(self) -> None:
        (self.repo / "d.md").write_text("192.0.2.1 198.51.100.7 203.0.113.9 127.0.0.1 a@example.org b@users.noreply.github.com\n")
        self.commit()
        self.assertEqual(self.check().returncode, 0)

    def test_a_marked_fixture_line_is_skipped_but_an_unmarked_one_is_not(self) -> None:
        (self.repo / "fixture.py").write_text('ip = "10.9.8.7"  # prepublish: ignore\nother = "10.9.8.6"\n')
        self.commit()
        done = self.check()
        self.assertEqual(done.returncode, 1)
        self.assertIn("fixture.py:2", done.stdout)
        self.assertNotIn("fixture.py:1", done.stdout)

    def test_the_current_user_and_host_are_checked_automatically(self) -> None:
        import getpass

        name = getpass.getuser()
        if len(name) < 4:
            self.skipTest("user name too short to check meaningfully")
        (self.repo / "a.md").write_text(f"made by {name}\n")
        self.commit()
        env = {**self.env}
        env.pop("PREPUBLISH_IDENTIFIERS", None)
        done = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo)], env=env, capture_output=True,
                              text=True, check=False, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn(name, done.stdout)
