# Project Synergy user manual

This manual assumes you use Claude mostly by typing requests, and that you are new to tmux and the command line. Every command shown here is checked by the project's tests against the real program.

## Introduction

Project Synergy lets several Claude sessions work on one machine without stepping on each other.

- An **operator** is one Claude session with a name such as OPS1 or OPS2.
- The **registry** is one small database file that records the operators, the tasks and who owns them, the files and worktrees operators have claimed, and the workers that are running.
- A **claim** is atomic: if two operators try to claim the same task at the same moment, exactly one wins and the other gets a clear message.
- **tmux** keeps sessions running after you disconnect. Synergy uses its own tmux server so its windows stay apart from yours.

Why use it: you give goals, the operators split the work, and you are only asked about things that genuinely need you. What it does not do: it does not make Claude smarter, it does not stop a shell from editing a file, and it is not a security product (read the Safety guide).

## Quick start

See the [five-minute quick start](quick-start.md). It takes you from a fresh clone to two coordinating operators and shows a refused duplicate claim.

## Installation

Requirements: Linux (Ubuntu and Debian are tested; other Linux systems are untested), Python 3.11 or later with no extra packages, git, tmux, and Anthropic's Claude command-line tool. Bubblewrap is needed only for the reviewer kit.

```bash
git clone https://github.com/TheHidN/python-project-synergy.git
cd python-project-synergy
./install.sh
```

The installer is safe to run again. It checks your system and stops before changing anything if something is missing or if the name `synergy` is already used by something else. It then creates private folders, installs the `synergy` command, installs the operating rules, creates a config only if you have none, and runs `synergy init`, which creates the registry, OPS1 and OPS2, their tokens, packets and launchers. It does not start Claude, edit your Claude settings, use the network or need administrator rights. Add `--no-tmux` to skip creating the managed tmux session.

Where things live (the defaults; each can be changed with an environment variable or the config):

| What | Where |
|---|---|
| The command | `~/.local/bin/synergy` |
| Configuration | `~/.config/synergy/config.toml` |
| Registry, tokens, packets, launchers | `~/.local/state/synergy/` |
| Reviewer folders | `~/.local/share/synergy/reviews/` |

## Claude setup

Project Synergy does not install Claude or sign in for you. Install Anthropic's Claude command-line tool yourself and sign in once with your own account. Each operator is one Claude session, so each needs an account that can sign in: you may use one account for several operators, or one account per operator. Accounts do not share memory, context or usage, and Synergy never assumes they do.

The installer creates a launcher for every initial operator. A launcher sets the session's identity and starts the Claude command with a first instruction to read that operator's packet. Which permission mode your operators run in is your decision; Synergy does not change it. The default launcher adds no permission flags. To add flags, set them in the config (see the Configuration guide), knowing the Safety guide's cautions about bypass mode.

## Multi-operator setup

OPS1 and OPS2 exist after installation. To add another:

```bash
SYNERGY_OPERATOR=OPS1 synergy onboard OPS3
```

Owner-level commands (`onboard`, `operator register`, `rules set`, `operator token backfill`) must be run from an ACTIVE operator's session, so a session that is not onboarded cannot add operators; the actor is logged. On a brand-new install, before anyone is ACTIVE, they are open so you can set things up. This checks that rules are recorded, registers OPS3 as PENDING with its own token, writes its packet and launcher, and starts nothing. It prints the launcher path. A separate Claude account must sign in on its own; the tool cannot do that for it. Start the launcher yourself. The new session reads its packet and acknowledges it. Check:

```bash
synergy onboard --status OPS3
```

It says COMPLETE only after the acknowledgement exists. A PENDING operator cannot claim or be assigned anything. Adding an operator never launches a paid session by itself.

## tmux guide

tmux is a program that keeps terminal sessions alive. Synergy uses a separate tmux server (socket `synergy`, session `SYNERGY` by default) so its windows stay apart from your own.

