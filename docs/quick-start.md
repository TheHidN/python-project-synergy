# Quick start: two coordinating operators in five minutes

You need a Linux machine with Python 3.11 or later, git, tmux and Anthropic's Claude command-line tool installed and signed in (see the manual's Claude setup chapter if not).

1. Get the code and install it. The installer checks your system, creates private folders, installs the `synergy` command, sets up OPS1 and OPS2 with their tokens, and creates a detached tmux session named SYNERGY on its own tmux socket (it does not touch your other tmux sessions; add `--no-tmux` to skip it). If you were given a folder instead of a link, copy it and skip the clone. It never starts Claude and never touches your Claude settings.

```bash
git clone https://github.com/TheHidN/python-project-synergy.git
cd project-synergy
./install.sh
```

2. Check that it works. If your shell says `synergy: command not found`, add `~/.local/bin` to your PATH and open a new terminal.

```bash
synergy status
synergy doctor
```

3. Start the first operator. Each launcher starts a Claude session, which uses your Claude allowance, and Claude must already be installed and signed in (Synergy never signs in for you). The installer wrote a launcher for it. Run it in a terminal (ideally inside tmux so it survives disconnects). Claude opens, reads its packet, and acknowledges it by itself.

```bash
bash ~/.local/state/synergy/launchers/OPS1.sh
```

4. Start the second operator the same way, in another terminal.

```bash
bash ~/.local/state/synergy/launchers/OPS2.sh
```

5. Confirm both are onboarded. If an operator still says NOT COMPLETE, its session has not acknowledged yet (not signed in, or stopped at a permission prompt); you can acknowledge by hand from that operator's session (the packet path assumes the default state folder; use the folder in `SYNERGY_STATE_DIR` if you set one): `HASH=$(sha256sum ~/.local/state/synergy/packets/OPS1.md | cut -d' ' -f1)` and then `synergy operator ack OPS1 "$HASH"` with `SYNERGY_OPERATOR=OPS1` set. Each line must say COMPLETE; until an operator has acknowledged its packet it says NOT COMPLETE and can claim nothing.

```bash
synergy onboard --status OPS1
synergy onboard --status OPS2
```

6. Watch them avoid duplicate work. OPS1 creates a task, OPS2 claims it, and a second claim is refused with a clear message.

```bash
SYNERGY_OPERATOR=OPS1 synergy task create T1 "Write the project notes" --actor OPS1
SYNERGY_OPERATOR=OPS2 synergy task claim T1 OPS2
SYNERGY_OPERATOR=OPS1 synergy task claim T1 OPS1
synergy status
```

7. Give a request once and let the team pick it up. Any operator can record it, and every other operator is notified automatically and finds it waiting in its inbox, even if it was away.

```bash
SYNERGY_OPERATOR=OPS1 synergy request add "Check that the installer works on a clean machine"
SYNERGY_OPERATOR=OPS2 synergy inbox list
SYNERGY_OPERATOR=OPS2 synergy request take REQ-0001
SYNERGY_OPERATOR=OPS1 synergy request status REQ-0001
```

(Add `--project NAME` to `request add` to scope a request to a project; the default is `general`. If `request take` prints already yours, automatic delegation had already given the request to OPS2, which is fine.) That is the whole idea: one registry, one owner per task, one request typed once. To add a third operator, run `synergy onboard OPS3` from an operator's session (for example `SYNERGY_OPERATOR=OPS1 synergy onboard OPS3`) and start its launcher. The manual explains tmux workspaces, workers, safety and troubleshooting.
