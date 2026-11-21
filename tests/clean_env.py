"""Imported by the test modules: clears Synergy settings inherited from the shell, so the suite gives the same
result inside an operator session as anywhere else."""

import os

for _name in ("SYNERGY_OPERATOR", "SYNERGY_TOKEN_FILE", "SYNERGY_TOKEN", "SYNERGY_STATE_DIR", "SYNERGY_CONFIG_DIR",
              "SYNERGY_REVIEW_DIR", "SYNERGY_TMUX_TEST_COMMAND"):
    os.environ.pop(_name, None)
