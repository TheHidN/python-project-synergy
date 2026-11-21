import contextlib
import hashlib
import io
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import clean_env  # noqa: F401
from project_synergy import cli
from project_synergy import registry as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.fake = pathlib.Path(self._tmp.name) / "fakebin"
        self.fake.mkdir()
        self.marker = pathlib.Path(self._tmp.name) / "claude-was-run"
        claude = self.fake / "claude"
        claude.write_text(f"#!/bin/sh\ntouch {self.marker}\n")
        claude.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.fake}:{os.environ['PATH']}",
            "LANG": "C.UTF-8",
        }
        self.config = self.home / ".config" / "synergy"
        self.state = self.home / ".local" / "state" / "synergy"
        self.wrapper = self.home / ".local" / "bin" / "synergy"

    def run_script(self, script: pathlib.Path, *args: str, env: dict[str, str] | None = None,
                   stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(script), *args], env={**self.env, **(env or {})}, input=stdin,
                              capture_output=True, text=True, check=False, timeout=120)

    def install(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_script(INSTALL, "--no-tmux", *args, env=env)

    def synergy(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.wrapper), *args], env=self.env, capture_output=True, text=True,
                              check=False, timeout=60)

    def digest(self, path: pathlib.Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFreshInstall(InstallCase):
    def test_install_sets_everything_up_automatically(self) -> None:
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        self.assertTrue((self.config / "config.toml").exists())
        self.assertTrue((self.state / "registry.db").exists())
        self.assertEqual(self.synergy("status").returncode, 0)
        ops = self.synergy("operator", "list").stdout
        self.assertIn("OPS1", ops)
        self.assertIn("OPS2", ops)
        for op in ("OPS1", "OPS2"):
            token = self.state / "tokens" / f"{op}.token"
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            self.assertNotIn(token.read_text().strip(), done.stdout + done.stderr)
            self.assertTrue((self.state / "packets" / f"{op}.md").exists())
            launcher = self.state / "launchers" / f"{op}.sh"
            self.assertIn(f"SYNERGY_OPERATOR={op}", launcher.read_text())
            self.assertNotIn(token.read_text().strip(), launcher.read_text())
        self.assertNotEqual(self.synergy("rules", "show").stdout.strip(), "(not set)")
        self.assertTrue((self.config / "rules" / "synergy-rules.md").exists())
        self.assertIn("launcher", done.stdout)
        self.assertFalse(self.marker.exists(), "the installer must never start Claude")

    def test_second_run_changes_nothing(self) -> None:
        self.install()
        files = [self.config / "config.toml", self.state / "tokens" / "OPS1.token",
                 self.state / "tokens" / "OPS2.token", self.wrapper, self.state / "packets" / "OPS1.md"]
        before = [self.digest(f) for f in files]
        events = self.synergy("verify").stdout
        again = self.install()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(before, [self.digest(f) for f in files])
        self.assertEqual(events, self.synergy("verify").stdout)
        self.assertIn("already", again.stdout)

    def test_existing_config_and_claude_settings_are_never_touched(self) -> None:
        self.config.mkdir(parents=True)
        mine = self.config / "config.toml"
        mine.write_text('# my own config\n[operators]\ninitial = ["ALPHA"]\n')
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"permissions": {"defaultMode": "plan"}}\n')
        before = (self.digest(mine), self.digest(settings))
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(before, (self.digest(mine), self.digest(settings)))
        self.assertIn("ALPHA", self.synergy("operator", "list").stdout)
        self.assertIn("not changed", done.stdout)

    def test_a_foreign_synergy_command_is_not_overwritten(self) -> None:
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text("#!/bin/sh\necho somebody else's tool\n")
        self.wrapper.chmod(0o755)
        before = self.digest(self.wrapper)
        done = self.install()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("not created by this installer", done.stdout + done.stderr)
        self.assertEqual(before, self.digest(self.wrapper))
        self.assertFalse(self.state.exists())

    def test_old_python_stops_before_anything_is_created(self) -> None:
        old = self.fake / "python-old"
        old.write_text("#!/bin/sh\necho 3.8.10\n")
        old.chmod(0o755)
        done = self.install(env={"SYNERGY_PYTHON": str(old)})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("3.11", done.stdout + done.stderr)
        self.assertFalse(self.config.exists() or self.state.exists() or self.wrapper.exists())

    def test_untested_systems_are_labelled_but_not_blocked(self) -> None:
        osr = pathlib.Path(self._tmp.name) / "os-release"
        osr.write_text('ID=fedora\nVERSION_ID="40"\n')
        done = self.install(env={"SYNERGY_OS_RELEASE": str(osr)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("untested", done.stdout.lower())
        osr.write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.assertNotIn("untested", self.install(env={"SYNERGY_OS_RELEASE": str(osr)}).stdout.lower())

    def test_missing_claude_or_tmux_is_a_warning_with_instructions(self) -> None:
        (self.fake / "claude").unlink()
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("claude command", done.stdout)


class TestReadOnlySource(InstallCase):
    def test_installs_from_a_read_only_source_tree_and_leaves_editable_files(self) -> None:
        src = pathlib.Path(self._tmp.name) / "readonly-repo"
        src.mkdir()
        for name in ("install.sh", "uninstall.sh", "config.example.toml"):
            shutil.copy2(ROOT / name, src / name)
        for folder in ("src", "rules", "templates", "review-kit"):
            shutil.copytree(ROOT / folder, src / folder, ignore=shutil.ignore_patterns("__pycache__"))
        for dirpath, _dirs, files in os.walk(src, topdown=False):
            for name in files:
                os.chmod(os.path.join(dirpath, name), 0o444)
            os.chmod(dirpath, 0o555)
        self.addCleanup(lambda: [os.chmod(d, 0o755) for d, _s, _f in os.walk(src)])
        done = self.run_script(src / "install.sh", "--no-tmux")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        config = self.config / "config.toml"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertTrue(os.access(self.config / "rules" / "synergy-rules.md", os.W_OK))
        self.assertIn("rules_files", config.read_text())


class TestUninstall(InstallCase):
    def test_uninstall_moves_the_command_aside_and_keeps_all_your_data(self) -> None:
        self.install()
        token = self.state / "tokens" / "OPS1.token"
        before = (self.digest(token), self.digest(self.config / "config.toml"))
        refused = self.run_script(UNINSTALL, stdin="")
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(self.wrapper.exists())
        done = self.run_script(UNINSTALL, "--yes")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.wrapper.exists())
        self.assertEqual(before, (self.digest(token), self.digest(self.config / "config.toml")))
        self.assertTrue((self.state / "registry.db").exists())
        self.assertIn("kept", done.stdout)
        backups = list((self.home / ".local" / "share").glob("synergy-uninstalled-*/synergy"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue(self.wrapper.exists())

    def test_uninstall_refuses_a_command_it_did_not_create(self) -> None:
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text("#!/bin/sh\necho other\n")
        done = self.run_script(UNINSTALL, "--yes")
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(self.wrapper.exists())


class TestScriptsAreSafe(unittest.TestCase):
    def test_no_destructive_commands_and_no_network_in_the_scripts(self) -> None:
        for script in (INSTALL, UNINSTALL):
            text = script.read_text()
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for pat in (r"\brm\b", r"\bkill", r"\bpkill", r"\bkillall", r"tmux kill", r"send-keys", r"\bcurl\b",
                        r"\bwget\b", r"\bpip3?\b", r"\bsudo\b", r"git (reset|clean)"):
                self.assertIsNone(re.search(pat, code), f"{script.name}: {pat}")

    def test_scripts_are_executable_bash_with_strict_mode(self) -> None:
        for script in (INSTALL, UNINSTALL):
            text = script.read_text()
            self.assertTrue(text.startswith("#!/usr/bin/env bash"))
            self.assertIn("set -euo pipefail", text)
            self.assertEqual(subprocess.run(["bash", "-n", str(script)], check=False).returncode, 0)


class TestInitRulesFile(unittest.TestCase):
    def test_init_with_a_rules_file_records_its_hash_and_writes_packets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "cfg")
            os.makedirs(cfg)
            with open(os.path.join(cfg, "config.toml"), "w") as f:
                f.write('[operators]\ninitial = ["OPS1", "OPS2"]\n')
            rules = os.path.join(d, "rules.md")
            with open(rules, "w") as f:
                f.write("the rules\n")
            os.environ["SYNERGY_STATE_DIR"] = os.path.join(d, "state")
            os.environ["SYNERGY_CONFIG_DIR"] = cfg
            self.addCleanup(os.environ.pop, "SYNERGY_STATE_DIR", None)
            self.addCleanup(os.environ.pop, "SYNERGY_CONFIG_DIR", None)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["init", "--rules-file", rules]), 0)
            conn = R.open_registry(os.path.join(d, "state", "registry.db"))
            self.addCleanup(conn.close)
            self.assertEqual(R.get_setting(conn, "rules_hash"), hashlib.sha256(b"the rules\n").hexdigest())
            for op in ("OPS1", "OPS2"):
                self.assertTrue(os.path.exists(os.path.join(d, "state", "packets", f"{op}.md")))
                row = [o for o in R.list_operators(conn) if o["id"] == op][0]
                self.assertEqual(len(row["packet_hash"]), 64)
            self.assertEqual(cli.main(["init", "--rules-file", os.path.join(d, "missing.md")]), 3)


