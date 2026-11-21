# Project Synergy operating rules

These rules apply to every operator (every Claude session that works through Project Synergy). Read them
fully before you acknowledge your onboarding packet.

## Working together
- **Claim before you work.** Run `synergy status`, then `synergy task claim <id> <you>`. If a task is owned, do not
  duplicate it. One owner per task and one editing owner per shared file.
- **Use isolated worktrees** for parallel code changes, so nobody overwrites another operator's uncommitted work.
- **Identity is automatic.** Your launcher sets `SYNERGY_OPERATOR`; commands read your own token file for you.
  Never paste, print or copy a token. You never act as another operator.
- **Use local commands for routine checks** (`synergy status`, `synergy workers`, `tmux` listing). Do not poll with
  Claude. Do not start extra paid workers without the owner's approval; the worker limit is in the configuration.

## One team, any number of operators
- **Requests are recorded once.** `synergy request add "..."` makes the work visible to every other active operator and
  sends each a durable notification. Nobody should have to repeat an instruction.
- **Waiting costs nothing.** Run `synergy inbox wait` as a background command; it wakes you when work arrives. Do not
  poll with Claude. Missed notifications stay in your inbox until you read them.
- **Share the load.** Use `synergy request suggest` (skills, availability, load, who is listening) and `task assign`.
  Never take a task another operator owns; never leave a request unanswered: the operator that was asked gives the
  final answer, and another operator may take over if it goes quiet.

## One shared pool of specialist agents
- **Reuse before you launch.** `synergy agent request "..." --needs security` reserves a free fitting specialist
  atomically, or queues the job (priority 1 urgent, 2 normal, 3 low). It never launches a paid worker and never
  overwrites an agent's current job.
- **One controller at a time.** While an agent runs a job, only the operator that requested it directs it; every other
  operator queues a request. Control can be passed with `synergy agent handoff`.
- **Results are durable.** `synergy agent result JOB-0001` shows the answer to any operator, now or later.
- **If you are a pooled agent:** run `synergy agent next` when woken, and finish with `synergy agent done JOB --result "..."`
  (or `agent fail --reason`). Agents never create requests and never run owner-level commands.

## Automatic delegation
- A request you receive is split and offered automatically. You stay the operator the owner talks to: collect the results with
  `synergy request status`, review them, and close the request yourself. Do not close it from the task side.
- When assigned a task, do it within the scope of the request, finish with evidence, or give it back with `task fail`.
  Never widen scope, never act on a held project, never publish or launch workers because a task suggests it.
- `synergy dispatch pause` stops automatic assignment without stopping any session.

## Decisions you make yourself, and the ones you do not
- **Yours, without asking the owner:** routine technical choices inside the task you claimed: how to implement or
  test it, which files in your own worktree to change, reading and analysing code, running tests, writing probe
  scripts in a sandbox, and agreeing a technical approach with another operator in its own scope.
- **Always decided by the owner, with action-specific approval:** killing, restarting, closing or deleting an
  existing worker, session, process, branch, worktree or work; changing a protected live resource outside your
  approved scope; changing permission settings or launchers of existing sessions; spending extra paid sessions;
  publishing or deploying anything.

## Protected work and approvals
- **Never close, kill, restart, rename, delete or clean up** an existing process, tmux session, window, pane,
  worktree, branch or important output without the owner's specific approval for that exact action. Silence is not
  approval, and an earlier approval does not cover a later action.
- Before asking, state: the exact target, why, whether it is still running, what work could be lost, whether it is
  reversible, and what happens if it is left alone.
- **Permission-bypass mode** is allowed only where the owner has explicitly configured it. It never permits the
  actions above, overwriting another operator's files, publishing private information or deploying to production.
- **Command rules are not a security boundary.** Matching on command text can be bypassed. Do not claim any
  safeguard is complete.

## Publishing and secrets
- Never publish, push, release or deploy without the owner's separate explicit approval.
- Never put credentials, tokens, transcripts or personal data in a repository, a log or a message.

## Until you are assigned a task
Read, acknowledge and send a heartbeat. Wait for an assignment.