```bash
synergy tmux ensure
synergy tmux windows
synergy tmux attach-hint
```

`attach-hint` prints the exact commands. In short: attach with `tmux -L synergy attach -t SYNERGY`; press `Ctrl-b` then `w` to choose a window; press `Ctrl-b` then `d` to detach, and everything keeps running. After an SSH disconnection, just attach again. From inside another tmux, use `TMUX= tmux -L synergy attach -t SYNERGY`.

Start a worker window. The worker is recorded in the registry first, so the worker limit and task ownership are enforced; commands that ask for permission-bypass mode are refused unless you pass `--allow-bypass`.

```bash
SYNERGY_OPERATOR=OPS1 synergy worker start W1 OPS1 --dir /tmp -- sleep 600
synergy tmux inspect W1
synergy worker list
```

Inspecting is read-only and shows a state word, never the pane text unless you add `--text`. Synergy has no command that closes, kills or types into a window; that is deliberate.

## Collaboration guide

Rules of the road, which every operator reads in its packet:

- Claim before you work. If a task is owned, do not duplicate it. One owner per task and one editing owner per shared file.
- Use an isolated git worktree for parallel code changes.
- Operators decide routine technical choices inside the task they claimed. Killing, restarting, closing or deleting, changing protected live resources, changing permission settings of existing sessions, extra paid sessions, and publishing stay with you.

Everyday commands:

```bash
SYNERGY_OPERATOR=OPS1 synergy task create T2 "Add the install test" --actor OPS1 --depends-on T1
SYNERGY_OPERATOR=OPS2 synergy task claim T2 OPS2
SYNERGY_OPERATOR=OPS2 synergy task status T2 OPS2 IN_PROGRESS
SYNERGY_OPERATOR=OPS2 synergy file claim /srv/work/app.conf OPS2 --task T2
SYNERGY_OPERATOR=OPS2 synergy task status T2 OPS2 DONE --evidence "tests pass, commit abc123"
synergy task list
synergy file list
```

Task statuses are OPEN, CLAIMED, IN_PROGRESS, BLOCKED, DONE and CANCELLED. DONE needs evidence, and DONE and CANCELLED are final. A task whose dependencies are not DONE cannot be claimed. A quiet operator never loses its tasks: `synergy stale` only reports; moving an owned task needs a recorded decision (`synergy task reassign`). A file or worktree claim records intent. It does not stop a shell from editing the file, so use worktrees.

Worker states are RUNNING, WORKING, IDLE, WAITING, BLOCKED, FINISHED and UNKNOWN. FINISHED needs evidence that the deliverables were checked and is final:

```bash
SYNERGY_OPERATOR=OPS1 synergy worker state W1 FINISHED --evidence "report read and checked"
```

### Requests: type it once

You give a plain-English request to any operator. That operator records it, and the rest of the team learns about it automatically.

```bash
SYNERGY_OPERATOR=OPS1 synergy request add "Review the install guide" --needs docs,review
SYNERGY_OPERATOR=OPS1 synergy request suggest REQ-0002
SYNERGY_OPERATOR=OPS3 synergy request take REQ-0002
SYNERGY_OPERATOR=OPS3 synergy task create RV-1 "check every command" --actor OPS3 --request REQ-0002
SYNERGY_OPERATOR=OPS3 synergy request finish REQ-0002 --evidence "guide reviewed"
SYNERGY_OPERATOR=OPS3 synergy message send OPS1 "Two commands need a newer Python" --subject REQ-0002
SYNERGY_OPERATOR=OPS1 synergy request status REQ-0002
SYNERGY_OPERATOR=OPS1 synergy request done REQ-0002 --summary "guide reviewed, two fixes made"
```

How the team hears about it, without anyone polling: every notification is saved in the registry until it is read, and each operator runs `synergy inbox wait` as a background command. It costs no Claude tokens while it waits and ends the moment something arrives, which wakes the operator session. An operator that was offline simply finds its notifications waiting when it starts (`synergy inbox list`).

