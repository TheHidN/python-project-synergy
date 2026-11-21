import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from helpers import sandbox_works

import clean_env  # noqa: F401
from project_synergy import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent
HAVE_GIT = shutil.which("git") is not None


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class TestReviewPrepare(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        self.repo = base / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("print('a')\n")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",  # prepublish: ignore
               "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(base), "PATH": os.environ["PATH"],  # prepublish: ignore
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "chore: seed"]):
            subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null", *cmd], env=env,
                           check=True, capture_output=True)
        self.state = base / "state"
        self.cfgdir = base / "cfg"
        self.cfgdir.mkdir()
        self.reviews = base / "reviews"
        for k, v in (("SYNERGY_STATE_DIR", str(self.state)), ("SYNERGY_CONFIG_DIR", str(self.cfgdir)),
                     ("SYNERGY_REVIEW_DIR", str(self.reviews))):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def folder(self, name: str = "rev1") -> pathlib.Path:
        return self.reviews / name

    def test_review_folders_live_outside_the_denied_state_and_config_folders(self) -> None:
        os.environ.pop("SYNERGY_REVIEW_DIR")
        os.environ["XDG_DATA_HOME"] = str(self.state.parent / "data")
        self.addCleanup(os.environ.pop, "XDG_DATA_HOME", None)
        self.assertEqual(self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))[0], 0)
        folder = self.state.parent / "data" / "synergy" / "reviews" / "rev1"
        self.assertTrue((folder / "settings.json").exists())
        for denied in (self.state, self.cfgdir):
            self.assertNotIn(str(denied), str(folder))

    def test_a_review_root_inside_the_state_folder_is_refused(self) -> None:
        os.environ["SYNERGY_REVIEW_DIR"] = str(self.state / "reviews")
        code, _, err = self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))
        self.assertEqual(code, 3)
        self.assertIn("inside", err)

    def test_prepare_builds_a_ready_made_isolated_review_folder(self) -> None:
        code, out, err = self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))
        self.assertEqual(code, 0, out + err)
        d = self.folder()
        self.assertEqual((d / "code" / "src" / "a.py").read_text(), "print('a')\n")
        self.assertEqual(stat.S_IMODE((d / "code" / "src" / "a.py").stat().st_mode) & 0o222, 0)
        self.assertEqual(stat.S_IMODE((d / "code" / "src").stat().st_mode) & 0o222, 0)
        for name in ("run-isolated.sh", "run-tests.sh", "run-probe.sh"):
            self.assertTrue(os.access(d / "kit" / name, os.X_OK), name)
        self.assertTrue((d / "work").is_dir())
        self.assertTrue((d / "BRIEF.md").exists())
        launcher = d / "launch.sh"
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text()
        self.assertIn("--permission-mode dontAsk", text)
        self.assertIn("--settings", text)
        self.assertNotIn("bypass", text.lower())
        self.assertIn("launch.sh", out)
        self.assertIn("nothing was started", out)

    def test_settings_never_prompt_and_deny_the_live_registry_and_tokens(self) -> None:
        self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))
        perms = json.loads((self.folder() / "settings.json").read_text())["permissions"]
        self.assertEqual(perms["defaultMode"], "dontAsk")
        self.assertTrue(perms["denyInsteadOfPrompting"])
        allow = " | ".join(perms["allow"])
        self.assertIn("bash ./kit/run-tests.sh", allow)
        self.assertIn("bash ./kit/run-probe.sh", allow)
        for word in ("unittest", "python", "cat", "sh -c"):
            self.assertNotIn(word, allow)
        deny = " | ".join(perms["deny"])
        self.assertIn(f"/{self.state}", deny)
        self.assertIn(f"/{self.cfgdir}", deny)
        self.assertIn("kill", " | ".join(perms["ask"]))

    def test_the_brief_tells_the_reviewer_to_use_only_the_kit_commands(self) -> None:
        self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))
        brief = (self.folder() / "BRIEF.md").read_text()
        for needle in ("bash ./kit/run-tests.sh --code ./code --work ./work", "bash ./kit/run-probe.sh --code ./code --work ./work",
                       "Write tool", "REVIEW.md", "read-only"):
            self.assertIn(needle, brief)

    def test_the_prepared_kit_runs_the_code_tests_in_the_sandbox(self) -> None:
        if not sandbox_works():
            self.skipTest("bubblewrap cannot create a sandbox here")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_a.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",  # prepublish: ignore
               "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(self.state.parent), "PATH": os.environ["PATH"],  # prepublish: ignore
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        for cmd in (["add", "-A"], ["commit", "-q", "-m", "test: add"]):
            subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null", *cmd], env=env, check=True,
                           capture_output=True)
        self.assertEqual(self.run_cli("review", "prepare", "rev2", "--repo", str(self.repo))[0], 0)
        d = self.folder("rev2")
        done = subprocess.run(["bash", "./kit/run-tests.sh", "--code", "./code", "--work", "./work"], cwd=d,
                              env={"HOME": str(self.state.parent), "PATH": os.environ["PATH"]}, capture_output=True,
                              text=True, check=False, timeout=90)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_an_existing_review_folder_is_never_overwritten(self) -> None:
        self.assertEqual(self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))[0], 0)
        marker = self.folder() / "work" / "mine.txt"
        marker.write_text("keep")
        code, _, err = self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))
        self.assertEqual(code, 3)
        self.assertIn("already exists", err)
        self.assertEqual(marker.read_text(), "keep")

    def test_bad_names_and_repos_are_refused(self) -> None:
        for bad in ("", "../x", "a b", "x" * 40):
            self.assertNotEqual(self.run_cli("review", "prepare", bad, "--repo", str(self.repo))[0], 0)
        code, _, err = self.run_cli("review", "prepare", "rev9", "--repo", str(self.state))
        self.assertEqual(code, 3)
        self.assertFalse(self.folder("rev9").exists())

    def test_the_model_is_chosen_per_review_and_validated(self) -> None:
        self.assertEqual(self.run_cli("review", "prepare", "a1", "--repo", str(self.repo))[0], 0)
        self.assertIn("--model sonnet", (self.folder("a1") / "launch.sh").read_text())
        code, out, _ = self.run_cli("review", "prepare", "a2", "--repo", str(self.repo), "--model", "opus")
        self.assertEqual(code, 0)
        self.assertIn("--model opus", (self.folder("a2") / "launch.sh").read_text())
        self.assertIn("record", out.lower())
        self.assertEqual(self.run_cli("review", "prepare", "a3", "--repo", str(self.repo), "--model",
                                      "claude-opus-5-5")[0], 0)
        for bad in ("opus; echo x", "$(id)", "", "-x", "a b"):
            code, _, err = self.run_cli("review", "prepare", "a9", "--repo", str(self.repo), "--model", bad)
            self.assertIn(code, (2, 3), bad)
        self.assertFalse(self.folder("a9").exists())

    def test_nothing_is_launched(self) -> None:
        from unittest import mock

        real_run = subprocess.run
        calls: list[list[str]] = []

        def spy(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            return real_run(cmd, *a, **kw)

        with mock.patch.object(subprocess, "run", spy):
            self.assertEqual(self.run_cli("review", "prepare", "rev1", "--repo", str(self.repo))[0], 0)
        for cmd in calls:
            self.assertEqual(cmd[0], "git", cmd)
            self.assertIn("archive", cmd)


class TestReviewerTemplate(unittest.TestCase):
    def test_template_is_valid_and_has_no_code_running_allow_rules(self) -> None:
        perms = json.loads((ROOT / "templates" / "reviewer-settings.json").read_text())["permissions"]
        self.assertEqual(perms["defaultMode"], "dontAsk")
        for rule in perms["allow"]:
            for word in ("unittest", "python", "cat ", "sh -c", "bash -c", "rm"):
                self.assertNotIn(word, rule)
