import os
import tempfile
import unittest

import clean_env  # noqa: F401
from project_synergy import config as C
from project_synergy import errors as E


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name

    def test_defaults_follow_xdg_and_home_not_a_user_name(self) -> None:
        env = {"HOME": self.home}
        self.assertEqual(C.config_dir(env), os.path.join(self.home, ".config", "synergy"))
        self.assertEqual(C.state_dir(env, C.load({})), os.path.join(self.home, ".local", "state", "synergy"))
        env = {"HOME": self.home, "XDG_CONFIG_HOME": "/c", "XDG_STATE_HOME": "/s"}
        self.assertEqual(C.config_dir(env), "/c/synergy")
        self.assertEqual(C.state_dir(env, C.load({})), "/s/synergy")

    def test_env_overrides_win(self) -> None:
        env = {"HOME": self.home, "SYNERGY_CONFIG_DIR": "/cfg", "SYNERGY_STATE_DIR": "/st"}
        self.assertEqual(C.config_dir(env), "/cfg")
        self.assertEqual(C.state_dir(env, C.load({})), "/st")

    def test_defaults_values(self) -> None:
        cfg = C.load({})
        self.assertEqual(cfg.max_workers, 2)
        self.assertEqual(cfg.tmux_socket, "synergy")
        self.assertEqual(cfg.tmux_session, "SYNERGY")

    def test_load_toml_file(self) -> None:
        path = os.path.join(self.home, "config.toml")
        with open(path, "w") as f:
            f.write('state_dir = "/data/syn"\n[tmux]\nsocket = "claudeops"\nsession = "CLAUDE-OPS"\n'
                    "[limits]\nmax_workers = 3\n")
        cfg = C.load_file(path)
        self.assertEqual((cfg.tmux_socket, cfg.tmux_session, cfg.max_workers), ("claudeops", "CLAUDE-OPS", 3))
        self.assertEqual(C.state_dir({"HOME": self.home}, cfg), "/data/syn")

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(C.load_file(os.path.join(self.home, "none.toml")).max_workers, 2)

    def test_invalid_values_rejected(self) -> None:
        for bad in ({"limits": {"max_workers": -1}}, {"limits": {"max_workers": "two"}},
                    {"tmux": {"socket": "has space"}}, {"tmux": {"session": ""}},
                    {"limits": {"stale_after_minutes": 0}}):
            with self.assertRaises(E.ValidationError):
                C.load(bad)

    def test_bad_toml_rejected(self) -> None:
        path = os.path.join(self.home, "config.toml")
        with open(path, "w") as f:
            f.write("this is = = not toml")
        with self.assertRaises(E.ValidationError):
            C.load_file(path)