class TestQuickStartForReal(InstallCase):
    def synergy_lines(self) -> list[str]:
        lines, in_block = [], False
        for line in (ROOT / "docs" / "quick-start.md").read_text().splitlines():
            if line.strip().startswith("```"):
                in_block = not in_block
            elif in_block and re.match(r"^([A-Z_]+=\S+\s+)*synergy\s", line.strip()):
                lines.append(line.strip())
        return lines

    def sh(self, line: str) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "PATH": f"{self.wrapper.parent}:{self.env['PATH']}"}
        return subprocess.run(["bash", "-c", line], env=env, capture_output=True, text=True, check=False, timeout=60)

    def test_the_quick_start_commands_work_in_a_clean_install(self) -> None:
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = self.synergy_lines()
        self.assertGreaterEqual(len(lines), 8)
        for op in ("OPS1", "OPS2"):
            before = self.sh(f"synergy onboard --status {op}")
            self.assertEqual(before.returncode, 3)
            self.assertIn("NOT COMPLETE", before.stdout)
            packet_hash = self.digest(self.state / "packets" / f"{op}.md")
            acked = self.sh(f"SYNERGY_OPERATOR={op} synergy operator ack {op} {packet_hash}")
            self.assertEqual(acked.returncode, 0, acked.stdout + acked.stderr)
        results = [(line, self.sh(line)) for line in lines]
        by_line = dict(results)
        for line, proc in results:
            expected = 3 if line.startswith("SYNERGY_OPERATOR=OPS1 synergy task claim T1 OPS1") else 0
            self.assertEqual(proc.returncode, expected, f"{line}\n{proc.stdout}{proc.stderr}")
        refused = by_line["SYNERGY_OPERATOR=OPS1 synergy task claim T1 OPS1"]
        self.assertIn("already owned by OPS2", refused.stderr)
        for op in ("OPS1", "OPS2"):
            self.assertIn("COMPLETE", by_line[f"synergy onboard --status {op}"].stdout)
        status = by_line["synergy status"].stdout
        self.assertIn("T1", status)
        self.assertIn("OPS2", status)
        for token_file in (self.state / "tokens").glob("*.token"):
            self.assertNotIn(token_file.read_text().strip(), done.stdout + done.stderr + status)