`synergy request suggest` ranks the operators by the skills the request needs, who is listening, who is available, and how busy each is (set yours with `synergy operator profile`). It only suggests; the operator that was asked decides. The operator that was asked gives you the one consolidated answer (`synergy request status`). If it goes quiet, another operator can take over the final answer with `synergy request adopt`, or the operator can pass it on with `synergy request handoff`; task owners and history are kept. If an operator is busy, offline or out of Claude usage, nothing is lost: the notification waits, the request report shows who is listening, and the operator that was asked can take the work itself.

This works for any number of operators, with any names.

### The team: one approval to join, then cooperation

You approve an operator joining the team once. Only an administrator can record that approval or change it: the operators the installer created are administrators, and an administrator can make another one with `--admin`. Ordinary members can never approve, revoke, suspend, lift a hold, reset a token, change the rules hash, change scheduling weights, pause delegation or the Stop hook, turn token checks off, or widen anybody's scope, which keeps a scoped member from granting itself more. Members may record usage only for their own Claude account, and a pooled agent is never an administrator. On an upgrade, only operators that are active and approved become administrators; revoking an administrator removes the right, and reauthorizing does not give it back unless you pass `--admin`. The approval is recorded in the registry (who approved, when, the reference to your decision, the operator's account, project scopes, and whether it may assign and receive work). Join a new operator with one command run by an existing member: `synergy onboard OPS3 --approval-ref "owner-decision-2026-11-21.md" --scopes synergy,docs`. A new operator cannot become active, and receives no work, until its approval is on record. After that, members cooperate without asking you again: every assignment is checked by the registry for the sender's and receiver's approval, the project scope, the hold on that project, and each side's permission to assign or receive; anything out of scope is refused automatically.

- See the team and each member's permissions: `synergy team show`. A member reads its own authorization (also after a restart or a compaction) with `synergy team me`.
- Stop new work for one operator without touching what it is doing: `synergy team suspend OPS3 --reason "..."`, and `synergy team resume OPS3`.
- Remove an operator: `synergy team revoke OPS3 --reason "..."`. It can no longer act or receive work; its sessions are not stopped and its tasks and files are kept. Bring it back with `synergy team reauthorize OPS3 --approval-ref "..."`; it returns to pending and must acknowledge the current packet again before it works.
- Hold a whole project: `synergy team hold PROJECT --reason "..."` stops new requests and assignments in it until `synergy team release PROJECT`.
- Specialists join the pool with `synergy agent register ID --approval-ref "..."`; they never create requests or assign work.

What approval does not do: it never lets Synergy kill, close, restart, rename or delete anything, start paid workers, change protected settings, override a hold, or publish anything; those still need your specific approval. The trust model is protection against mistakes and confused sessions, not strong isolation: operator tokens are files readable by every process of the same Unix user, so a process running as that user could act as any operator. If that matters, run operators as separate Unix users. A registry that was upgraded from an older version keeps token checks off until you run `synergy operator token backfill`; `synergy doctor` warns about it, and with checks off every administrator action can be performed by any session, so turn them on. A job for a held project that is filed with no request or task is recorded under `general`, so attach jobs to their request.

### Natural-language delegation

You give any operator an ordinary instruction in its chat; you never mention Synergy. The operator runs `synergy request advise "<your instruction>"`, a local, token-free check that answers DELEGATE or LOCAL (it looks at how many independent parts the instruction has, what skills it needs, who is listening and approved, project scopes and holds, and usage). If DELEGATE, the operator registers it with `synergy request add`, tells you in one line what it handed to whom, collects the results, and gives you one consolidated answer; if LOCAL it just does the work. Operators follow this because the shared rules file tells them to and, for sessions started from a Synergy launcher, the session-start hook repeats it. Nothing is delegated that needs your decision or approval, and delegation never widens what you authorized.

### Automatic delegation

You give a request to any operator with `synergy request add "..."`. Synergy then does the routine work with local rules, using no Claude tokens:

