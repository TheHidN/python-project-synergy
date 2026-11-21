# Project Synergy 🤝

> 🧭 Multi-Claude collaboration and orchestration: several Claude sessions share one task registry, claim work without duplicating it, and are managed from one tmux workspace.

## ✨ What it does

- 🗂 **Shared task registry.** One SQLite file records operators, tasks, who owns each task, file and worktree claims, and workers. Claims are atomic, so two operators can never both own the same task.
- 🔑 **Automatic operator tokens.** Every operator gets its own secret token when it is created. You never copy or paste one.
- 🚪 **Onboarding in one command.** `synergy onboard OPS3` registers a new operator, writes its reading packet and launcher, and reports complete only after the operator acknowledges.
- 🖥 **Managed tmux workspace.** A separate tmux socket holds worker windows, with a limit on how many run at once.
- 📬 **Type a request once.** `synergy request add` records plain-English work, notifies the whole team through durable inboxes, and gives you one consolidated answer from the operator you asked. Any number of operators, no polling.
- 🧑‍🤝‍🧑 **One shared specialist pool.** Every operator can request work from the same registered agents; reservations are atomic, busy agents queue work by priority, and results are kept for everyone.
- 🔍 **Read-only monitoring.** Status, worker states and window inspection use ordinary local commands, not Claude.
- 🧪 **Interruption-free reviewers.** `synergy review prepare` builds an isolated review folder and a sandbox so a reviewer can read, test and report without asking you for permission.

## 🚀 Quick start

```bash
git clone https://github.com/TheHidN/python-project-synergy.git
cd python-project-synergy
./install.sh
```

Then follow the [five-minute quick start](docs/quick-start.md).

## 📚 Documentation

- [Quick start](docs/quick-start.md): from clone to two coordinating operators in about five minutes.
- [User manual](docs/user-manual.md): installation, setup, collaboration, safety, troubleshooting, updating and more.
- [CLI reference](docs/cli-reference.md): every command, generated from the program itself.
- [Operator tokens](docs/tokens.md) and [reviewers](docs/reviewers.md): how identity and isolated reviews work.

## 🛡 Safety in one minute

Registry ownership stops operators from duplicating work. It does not stop a shell from editing a file, and operator tokens stop mistakes, not a hostile program running as your own Linux user. Bypass mode is your choice and is never a guarantee. The tool never kills, closes, restarts or deletes anything for you. Read the [safety guide](docs/user-manual.md#safety-guide) before you rely on it.

## 🧰 Requirements

- Linux (Ubuntu and Debian are the tested systems; others are untested).
- Python 3.11 or later, with no extra packages.
- git and tmux. Bubblewrap is needed only for the isolated reviewer kit.
- Anthropic's Claude command-line tool, installed and signed in by you.

## 🧪 Develop and test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

## ⚖️ License

MIT. See [LICENSE](LICENSE).