class TestCommandOnly(InstallCase):
    def test_command_only_installs_the_command_and_leaves_an_existing_setup_untouched(self) -> None:
        self.config.mkdir(parents=True)
        cfg = self.config / "config.toml"
        cfg.write_text(f'state_dir = "{self.state}"\n')
        self.state.mkdir(parents=True)
        db = self.state / "registry.db"
        conn = R.open_registry(str(db))
        R.set_setting(conn, "rules_hash", "a" * 64)
        conn.close()
        before = (cfg.read_bytes(), hashlib.sha256(db.read_bytes()).hexdigest())
        done = self.run_script(INSTALL, "--command-only")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.wrapper.exists())
        self.assertEqual(cfg.read_bytes(), before[0])
        self.assertFalse((self.config / "rules").exists(), "no rules were copied")
        status = subprocess.run([str(self.wrapper), "status"], env=self.env, capture_output=True, text=True, check=False,
                                timeout=60, cwd="/")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("tokens:", status.stdout)
        self.assertFalse(self.marker.exists(), "the claude command was never run")

    def test_command_only_refuses_when_nothing_is_set_up_yet(self) -> None:
        done = self.run_script(INSTALL, "--command-only")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("run ./install.sh", done.stderr)
        self.assertFalse(self.wrapper.exists())