1. If the request lists parts (lines starting with `-` or `*` are independent parts, lines starting with `1.` `2.` are ordered), it splits them into subtasks `REQ-0001-S1`, `-S2`, and so on. A simple request is not split. The operator you asked can also record a plan: `synergy request plan REQ-0001 --step "design|needs=architecture|criteria=one page" --step "build|after=1|files=src/x.py"`.
2. It offers each ready subtask to the best eligible operator: lowest weekly usage first (operators on the same Claude account count as one), then skills, who is idle, and who already holds the files involved. An operator is eligible only if its approved project scopes include the request's project (set with `synergy request add --project NAME`; the default project is `general`) and that project is not on hold. It never picks an operator that is not onboarded, is busy or out of usage, or whose account is within its reserve of the limit, and it never piles work on one operator while another is idle (the operator you asked is used for a part only when nobody else can take it, so a part may go to a busy colleague before it comes back to you).
3. Subtasks that depend on others wait and are offered when the earlier ones are done. A subtask whose files are held by two different operators is held back, never double-edited.
4. Each assignee finds the assignment in its inbox, works, and finishes with `synergy task status ID DONE --evidence "..."` (the operator is taken from your session; `task status ID OPERATOR DONE` also works; valid statuses are CLAIMED, IN_PROGRESS, BLOCKED, DONE and CANCELLED) or `synergy request finish`. If it cannot do the work it runs `synergy task fail ID --reason "..."`: the task goes back to the pool for someone else (never the same operator twice), and after three failures it waits for a person and the operator you asked is told.
5. When every part is done the operator you asked is notified. That operator, and only that operator, reviews the results (`synergy request status REQ-0001` shows every part, owner and result in one place) and closes the request with `synergy request done`. Task completion and final acceptance are separate steps.

Waiting work is offered again whenever something changes: a request arrives, a task finishes, or an operator starts `synergy inbox wait` and becomes idle. Nothing polls and no extra Claude session is started. Inspect decisions with `synergy dispatch status` (each assignment records why that operator was chosen) and `synergy status`. Pause with `synergy dispatch pause` and resume with `synergy dispatch resume` (run them as an operator, with `SYNERGY_OPERATOR` set; `dispatch status` needs no identity): pausing only stops automatic assignment; sessions, manual `request take`, `task claim` and every running task are untouched. Automatic delegation never expands what you asked for, never launches workers, never publishes anything, and never acts on a project that is on hold unless an operator for it is active and agrees.

How a session starts listening: each Claude session runs `synergy inbox wait` itself as a background command (its onboarding packet says so). Synergy does not start Claude sessions or system services on its own.

### Staying available while idle

A session is available while `synergy inbox wait` is running in the background. It costs no Claude tokens while it waits, because it is an ordinary local program that checks the registry every couple of seconds, and since 0.1.0 it has no idle timeout: it ends only when work arrives (exit 0), when the operator is no longer an approved member (exit 6), or when it is told to stop. Because it never ends for nothing, a quiet session is never woken for nothing. When it ends with work, the session reads it, does it, and starts the wait again.

What keeps operators available:
- **Durable inbox.** Work that arrives while an operator is offline stays in its inbox and is delivered the moment its listener starts again.
- **Reminder after commands.** An operator that runs a Synergy command without a listener running gets one line on stderr (at most every 10 minutes) telling it to start `inbox wait`.
- **Optional hooks for sessions started from a Synergy launcher.** `synergy inbox hook` asks a session that is about to stop without a listener to start one (at most 3 times an hour, silent when the operator set itself away with `synergy operator profile --availability BUSY`, when delegation is paused, or when `synergy hook keepalive off`), and still resumes a session for a pending assignment. `synergy inbox session-start` tells a started or resumed operator session to read `synergy team me` and start its listener. Both are bounded, never loop, never block when anything is wrong, and do nothing for sessions that are not operator sessions. Hooks are part of a session's settings, so they are applied only by the owner (see the project notes for the exact settings).
- **Watchdog.** `synergy watchdog` lists operators that are offline while work waits for them (exit 3 if any) so a person or a scheduled job can see it without using any tokens.

