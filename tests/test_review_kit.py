import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest

from helpers import sandbox_works

ROOT = pathlib.Path(__file__).resolve().parent.parent
KIT = ROOT / "review-kit"


@unittest.skipUnless(sandbox_works(), "bubblewrap is not usable on this machine")
class TestIsolatedRunner(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        top = pathlib.Path(self._tmp.name)
        base = top / "review"
        base.mkdir()
        self.kit = base / "kit"
        shutil.copytree(KIT, self.kit)
        self.home = top / "home"
        self.home = base / "home"
        (self.home / ".config" / "synergy" / "tokens").mkdir(parents=True)
        (self.home / ".config" / "synergy" / "tokens" / "OPS1.token").write_text("live-secret-token\n")
        self.code = base / "code"
        (self.code / "src").mkdir(parents=True)
        (self.code / "src" / "hello.py").write_text("print('hello from code')\n")
        self.work = base / "work"
        self.work.mkdir()
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "SECRET_X": "should-not-leak",
                    "SYNERGY_OPERATOR": "OPS1"}

    def run_in(self, *cmd: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.kit / "run-isolated.sh"), "--code", str(self.code), "--work",
                               str(self.work), "--", *cmd], env=env or self.env, capture_output=True, text=True,
                              check=False, timeout=60)

    def test_runs_a_command_and_returns_its_output_and_exit_code(self) -> None:
        done = self.run_in("sh", "-c", "echo hi; exit 3")
        self.assertEqual((done.returncode, done.stdout.strip()), (3, "hi"))

    def test_live_home_and_tokens_are_invisible(self) -> None:
        done = self.run_in("sh", "-c", f"ls -a {self.home}; cat {self.home}/.config/synergy/tokens/OPS1.token")
        self.assertNotIn("live-secret-token", done.stdout + done.stderr)
        self.assertNotIn(".config", done.stdout)
        self.assertNotEqual(done.returncode, 0)

    def test_nothing_can_be_written_outside_the_work_folder(self) -> None:
        for target in (f"{self.home}/x", "/etc/synergy-test", f"{self.code}/src/new.py", "/run/x"):
            done = self.run_in("sh", "-c", f"touch {target}")
            self.assertNotEqual(done.returncode, 0, target)
        self.assertFalse((self.home / "x").exists())
        ok = self.run_in("sh", "-c", f"touch {self.work}/made-here")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue((self.work / "made-here").exists())

    def test_the_code_is_readable_but_read_only(self) -> None:
        done = self.run_in("python3", "-I", f"{self.code}/src/hello.py")
        self.assertEqual(done.stdout.strip(), "hello from code")
        self.assertNotEqual(self.run_in("sh", "-c", f"echo x >> {self.code}/src/hello.py").returncode, 0)
        self.assertEqual((self.code / "src" / "hello.py").read_text(), "print('hello from code')\n")

    def test_the_environment_is_scrubbed(self) -> None:
        done = self.run_in("sh", "-c", "env")
        self.assertNotIn("should-not-leak", done.stdout)
        self.assertNotIn("SYNERGY_OPERATOR", done.stdout)

    def test_there_is_no_network(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        threading.Thread(target=lambda: server.accept(), daemon=True).start()
        code = f"import socket;s=socket.socket();s.settimeout(3);s.connect(('127.0.0.1',{port}))"
        done = self.run_in("python3", "-I", "-c", code)
        self.assertNotEqual(done.returncode, 0)

    def test_session_sockets_under_run_are_hidden(self) -> None:
        done = self.run_in("sh", "-c", "ls -A /run | wc -l")
        self.assertEqual(done.stdout.strip(), "0")

    def test_it_refuses_to_run_without_a_sandbox(self) -> None:
        bare = pathlib.Path(self._tmp.name) / "emptybin"
        bare.mkdir()
        for name in ("bash", "dirname", "cd", "pwd", "env"):
            path = shutil.which(name)
            if path:
                (bare / name).symlink_to(path)
        done = subprocess.run(["/usr/bin/env", "bash", str(self.kit / "run-isolated.sh"), "--code", str(self.code),
                               "--work", str(self.work), "--", "true"],
                              env={"HOME": str(self.home), "PATH": str(bare)}, capture_output=True, text=True,
                              check=False, timeout=30)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("unsandboxed", done.stdout + done.stderr)

    def test_missing_arguments_are_refused(self) -> None:
        done = subprocess.run(["bash", str(self.kit / "run-isolated.sh"), "--code", str(self.code), "--", "true"],
                              env=self.env, capture_output=True, text=True, check=False, timeout=30)
        self.assertNotEqual(done.returncode, 0)


@unittest.skipUnless(sandbox_works(), "bubblewrap is not usable on this machine")
class TestKitWrappers(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        top = pathlib.Path(self._tmp.name)
        base = top / "review"
        base.mkdir()
        self.kit = base / "kit"
        shutil.copytree(KIT, self.kit)
        self.home = top / "home"
        self.code = base / "code"
        (self.code / "src" / "pkg").mkdir(parents=True)
        (self.code / "src" / "pkg" / "__init__.py").write_text("VALUE = 7\n")
        (self.code / "tests").mkdir()
        (self.code / "tests" / "test_a.py").write_text(
            "import unittest\nfrom pkg import VALUE\nclass T(unittest.TestCase):\n"
            "    def test_v(self):\n        self.assertEqual(VALUE, 7)\n")
        self.work = base / "work"
        self.work.mkdir()
        self.env = {"HOME": str(base / "home"), "PATH": os.environ["PATH"]}
        (base / "home").mkdir()

    def test_run_tests_runs_the_code_tests_inside_the_sandbox(self) -> None:
        done = subprocess.run(["bash", str(self.kit / "run-tests.sh"), "--code", str(self.code), "--work", str(self.work)],
                              env=self.env, capture_output=True, text=True, check=False, timeout=90)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("OK", done.stderr + done.stdout)

    def test_run_probe_runs_a_script_with_the_code_importable_and_only_the_work_folder_writable(self) -> None:
        probe = self.work / "probe.py"
        probe.write_text("import sys, os\nsys.path.insert(0, os.environ['CODE_SRC'])\nfrom pkg import VALUE\n"
                         "print('value', VALUE, 'work', sys.argv[1] == os.getcwd())\n"
                         "open(os.path.join(sys.argv[1], 'out.txt'), 'w').write('x')\n")
        done = subprocess.run(["bash", str(self.kit / "run-probe.sh"), "--code", str(self.code), "--work", str(self.work),
                               "probe.py"], env=self.env, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("value 7 work True", done.stdout)
        self.assertTrue((self.work / "out.txt").exists())

    def test_run_probe_refuses_a_script_outside_the_work_folder(self) -> None:
        done = subprocess.run(["bash", str(self.kit / "run-probe.sh"), "--code", str(self.code), "--work", str(self.work),
                               "/etc/passwd"], env=self.env, capture_output=True, text=True, check=False, timeout=30)
        self.assertNotEqual(done.returncode, 0)
        done = subprocess.run(["bash", str(self.kit / "run-probe.sh"), "--code", str(self.code), "--work", str(self.work),
                               "../x.py"], env=self.env, capture_output=True, text=True, check=False, timeout=30)
        self.assertNotEqual(done.returncode, 0)


class TestKitFilesAreSafe(unittest.TestCase):
    def test_no_destructive_commands_or_network_in_the_kit(self) -> None:
        import re
        for script in KIT.glob("*.sh"):
            code = "\n".join(ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#"))
            for pat in (r"\brm\b", r"\bkill", r"\bcurl\b", r"\bwget\b", r"\bsudo\b", r"tmux", r"send-keys",
                        r"git (reset|clean)", r"--dangerously"):
                self.assertIsNone(re.search(pat, code), f"{script.name}: {pat}")
            self.assertIn("set -euo pipefail", code)
            self.assertEqual(subprocess.run(["bash", "-n", str(script)], check=False).returncode, 0)