Limits, plainly: Synergy cannot start or wake a Claude session by itself. A session that was closed, crashed or never started a listener stays offline until it is used again; its work waits safely. Resumed conversations lose their background commands, so they must start the listener again (the session-start hook does this for launcher-started sessions). Revoking an operator stops its listener from waiting and blocks new work and access; it does not close its session.

### How a running Claude session is woken, and what is not automatic

Writing to the registry does not wake an idle Claude conversation. What actually works, tested with two real sessions:
- **A finished background command wakes the session.** `synergy inbox wait` ends the moment something arrives, which wakes the session that started it. It must be started again every time (the packet says so); if a session is busy it simply sees the work at its next turn. Notifications are durable, so nothing is lost.
- **A message from a peer session is shown to a session inside a turn it is already running.** It is not reliable for waking an idle one. `synergy dispatch nudge` lists operators with unread work who are not listening so a peer session can send them one short message.
- **An optional Stop hook** can keep a session working while it has unread assignments: the helper `synergy inbox hook` prints a block decision only then, never loops, and never blocks when the setup is broken. Installing a hook changes that session's settings, so it is not done automatically; the proposal is in the project notes and needs the owner's approval.
- **Recovery without a restart:** an automatic assignment that is not acknowledged (`synergy task start ID`) within 30 minutes, whose owner is not listening, goes back to the pool for another operator (never straight back to the quiet one) and the operator who asked is told.
- Each task shows its stage in `synergy request status`: queued, assigned, running, completed, failed (queued again) and accepted.

### The shared agent pool

Specialists (a Security Expert, a Writer, developers, testers) are registered once and then used by every operator. An operator registers an existing specialist with `synergy agent register SEC --skills security,review --window CODING:SEC --approval-ref "the record that shows you approved it"`. This only adds a row: nothing is sent to the session. The specialist takes work only after its own session reads its packet and acknowledges it. `synergy agent discover` lists running Claude windows that could be registered (read-only). It reads the whole machine's tmux, so it lists your real windows even from a scratch setup.

```bash
SYNERGY_OPERATOR=OPS1 synergy agent request "Review the installer" --needs security --priority 1
HASH=$(sha256sum ~/.local/state/synergy/packets/SEC.md | cut -d' ' -f1)
SYNERGY_OPERATOR=SEC synergy operator ack SEC "$HASH"
SYNERGY_OPERATOR=SEC synergy agent next
SYNERGY_OPERATOR=SEC synergy agent done JOB-0001 --result "2 low findings, see review.md"
SYNERGY_OPERATOR=OPS2 synergy agent result JOB-0001
synergy agent list
```

A free agent that fits is reserved in one atomic step, so two operators can never assign the same agent at once. A busy agent keeps its job and the new request waits in a priority queue (urgent first, then oldest); when the agent finishes, the best waiting job it can do is reserved for it and it is notified. Only the operator that requested a running job directs that agent; others queue requests. If an agent goes quiet, `synergy agent requeue JOB` returns a reserved or running job to the queue with its history (a finished job is refused). Agents out of Claude usage are skipped. Nothing here launches a paid worker.

## CLI reference

Every command, with its arguments, is in the [CLI reference](cli-reference.md). It is generated from the program itself, so it cannot drift from the code. Exit codes: 0 done, 2 wrong usage, 3 refused with a reason, 4 no registry yet.

## Configuration guide

The config is a TOML file at `~/.config/synergy/config.toml`; the example is `config.example.toml`. Every key is optional.

```toml
state_dir = ""

[operators]
initial = ["OPS1", "OPS2"]

[limits]
max_workers = 2
stale_after_minutes = 60

[tmux]
socket = "synergy"
session = "SYNERGY"

[safety]
protected_sessions = []

[onboarding]
rules_files = ["~/.config/synergy/rules/synergy-rules.md"]
launch_command = "claude"
launch_args = []
```

- `max_workers` is the most workers that may be active at once, across all operators. Only you raise it.
- `protected_sessions` lists tmux session names Synergy refuses to use as its own.
- `launch_args` adds flags to operator launchers. Permission-bypass flags are your decision; reviewer launchers never use them.
- Environment variables override locations: `SYNERGY_CONFIG_DIR`, `SYNERGY_STATE_DIR`, `SYNERGY_REVIEW_DIR`. A bad config is refused with a message; `synergy doctor` checks it.

## Safety guide

Read this chapter before relying on Project Synergy.

Known limits, stated plainly: `--allow-bypass` and the check that refuses bypass flags in `worker start` are guards against mistakes (they match command text) and are not a security boundary. The reviewer sandbox binds the whole system read-only, so a probe can read files outside your home folder; keep the state folder and tokens inside your home folder when you use reviews.

What is enforced by the program: atomic claims and unique ids in the registry, the worker limit, owner-only token files, refusal to act as another operator, a tamper-evident event log (`synergy verify` prints its head hash), and an isolated sandbox for reviewer commands.

What is not a guarantee:

- Operator tokens stop mistakes and confused workers, not a hostile program. All operators normally run as the same Linux user, so any program of that user can read the token files. Separate Linux users give stronger isolation; see [tokens](tokens.md).
- A registry claim does not stop a shell from editing a file.
- Bypass mode removes permission prompts and is not a security boundary. No permission mode, rule or instruction guarantees safety. Rules that match command text are not a security boundary either, because a command can be written in another form.
- The reviewer sandbox hides your home folder, blocks the network and makes the system read-only, but a reviewer's own file-reading tools run outside it; the block on the registry and tokens is a permission rule.

What the program never does for you: kill, close, restart or delete a process, tmux window, session, worktree or branch; edit your Claude settings; publish; start a paid session without you running the launcher.

Approvals: always get specific approval from the person in charge before killing, restarting, closing or deleting existing workers or work, before changing a protected live resource, before changing permission settings of existing sessions, and before publishing or deploying.

## Usage efficiency

Every `synergy` command is an ordinary local program and uses no Claude tokens. Claude reasoning, tool use and agent activity do use your allowance. No saving is claimed without measurement; use your account's usage view. To spend less: do not poll with Claude (use `synergy status`), reuse an existing worker, keep the worker limit low, and give reviewers the prepared folder so they run local tests instead of scanning everything.

Choosing the model: start with the default (Sonnet) for routine work and use a stronger model for hard or security-critical reviews, per run, never by changing a running session. For example `synergy review prepare rev2 --repo . --model opus`.

A reviewer that never asks you for permission:

```bash
synergy review prepare rev1 --repo .
```

It builds a frozen read-only copy of the code, a sandbox kit, a brief and settings that refuse anything not on a short allow list, then prints the launcher and starts nothing.

## Troubleshooting

**`synergy: command not found`.** The command is a small launcher file at `~/.local/bin/synergy`; it exists only after the installer ran. Check with `ls -l ~/.local/bin/synergy` and `echo "$PATH" | tr ':' '\n' | grep .local/bin`. If the launcher is missing, run the installer from your Synergy folder. If you already have a working setup (a configuration file and a registry) and only the command is missing, run `./install.sh --command-only`: it installs the launcher and changes nothing else (no configuration, rules, registry or tmux). If `~/.local/bin` is not on your PATH, add it to your shell startup file yourself (the installer never edits your shell files), then open a new terminal. The launcher reads your normal configuration, so it always talks to your real registry; it never creates a new one.

Run `synergy doctor` first. It lists problems in plain words.

| You see | Meaning and fix |
|---|---|
| `no registry here yet: run synergy init first` | Run `synergy init` (the installer does this). |
| `no operator identity for this session` | Run the command from the operator's launcher; it sets `SYNERGY_OPERATOR`. |
| `refusing to act as OPS2` | This session is another operator. Use the matching launcher. |
| `the token file ... must be owner-only` | Run `chmod 600` on that token file. |
| `operator OPS3 is PENDING, not ACTIVE` | OPS3 must read and acknowledge its packet first. |
| `task T1 is already owned by OPS1` | Pick another task or ask OPS1; never duplicate. |
| `operator token ... lost file` | Replace a lost token from an ACTIVE operator's session: `synergy operator token reset OPS3 --by OPS1 --decision "lost file"`. A half-finished onboarding is resumed by running `synergy onboard OPS3` again. |
| `no rules hash is recorded yet` | Run `synergy rules set --file` with your rules file, then onboard. |
| `the worker limit` message | Wait for a worker to finish, or have the person in charge raise `max_workers`. |
| `refusing to run unsandboxed` | Install bubblewrap (as an administrator), then retry. |
| `error: Python 3.11 or later is needed` | Install a newer Python and run `./install.sh` again. |
| `synergy: command not found` | Add `~/.local/bin` to your PATH. |

Every command, including `status`, `verify` and `doctor`, opens the registry and migrates it first if it is out of date (after a backup), so even a read-only command can add a migration event once after an update. `synergy status` and `synergy doctor` also say whether tokens are required; a registry migrated from an older version has them switched off until you run `synergy operator token backfill`.

If `synergy verify` reports a problem, the event log was changed outside the program: keep the folder as it is and investigate before continuing.

## Updating and uninstalling

To update, pull the new version, run the installer again, then check:

```bash
git pull
./install.sh
synergy doctor
```

A registry that needs a newer schema is migrated automatically, after a backup copy named `registry.db.bak-vN-<time>` is made beside it. To roll back, restore that file. Do not update while workers are running a task you cannot interrupt; updating never restarts a worker.

To uninstall:

```bash
./uninstall.sh --yes
```

It moves only the `synergy` command into a backup folder and keeps your configuration, registry and tokens. Delete those folders yourself only when you are sure. A running tmux session is not touched.

## Architecture

- **Registry** (`registry.db`): SQLite in write-ahead mode. Every change is one transaction opened with BEGIN IMMEDIATE, which serializes writers and makes claims atomic. Triggers refuse ordinary updates that would give a task to an unacknowledged operator or rewrite the event log.
- **Event log**: every change appends a row that includes the hash of the previous row; this detects accidental corruption and ordinary edits, but it is not tamper-proof against someone who can rewrite the whole file.
- **Operators and tokens**: each operator has a token stored as a hash in the registry and as an owner-only file; a session acts as the operator named by `SYNERGY_OPERATOR`.
- **Onboarding**: a packet (the rules with hashes, the operators and tasks, the safety rules) and a launcher per operator; completion requires an acknowledgement matching the packet's hash.
- **tmux workspace**: its own socket and session; windows are created and inspected, never closed or typed into by the program.
- **Worktrees**: you use git worktrees for parallel edits; the registry records who holds one.
- **Reviewer kit**: a bubblewrap sandbox plus a prepared folder for interruption-free reviews.

## FAQ

**Does it make Claude cheaper?** Its commands use no tokens, and it avoids duplicate work, but no saving is claimed without measurement.

**Can two operators edit the same file?** The registry will refuse the second claim, but it cannot stop a shell. Use a worktree each.

**Do I have to use bypass mode?** No. It is your choice. It never gives permission to destroy work and is not a safety measure.

**What if an operator goes quiet?** `synergy stale` reports it. Its tasks stay with it until you decide otherwise.

**Can my friends and I share one registry across machines?** No. The registry is one local file; sharing it over a network is not supported.

**Where is my token?** In an owner-only file in the state folder. The program reads it for you; you never copy it.

**Is it ready for production use?** It is tested, but the safety limits above are real. Treat it as a coordination aid, not a security boundary.
