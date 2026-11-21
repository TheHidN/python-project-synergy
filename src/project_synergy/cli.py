"""The `synergy` command. Every command is a local program: it uses no Claude model tokens."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import cast

from . import __version__
from . import agents as AG
from . import config as C
from . import errors as E
from . import onboard as O
from . import registry as R
from . import review as V
from . import tmux as T
from .tokens import token_file as _token_file
from .tokens import write_token as _write_token

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_NOT_INIT = 0, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="synergy", description="Project Synergy: coordinate several AI coding operators with shared task ownership."
    )
    p.add_argument("--version", action="version", version=f"synergy {__version__}")
    sub = p.add_subparsers(dest="group")

    ini = sub.add_parser("init", help="create the registry and the initial operators' tokens (safe to run again)")
    ini.add_argument("--rules-file", help="record this file's sha256 as the rules hash and write operator packets")
    ini.add_argument("--no-require-tokens", action="store_true",
                     help="development only: do not require tokens for identity-sensitive actions")
    ob = sub.add_parser("onboard", help="onboard a new operator (registers it, writes its packet and launcher; starts nothing)")
    ob.add_argument("id")
    ob.add_argument("--status", action="store_true", help="report whether the operator has acknowledged its packet")
    ob.add_argument("--role", default="")
    ob.add_argument("--approval-ref", default="", help="the record or message showing an administrator approved this operator joining")
    ob.add_argument("--scopes", default="*", help="projects it may work on, comma separated, or *")
    ob.add_argument("--admin", action="store_true", help="make it an administrator too")
    rv = sub.add_parser("review", help="prepare an isolated, never-prompting review folder (starts nothing)").add_subparsers(dest="action")
    rp = rv.add_parser("prepare", help="frozen code copy, sandbox kit, brief, settings and launcher")
    rp.add_argument("name")
    rp.add_argument("--repo", default=".")
    rp.add_argument("--ref", default="HEAD")
    rp.add_argument("--model", default="sonnet", help="sonnet (default), opus, haiku or a full model id; use opus for hard or security-critical reviews")
    rq = sub.add_parser("request", help="plain-English requests that every operator can see and take").add_subparsers(dest="action")
    ra_ = rq.add_parser("add", help="record a request in plain English; the other operators are notified automatically")
    ra_.add_argument("text")
    ra_.add_argument("--auto", action="store_true", help="(default) let Synergy split and hand out the work automatically")
    ra_.add_argument("--no-auto", action="store_true", help="record it only; do not split or assign anything")
    ra_.add_argument("--project", default="general", help="the project this request belongs to")
    ra_.add_argument("--needs", default="", help="comma-separated skills the work needs, used to suggest who should take it")
    rq.add_parser("list", help="list requests")
    rs_ = rq.add_parser("status", help="one consolidated progress report for a request")
    rs_.add_argument("id", nargs="?", default="")
    rp_ = rq.add_parser("plan", help="record a plan for a request you asked: each --step is 'title|needs=..|files=..|after=1,2|criteria=..'")
    rp_.add_argument("id")
    rp_.add_argument("--step", action="append", required=True)
    rv_ = rq.add_parser("advise", help="advice for an ordinary instruction: DELEGATE or LOCAL, and who would get it (changes nothing)")
    rv_.add_argument("text")
    rv_.add_argument("--project", default="general")
    rr_ = rq.add_parser("route", help="hand an unowned request to the best eligible operator (explains why)")
    rr_.add_argument("id")
    rr_.add_argument("--model", default="any")
    rt_ = rq.add_parser("take", help="take a request (atomic: one operator wins)")
    rt_.add_argument("id")
    rf_ = rq.add_parser("finish", help="report that you finished a request you took (the operator that asked closes it)")
    rf_.add_argument("id")
    rf_.add_argument("--evidence", required=True, help="the result, in a sentence")
    rd_ = rq.add_parser("done", help="close a request you asked, with a short summary of the result")
    rd_.add_argument("id")
    rd_.add_argument("--summary", required=True)
    rg_ = rq.add_parser("suggest", help="rank the operators that could take a request (local, no tokens)")
    rg_.add_argument("id")
    rh_ = rq.add_parser("handoff", help="pass the final response for a request you asked to another operator")
    rh_.add_argument("id")
    rh_.add_argument("--to", required=True)
    rq.add_parser("adopt", help="take over the final response when the operator that was asked has gone quiet").add_argument("id")
    ib = sub.add_parser("inbox", help="your durable notifications: new requests, assignments, results, messages").add_subparsers(dest="action")
    il = ib.add_parser("list", help="show unread notifications (no marking)")
    il.add_argument("--all", action="store_true")
    ir = ib.add_parser("read", help="mark notifications read: ids, or all")
    ir.add_argument("ids", nargs="+")
    ib.add_parser("session-start", help="for a session-start hook: tells an operator session how to come back online (prints JSON or nothing)")
    ib.add_parser("hook", help="for a Stop hook: keep a session working while assignments are unread (prints JSON or nothing)")
    iw = ib.add_parser("wait", help="wait until something new arrives, print it and exit (run in the background; costs no tokens while waiting)")
    iw.add_argument("--timeout", type=int, default=0, help="seconds before giving up; 0 (default) waits until work arrives, with no idle wake-ups")
    ms = sub.add_parser("message", help="send a durable message to one operator or to all").add_subparsers(dest="action")
    m1 = ms.add_parser("send", help="message one operator")
    m1.add_argument("to")
    m1.add_argument("text")
    m1.add_argument("--subject", default="")
    ms.add_parser("all", help="message every other active operator").add_argument("text")
    hk = sub.add_parser("hook", help="switch the optional Stop hook helper on or off (it never stops or deletes any session)").add_subparsers(dest="action")
    hk.add_parser("status", help="is the Stop hook helper on or off, and the limits it works within")
    hk.add_parser("on", help="allow the Stop hook helper to resume a session for pending assignments")
    hk.add_parser("off", help="make the Stop hook helper do nothing")
    hka = hk.add_parser("keepalive", help="switch the reminder to start a listener before a session stops on or off (administrator)")
    hka.add_argument("state", choices=("on", "off"))
    wd = sub.add_parser("watchdog", help="read-only: operators that are offline while work waits for them (exit 3 if any)")
    wd.add_argument("--minutes", type=int, default=10)
    tm = sub.add_parser("team", help="approved team membership, scopes and holds").add_subparsers(dest="action")
    tm.add_parser("show", help="who is on the team, with scopes, permissions and the owner approval on record")
    tm.add_parser("me", help="your own authorization, in plain words (read it again after a restart)")
    tap = tm.add_parser("approve", help="record the owner's approval for an operator to join")
    tap.add_argument("id")
    tap.add_argument("--approval-ref", required=True)
    tap.add_argument("--scopes", default="*")
    tap.add_argument("--no-assign", action="store_true")
    tap.add_argument("--admin", action="store_true", help="make it an administrator (may approve, revoke, hold and reset tokens)")
    tsu = tm.add_parser("suspend", help="stop new work for an operator (nothing running is touched)")
    tsu.add_argument("id")
    tsu.add_argument("--reason", required=True)
    tm.add_parser("resume", help="allow new work again").add_argument("id")
    trv = tm.add_parser("revoke", help="remove an operator from the team (no session is killed, no work deleted)")
    trv.add_argument("id")
    trv.add_argument("--reason", required=True)
    tra = tm.add_parser("reauthorize", help="restore a revoked operator with a new owner reference")
    tra.add_argument("id")
    tra.add_argument("--approval-ref", required=True)
    tra.add_argument("--scopes", default=None)
    tho = tm.add_parser("hold", help="the owner's hold on a project: no new requests or assignments in it")
    tho.add_argument("project")
    tho.add_argument("--reason", required=True)
    trl = tm.add_parser("release", help="lift a project hold")
    trl.add_argument("project")
    trl.add_argument("--reason", required=True)
    dp = sub.add_parser("dispatch", help="automatic delegation of requests and tasks to eligible operators").add_subparsers(dest="action")
    dp.add_parser("status", help="is automatic delegation on or paused, and the latest decisions")
    dp.add_parser("pause", help="stop automatic assignment (running sessions and manual work are not affected)")
    dp.add_parser("resume", help="turn automatic assignment back on")
    dp.add_parser("run", help="offer waiting work to eligible operators now")
    dn = dp.add_parser("nudge", help="operators with unread work that are not listening (a peer session can message them)")
    dn.add_argument("--min-age", type=int, default=120, help="only unread items older than this many seconds")
    us = sub.add_parser("usage", help="weekly Claude usage per account, used to balance new work").add_subparsers(dest="action")
    uset = us.add_parser("set", help="record an account's weekly utilisation from a figure you read (never estimated)")
    uset.add_argument("account")
    uset.add_argument("--percent", required=True, type=float)
    uset.add_argument("--resets", default=None, help="when the week resets, ISO date and time")
    uset.add_argument("--reserve", type=float, default=None, help="percent of the week to keep free")
    uin = us.add_parser("ingest", help="read the latest figure from a local usage history file")
    uin.add_argument("account")
    uin.add_argument("--history", required=True)
    uin.add_argument("--reset-epoch", required=True, type=int)
    us.add_parser("show", help="the figures, how fresh they are, and which operators share each account")
    sc = sub.add_parser("schedule", help="the weights used to balance work").add_subparsers(dest="action")
    sc.add_parser("show", help="the current weights")
    scs = sc.add_parser("set", help="change one weight")
    scs.add_argument("name")
    scs.add_argument("value")
    ag = sub.add_parser("agent", help="the shared pool of AI specialist agents every operator can use").add_subparsers(dest="action")
    ar = ag.add_parser("register", help="add an existing specialist to the pool (an operator does this; nothing is sent to it)")
    ar.add_argument("id")
    ar.add_argument("--role", default="")
    ar.add_argument("--skills", default="")
    ar.add_argument("--model", default="any")
    ar.add_argument("--window", default="", help="its tmux window, recorded for reference only")
    ar.add_argument("--approval-ref", default="", help="the record showing an administrator approved this specialist joining the pool")
    ag.add_parser("list", help="the board: every agent, what it is doing, and the queue")
    ad = ag.add_parser("discover", help="list running Claude windows that could be registered (read-only)")
    ad.add_argument("--register", action="store_true", help="register what was found (names are taken from the windows)")
    aq = ag.add_parser("request", help="ask the pool for a specialist; reuses an existing agent, otherwise queues")
    aq.add_argument("text")
    aq.add_argument("--needs", default="")
    aq.add_argument("--agent", default="")
    aq.add_argument("--priority", type=int, default=2, choices=(1, 2, 3), help="1 urgent, 2 normal, 3 low")
    aq.add_argument("--model", default="any")
    aq.add_argument("--request", default="", help="the request this job belongs to")
    ag.add_parser("next", help="(agent) start the job reserved for you, or pick up the best waiting one")
    adn = ag.add_parser("done", help="(agent) finish a job with its result")
    adn.add_argument("id")
    adn.add_argument("--result", required=True)
    afl = ag.add_parser("fail", help="(agent) give a job back as failed, with the reason")
    afl.add_argument("id")
    afl.add_argument("--reason", required=True)
    ag.add_parser("result", help="show a job and its result").add_argument("id")
    ag.add_parser("cancel", help="withdraw a job that has not started").add_argument("id")
    ahf = ag.add_parser("handoff", help="pass control of a job to another operator")
    ahf.add_argument("id")
    ahf.add_argument("--to", required=True)
    ag.add_parser("requeue", help="put a job back in the queue when its agent has gone quiet").add_argument("id")
    sub.add_parser("status", help="read-only report: operators, tasks, claims, workers")
    sub.add_parser("doctor", help="check dependencies, configuration and the registry")
    sub.add_parser("verify", help="check the event log's hash chain and print its head hash")
    rl_ = sub.add_parser("rules", help="the rules packet hash operators acknowledge").add_subparsers(dest="action")
    rs = rl_.add_parser("set", help="record the hash of the rules packet (acknowledgement is refused until set)")
    rg = rs.add_mutually_exclusive_group(required=True)
    rg.add_argument("--hash")
    rg.add_argument("--file", help="a file whose sha256 becomes the rules hash")
    rs.add_argument("--by", default="owner")
    rl_.add_parser("show", help="print the current rules hash")
    st = sub.add_parser("stale", help="list operators not seen recently (report only, never reassigns)")
    st.add_argument("--minutes", type=int, default=None)

    op = sub.add_parser("operator", help="register, acknowledge and list operators").add_subparsers(dest="action")
    r = op.add_parser("register", help="register an operator as PENDING")
    r.add_argument("id")
    r.add_argument("--role", default="")
    r.add_argument("--environment", default="")
    r.add_argument("--account", default="")
    a = op.add_parser("ack", help="acknowledge the rules packet; makes the operator ACTIVE")
    a.add_argument("id")
    a.add_argument("hash")
    h = op.add_parser("heartbeat", help="record that the operator is alive")
    h.add_argument("id")
    op.add_parser("list", help="list operators")
    ss = op.add_parser("session", help="declare the name your Claude session shows in the session list")
    ss.add_argument("--name", required=True)
    pf = op.add_parser("profile", help="set your skills, model preference, capacity and availability")
    pf.add_argument("--skills", default=None)
    pf.add_argument("--model", default=None)
    pf.add_argument("--max-tasks", type=int, default=None)
    pf.add_argument("--availability", default=None, choices=("AVAILABLE", "BUSY", "OUT_OF_USAGE"))
    tkn = op.add_parser("token", help="rotate an operator's token, or issue missing ones").add_subparsers(dest="sub")
    rot = tkn.add_parser("rotate", help="replace this session's own token (automatic; no copying)")
    rot.add_argument("id")
    tkn.add_parser("backfill", help="issue tokens to operators that have none, then require tokens")
    rst = tkn.add_parser("reset", help="replace a lost token; needs an ACTIVE operator and a recorded decision")
    rst.add_argument("id")
    rst.add_argument("--by", required=True)
    rst.add_argument("--decision", required=True, help="the record of who decided it")

    tk = sub.add_parser("task", help="create, claim, assign and track tasks").add_subparsers(dest="action")
    c = tk.add_parser("create", help="create a task")
    c.add_argument("id")
    c.add_argument("description")
    c.add_argument("--actor", required=True)
    c.add_argument("--dir", default="")
    c.add_argument("--branch", default="")
    c.add_argument("--deliverables", default="")
    c.add_argument("--depends-on", nargs="*", default=[])
    c.add_argument("--request", default="", help="the request this task belongs to")
    tk.add_parser("start", help="acknowledge an assignment and start work (you are the owner)").add_argument("id")
    tfl = tk.add_parser("fail", help="give back a task you could not do; it returns to the pool for someone else")
    tfl.add_argument("id")
    tfl.add_argument("--reason", required=True)
    cl = tk.add_parser("claim", help="atomically claim a task for an operator")
    cl.add_argument("id")
    cl.add_argument("operator")
    asg = tk.add_parser("assign", help="give an unowned task to one ACTIVE operator")
    asg.add_argument("id")
    asg.add_argument("operator")
    asg.add_argument("--by", required=True)
    rl = tk.add_parser("release", help="the owner gives the task back")
    rl.add_argument("id")
    rl.add_argument("operator")
    s = tk.add_parser("status", help="the owner changes the task status: `id STATUS` (you are the operator) or `id OPERATOR STATUS`")
    s.add_argument("id")
    s.add_argument("rest", nargs="+", metavar="[OPERATOR] STATUS")
    s.add_argument("--evidence", default="")
    ra = tk.add_parser("reassign", help="move an owned task; needs a decision reference")
    ra.add_argument("id")
    ra.add_argument("new_owner")
    ra.add_argument("--decision", required=True)
    ra.add_argument("--by", required=True)
    tk.add_parser("list", help="list tasks")

    fl = sub.add_parser("file", help="exclusive ownership of files and worktrees").add_subparsers(dest="action")
    fc = fl.add_parser("claim", help="claim a file or worktree")
    fc.add_argument("path")
    fc.add_argument("operator")
    fc.add_argument("--task", default="")
    fc.add_argument("--kind", choices=("file", "worktree"), default="file")
    fr = fl.add_parser("release", help="release a claim")
    fr.add_argument("path")
    fr.add_argument("operator")
    fl.add_parser("list", help="list claims")

    wk = sub.add_parser("worker", help="the worker inventory").add_subparsers(dest="action")
    wr = wk.add_parser("register", help="record a worker (counts against the worker limit)")
    wr.add_argument("id")
    wr.add_argument("operator")
    wr.add_argument("--task", default="")
    wr.add_argument("--window", default="")
    wr.add_argument("--dir", default="")
    ws = wk.add_parser("state", help="set a worker's state")
    ws.add_argument("id")
    ws.add_argument("state")
    ws.add_argument("--by", default="")
    ws.add_argument("--evidence", default="", help="required for FINISHED: what shows the deliverables were checked")
    wk.add_parser("list", help="list workers")
    wst = wk.add_parser("start", help="record a worker and open its window in the managed tmux session")
    wst.add_argument("id")
    wst.add_argument("operator")
    wst.add_argument("--task", default="")
    wst.add_argument("--dir", default=os.getcwd())
    wst.add_argument("--allow-bypass", action="store_true")

    tm = sub.add_parser("tmux", help="the managed tmux workspace (never closes or types into anything)").add_subparsers(dest="action")
    tm.add_parser("ensure", help="create the managed session if missing")
    tm.add_parser("windows", help="list the managed windows")
    ti = tm.add_parser("inspect", help="state of one managed window (read-only)")
    ti.add_argument("window")
    ti.add_argument("--lines", type=int, default=12)
    ti.add_argument("--text", action="store_true", help="also print the last lines of the pane")
    tm.add_parser("attach-hint", help="how to reconnect over SSH")
    return p


def _table(rows: Sequence[sqlite3.Row], cols: Sequence[str]) -> str:
    if not rows:
        return "(none)"
    out = ["  ".join(f"{c:<12}" for c in cols)]
    out += ["  ".join(f"{str(r[c] if r[c] is not None else '-')[:40]:<12}" for c in cols) for r in rows]
    return "\n".join(out)


def _token_for(conn: sqlite3.Connection, db: str, actor: str) -> str:
    """The token of the operator this session is, read from its own owner-only file. Nothing is pasted.

    The session's identity comes from SYNERGY_OPERATOR (its launcher sets it). A session never acts as a
    different operator. This stops mistakes and confused workers, not a hostile program of the same user.
    """
    actor = actor.strip().upper()
    required = R.get_setting(conn, "require_tokens") == "1"
    session = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
    if session and session != actor:
        raise E.AuthFailed(f"this session is {session}; refusing to act as {actor}")
    if not session:
        if required:
            raise E.AuthFailed("no operator identity for this session: its launcher sets SYNERGY_OPERATOR")
        return ""
    path = _token_file(db, session)
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        if required:
            raise E.AuthFailed(f"no token file for {session}") from None
        return ""
    if mode & 0o077:
        raise E.AuthFailed(f"the token file for {session} must be owner-only (chmod 600)")
    with open(path) as handle:
        return handle.read().strip()


def _me(conn: sqlite3.Connection, db: str) -> tuple[str, str]:
    """The operator this session is, and its token (read automatically)."""
    op = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
    if not op:
        raise E.AuthFailed("no operator identity for this session: its launcher sets SYNERGY_OPERATOR")
    return op, _token_for(conn, db, op)


def _tick(conn: sqlite3.Connection, op: str, tok: str) -> list[dict[str, object]]:
    """Offer waiting work after something changed. Best effort: a refusal here never breaks the command that called it."""
    if not op:
        return []
    try:
        return R.dispatch_tick(conn, op, token=tok)
    except E.SynergyError:
        return []


def _print_nudges(conn: sqlite3.Connection, assignees: Iterable[str]) -> None:
    wanted = set(assignees)
    for n in R.pending_nudges(conn, min_age_seconds=0):
        if n["operator"] in wanted:
            hint = f"session {n['session']}" if n["session"] else "session name unknown; find it in the session list"
            print(f"NUDGE {n['operator']} ({hint}): not listening, {n['unread']} unread. Send it one short message "
                  "(\"Synergy: you have work in your inbox; run synergy inbox list\") through your session list; "
                  "a peer message is the supported way to prompt a quiet session.")


def _show(rows: list[sqlite3.Row]) -> None:
    for n in rows:
        print(f"[{n['id']}] {n['kind']} {n['subject']}: {n['body']}")


def _inbox_wait(conn: sqlite3.Connection, op: str, token: str, timeout: int) -> int:
    """Wait for work without using any model tokens. Ends when work arrives (0), when the timeout passes (5), when the
    operator is no longer a member in good standing (6), or when it is told to stop (0). The default has no timeout, so a
    quiet session is never woken for nothing."""

    problem = R.wait_problem(conn, op)
    if problem:
        raise E.NotActive(f"{op} cannot wait for work: {problem}")
    stop = {"now": False}

    def _stop(_signum: int, _frame: object) -> None:
        stop["now"] = True

    previous = (signal.signal(signal.SIGTERM, _stop), signal.signal(signal.SIGINT, _stop))
    poll = max(0.05, float(os.environ.get("SYNERGY_WAIT_POLL", "2.0")))
    deadline = time.time() + timeout if timeout > 0 else None
    last_listen = 0.0
    _tick(conn, op, token)
    try:
        while not stop["now"]:
            now = time.time()
            problem = R.wait_problem(conn, op)
            if problem:
                print(f"{op} is no longer a member in good standing ({problem}); stopped waiting")
                return 6
            if now - last_listen >= 30:
                R.set_listening(conn, op, token)
                last_listen = now
            if R.has_unread(conn, op):
                time.sleep(min(1.5, poll * 4))
                rows = R.list_notifications(conn, op)
                _show(rows)
                R.mark_read(conn, op, [r["id"] for r in rows], token)
                return EXIT_OK
            if deadline is not None and now >= deadline:
                print("nothing new")
                return 5
            time.sleep(poll)
        print("stopped by a signal; start the wait again to stay available")
        return 7
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])
        try:
            R.clear_listening(conn, op)
        except sqlite3.Error:
            pass


def _owner_actor(conn: sqlite3.Connection, db: str) -> str:
    """Who is running an owner-level command (register, onboard, rules, backfill).

    Until the registry has had its first active operator nobody can be authenticated, so these are open. After that an
    authenticated ADMINISTRATOR session is needed, and its name is logged."""
    if not R.is_bootstrapped(conn):
        return "owner"
    session = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
    if not session:
        raise E.AuthFailed("no operator identity for this session: owner-level commands need an administrator session "
                           "(its launcher sets SYNERGY_OPERATOR)")
    actor = R.authenticate_operator(conn, session, _token_for(conn, db, session))
    R.require_admin(conn, actor)
    return actor


class _OnboardLock:
    """One onboarding at a time per registry, so a second process can never mistake an onboarding in progress for
    a half-finished one and resume it."""

    def __init__(self, db: str) -> None:
        self.path = os.path.join(os.path.dirname(db), ".onboard.lock")

    def __enter__(self) -> None:
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _session_command() -> str:
    """A command for `tmux ensure` that tests may set; ignored unless tests explicitly allow it."""
    if os.environ.get("SYNERGY_ALLOW_TEST_HOOKS") == "1":
        return os.environ.get("SYNERGY_TMUX_TEST_COMMAND", "")
    return ""


def _paths() -> tuple[C.Config, str]:
    env = os.environ
    cfg = C.load_file(os.path.join(C.config_dir(env), "config.toml"))
    return cfg, os.path.join(C.state_dir(env, cfg), "registry.db")


def _doctor(cfg: C.Config, db: str) -> int:
    results: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        results.append((level, name, detail))

    ok_py = sys.version_info >= (3, 11)
    add("PASS" if ok_py else "FAIL", "python", f"{sys.version.split()[0]} (needs 3.11 or later)")
    add("PASS", "sqlite", sqlite3.sqlite_version)
    for tool, level in (("tmux", "WARN"), ("git", "WARN"), ("claude", "WARN")):
        path = shutil.which(tool)
        add("PASS" if path else level, tool, "found" if path else "not found (needed for some features)")
    state = os.path.dirname(db)
    add("PASS" if os.path.isdir(state) and os.access(state, os.W_OK) else "WARN", "state folder",
        "writable" if os.path.isdir(state) else "not created yet: run `synergy init`")
    if os.path.isdir(state):
        mode = os.stat(state).st_mode & 0o777
        add("PASS" if mode & 0o077 == 0 else "WARN", "folder access",
            "private (owner only)" if mode & 0o077 == 0 else f"mode {mode:o}: others can read it; run chmod 700 on it")
    if os.path.exists(db):
        conn = R.open_registry(db)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            add("PASS" if integrity == "ok" else "FAIL", "registry", f"integrity {integrity}")
            ok, msg = R.verify_chain(conn)
            add("PASS" if ok else "FAIL", "event log", msg)
            required = R.get_setting(conn, "require_tokens") == "1"
            add("PASS" if required else "WARN", "tokens",
                "required for identity-sensitive actions" if required
                else "NOT required: run `synergy operator token backfill` to require them")
        finally:
            conn.close()
    else:
        add("WARN", "registry", "not created yet: run `synergy init`")
    for level, name, detail in results:
        print(f"{level:<5} {name:<13} {detail}")
    return EXIT_REFUSED if any(r[0] == "FAIL" for r in results) else EXIT_OK


def _run(args: argparse.Namespace, cfg: C.Config, db: str) -> int:
    if args.group == "init":
        fresh = not os.path.exists(db)
        conn = R.open_registry(db)
        created = []
        try:
            if fresh and cfg.max_workers != 2:
                R.set_setting(conn, "max_workers", str(cfg.max_workers))
            if fresh and args.no_require_tokens:
                R.set_setting(conn, "require_tokens", "0")
            if args.rules_file:
                if not os.path.isfile(args.rules_file):
                    raise E.ValidationError(f"rules file {args.rules_file} does not exist")
                with open(args.rules_file, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                current = R.get_setting(conn, "rules_hash")
                if not current:
                    R.set_rules_hash(conn, digest, by="installer")
                elif current != digest:
                    print("note: a different rules hash is already recorded; it was left as it is")
            if not R.get_setting(conn, "require_approval"):
                R.set_setting(conn, "require_approval", "1")
            known = {o["id"] for o in R.list_operators(conn)}
            for name in cfg.initial_operators:
                if name not in known:
                    _write_token(db, name, R.register_operator(conn, name))
                    R.approve_operator(conn, name, by="owner", approval_ref="approved by the owner when installing Synergy")
                    R.set_setting(conn, "require_approval", "1")
                    created.append(name)
                    if R.get_setting(conn, "rules_hash"):
                        _packet, _launcher, digest = O.write_files(conn, cfg, db, name)
                        R.set_packet_hash(conn, name, digest)
        finally:
            conn.close()
        print(f"registry ready: {db}")
        if created:
            print("operators created (PENDING until each acknowledges the rules): " + ", ".join(created))
            print("each has an owner-only token file in the tokens folder; the session that acts as an operator "
                  "has SYNERGY_OPERATOR set to its name by its launcher")
        return EXIT_OK
    if args.group == "doctor":
        return _doctor(cfg, db)
    if args.group == "tmux":
        return _tmux(args, cfg)
    if args.group == "review":
        if args.action != "prepare":
            return EXIT_USAGE
        state, conf = os.path.dirname(db), C.config_dir(os.environ)
        folder = V.prepare(cfg, V.review_root(os.environ, state, conf), state, conf, args.name, args.repo, args.ref, args.model)
        print(f"review folder ready: {folder}")
        print(f"launcher: {folder}/launch.sh  (nothing was started; run it yourself to start the reviewer)")
        print("The reviewer can only read the frozen code, run the kit's two commands in a sandbox and write its report. "
              "Anything else is refused, not queued, so it never waits on you.")
        print("It cannot read the registry or tokens. This is built from the documentation; try it once on a disposable "
              "review before relying on it.")
        if args.model != "sonnet":
            print(f"Model {args.model} costs more than the default: record this use in your model record.")
        return EXIT_OK
    if not os.path.exists(db):
        print("no registry here yet: run `synergy init` first", file=sys.stderr)
        return EXIT_NOT_INIT
    conn = R.open_registry(db)
    try:
        return _dispatch(args, cfg, conn, db)
    finally:
        conn.close()


def _tmux(args: argparse.Namespace, cfg: C.Config) -> int:
    if args.action == "ensure":
        made = T.ensure_session(cfg, command=_session_command())
        print(f"session {cfg.tmux_session} on socket {cfg.tmux_socket}: {'created' if made else 'already there'}")
    elif args.action == "windows":
        rows = T.list_windows(cfg)
        for w in rows:
            print(f"{w['name']:<14} {w['command']:<10} idle {w['idle_seconds']}s  {w['path']}")
        if not rows:
            print("(no managed windows, or the session does not exist yet)")
    elif args.action == "inspect":
        info = T.inspect_window(cfg, args.window, args.lines, args.text)
        print(f"{info['name']}: {info['state']} ({info['command']}, idle {info['idle_seconds']}s)")
        if "text" in info:
            print(info["text"])
    elif args.action == "attach-hint":
        print(f"reconnect:  tmux -L {cfg.tmux_socket} attach -t {cfg.tmux_session}")
        print("detach:     press Ctrl-b, then d (everything keeps running)")
        print(f"list:       tmux -L {cfg.tmux_socket} list-windows -t {cfg.tmux_session}")
    else:
        return EXIT_USAGE
    return EXIT_OK


def _dispatch(args: argparse.Namespace, cfg: C.Config, conn: sqlite3.Connection, db: str) -> int:
    g, act = args.group, getattr(args, "action", None)
    if g == "status":
        print(R.status_report(conn))
    elif g == "verify":
        ok, msg = R.verify_chain(conn)
        print(msg)
        return EXIT_OK if ok else EXIT_REFUSED
    elif g == "stale":
        minutes = args.minutes or cfg.stale_after_minutes
        rows = R.stale_operators(conn, minutes)
        print(_table(rows, ("id", "status", "last_seen")))
        print(f"report only: no task is reassigned because an operator is quiet ({minutes} min)")
    elif g == "request":
        op, tok = ("", "") if act in ("list", "status") else _me(conn, db)
        if act == "add":
            rid = R.create_request(conn, args.text, op, token=tok, needs=args.needs, project=args.project)
            print(f"{rid} recorded. The other operators were notified; they will see it in their inbox. "
                  f"Follow it with: synergy request status {rid}")
            if not args.no_auto:
                steps = R.split_text(args.text)
                if steps:
                    ids = R.plan_request(conn, rid, op, steps, token=tok)
                    print(f"split into {len(ids)} parts: {', '.join(ids)}")
                made = _tick(conn, op, tok)
                for m in made:
                    print(m["explanation"])
                _print_nudges(conn, [str(m["assignee"]) for m in made])
                if not made:
                    print("no eligible operator right now; any operator can take it, and it is offered again when one is free")
        elif act == "plan":
            steps = []
            for raw in args.step:
                parts = [x.strip() for x in raw.split("|")]
                step: dict[str, object] = {"title": parts[0]}
                for extra in parts[1:]:
                    key, _, value = extra.partition("=")
                    if key == "after":
                        step["depends"] = [int(v) for v in value.split(",") if v.strip()]
                    elif key in ("needs", "files", "criteria"):
                        step[key] = value
                    else:
                        raise E.ValidationError(f"unknown step field {key!r}; use needs, files, after or criteria")
                steps.append(step)
            ids = R.plan_request(conn, args.id, op, steps, token=tok)
            print("plan recorded: " + ", ".join(ids))
            for m in _tick(conn, op, tok):
                print(m["explanation"])
        elif act == "advise":
            advice = R.delegate_advice(conn, op, args.text, project=args.project)
            print(f"{advice['decision']}: {advice['reason']}")
            print(f"parts {advice['parts']}; needs {advice['needs'] or '-'}; suggested model {advice['model']}")
            for c in cast(list[dict[str, object]], advice["candidates"]):
                print(f"  {c['id']:<10} score {c['score']}  {c['reason']}")
        elif act == "route":
            chosen = R.route_request(conn, args.id, op, model=args.model, token=tok)
            print(chosen["explanation"] if chosen else "no eligible operator right now; any operator can take it")
        elif act == "list":
            print(_table(R.list_requests(conn), ("id", "status", "origin", "text")))
        elif act == "status":
            ids = [args.id] if args.id else [r["id"] for r in R.list_requests(conn)]
            print("\n\n".join(R.request_report(conn, i) for i in ids) if ids else "(no requests yet)")
        elif act == "take":
            now = R.take_request(conn, args.id, op, token=tok)
            print(f"{args.id.upper()} taken by {op}" if now else f"{args.id.upper()} is already yours")
        elif act == "finish":
            R.set_task_status(conn, args.id, op, "DONE", evidence=args.evidence, token=tok)
            _tick(conn, op, tok)
            print(f"{args.id.upper()} reported finished; the operator that asked was notified and closes it")
        elif act == "done":
            R.close_request(conn, args.id, op, args.summary, token=tok)
            print(f"{args.id.upper()} closed")
        elif act == "suggest":
            ranking = R.suggest_assignees(conn, args.id)
            for r in ranking:
                print(f"{r['id']:<10} score {r['score']:>3}  {r['reason']}")
            if not ranking:
                print("no available operator fits; the operator that asked can take it itself")
        elif act == "handoff":
            R.handoff_request(conn, args.id, op, args.to, token=tok)
            print(f"{args.id.upper()} handed to {args.to.upper()}")
        elif act == "adopt":
            R.adopt_request(conn, args.id, op, stale_minutes=cfg.stale_after_minutes, token=tok)
            print(f"{args.id.upper()} adopted by {op}")
        else:
            return EXIT_USAGE
    elif g == "watchdog":
        wd_rows = R.availability_report(conn, minutes=args.minutes)
        for r in wd_rows:
            print(f"OFFLINE WITH WORK {r['operator']} ({str(r['kind']).lower()}): {r['unread']} unread since {r['oldest']}"
                  f"{'; session ' + str(r['session']) if r['session'] else ''}")
        if not wd_rows:
            print("nobody is offline with work waiting")
        return EXIT_REFUSED if wd_rows else EXIT_OK
    elif g == "hook":
        if act == "status":
            print(f"stop hook helper: {'ON' if R.hook_enabled(conn) else 'OFF'}; at most {R.HOOK_MAX_PER_ASSIGNMENT} reminders per assignment "
                  f"and {R.HOOK_MAX_PER_HOUR} per operator per hour; also silent when delegation is paused, when "
                  "SYNERGY_HOOK_DISABLED=1, or when the file hook.disabled exists in the config folder")
        elif act == "keepalive":
            me, tok = _me(conn, db)
            R.set_hook_keepalive(conn, args.state == "on", me, token=tok)
            print(f"keepalive reminder {args.state.upper()}")
        elif act in ("on", "off"):
            me, tok = _me(conn, db)
            R.set_hook(conn, act == "on", me, token=tok)
            print(f"stop hook helper {act.upper()}; no session was touched")
        else:
            return EXIT_USAGE
    elif g == "team":
        if act == "show":
            print(R.team_report(conn))
        else:
            me, tok = _me(conn, db)
            if act == "me":
                print(R.team_me(conn, me))
            elif act == "approve":
                R.approve_operator(conn, args.id, me, args.approval_ref, scopes=args.scopes, can_assign=not args.no_assign, token=tok, admin=args.admin)
                print(f"{args.id.upper()} approved (scopes {args.scopes}); the approval is on record")
            elif act == "suspend":
                R.suspend_work(conn, args.id, me, args.reason, token=tok)
                print(f"{args.id.upper()}: no new work from now on; nothing running was touched")
            elif act == "resume":
                R.resume_work(conn, args.id, me, token=tok)
                print(f"{args.id.upper()} can receive work again")
            elif act == "revoke":
                R.revoke_operator(conn, args.id, me, args.reason, token=tok)
                print(f"{args.id.upper()} removed from the team: no new work or access; no session was stopped and nothing was deleted")
            elif act == "reauthorize":
                R.reauthorize_operator(conn, args.id, me, args.approval_ref, scopes=args.scopes, token=tok)
                print(f"{args.id.upper()} reauthorized")
            elif act in ("hold", "release"):
                R.set_hold(conn, args.project, act == "hold", me, args.reason, token=tok)
                print(f"project {args.project} {'on hold' if act == 'hold' else 'released'}")
            else:
                return EXIT_USAGE
    elif g == "dispatch":
        if act == "status":
            print("automatic delegation: " + ("ON" if R.dispatch_enabled(conn) else "PAUSED (running sessions are not affected)"))
            for e in conn.execute("SELECT ts, subject, detail FROM events WHERE action='schedule.route' ORDER BY seq DESC LIMIT 5"):
                print(f"  {e['ts']}  {e['detail'][:150]}")
        elif act in ("pause", "resume"):
            op, tok = _me(conn, db)
            R.set_dispatch(conn, act == "resume", op, token=tok)
            print("automatic delegation " + ("resumed" if act == "resume" else "PAUSED; nothing else was stopped"))
        elif act == "nudge":
            nudge_rows = R.pending_nudges(conn, min_age_seconds=args.min_age)
            for n in nudge_rows:
                print(f"NUDGE {n['operator']} (session {n['session'] or 'unknown'}): {n['unread']} unread since {n['oldest']}")
            if not nudge_rows:
                print("nobody needs a nudge")
        elif act == "run":
            op, tok = _me(conn, db)
            made = R.dispatch_tick(conn, op, token=tok)
            for m in made:
                print(m["explanation"])
            _print_nudges(conn, [str(m["assignee"]) for m in made])
            print(f"{len(made)} assignment(s) made" if R.dispatch_enabled(conn) else "automatic delegation is paused")
        else:
            return EXIT_USAGE
    elif g == "inbox":
        op, tok = _me(conn, db)
        if act == "list":
            rows = R.list_notifications(conn, op, unread_only=not args.all)
            _show(rows)
            if not rows:
                print("(inbox empty)")
        elif act == "read":
            read_ids = [int(n["id"]) for n in R.list_notifications(conn, op)] if args.ids == ["all"] else [int(i) for i in args.ids]
            R.mark_read(conn, op, read_ids, token=tok)
            print(f"{len(read_ids)} marked read")
        elif act == "wait":
            return _inbox_wait(conn, op, tok, args.timeout)
        else:
            return EXIT_USAGE
    elif g == "message":
        op, tok = _me(conn, db)
        if act == "send":
            R.send_message(conn, op, args.to, args.text, subject=args.subject, token=tok)
            print(f"sent to {args.to.upper()}")
        elif act == "all":
            R.send_message(conn, op, None, args.text, token=tok)
            print("sent to every other active operator")
        else:
            return EXIT_USAGE
    elif g == "usage":
        op, tok = ("", "") if act == "show" else _me(conn, db)
        if act == "set":
            R.set_usage(conn, args.account, args.percent, resets_at=args.resets, reserve=args.reserve, by=op, token=tok)
            print(f"{args.account}: {args.percent}% recorded (manual, verified now)")
        elif act == "ingest":
            got = R.ingest_usage(conn, args.history, args.account, args.reset_epoch, by=op, token=tok)
            print(f"{args.account}: {got}% read from the usage history")
        elif act == "show":
            for name, pool in sorted(R.get_usage(conn).items()):
                figure = "unknown" if pool["percent"] is None else f"{pool['percent']}% used, {pool['left']}% left"
                print(f"{name:<14} {figure}; {pool['state']}; source {pool['source'] or '-'}; verified "
                      f"{pool['verified_at'] or '-'}; resets {pool['resets_at'] or '-'}; reserve {pool['reserve']}%; "
                      f"operators {','.join(cast(list[str], pool['operators']))}")
        else:
            return EXIT_USAGE
    elif g == "schedule":
        if act == "show":
            for name, value in R.get_tunables(conn).items():
                print(f"{name:<14} {value:g}")
        elif act == "set":
            op, tok = _me(conn, db)
            R.set_tunable(conn, args.name, args.value, by=op, token=tok)
            print(f"{args.name} set to {args.value}")
        else:
            return EXIT_USAGE
    elif g == "agent":
        if act == "register":
            with _OnboardLock(db):
                actor = _owner_actor(conn, db)
                problems = O.check_prerequisites(conn, cfg)
                if problems:
                    raise E.ValidationError("cannot register: " + "; ".join(problems))
                agent = args.id.strip().upper()
                if actor != "owner" and R.get_setting(conn, "require_approval") == "1" and not args.approval_ref.strip():
                    raise E.ValidationError("a specialist joining the pool needs the owner's approval on record: pass --approval-ref")
                token = R.register_operator(conn, agent, args.role, kind="AGENT", skills=args.skills,
                                            model_pref=args.model, window=args.window)
                _write_token(db, agent, token)
                R.approve_operator(conn, agent, by=actor, approval_ref=args.approval_ref or "approved by the owner when installing Synergy",
                                   scopes="*", can_assign=False, token=_token_for(conn, db, actor) if actor != "owner" else "")
                packet, launcher, digest = O.write_files(conn, cfg, db, agent)
                R.set_packet_hash(conn, agent, digest)
            print(f"{agent} registered in the pool (not attached). Nothing was sent to it and no session was touched.")
            print(f"It takes work only after its own session reads its packet and acknowledges it: {packet}")
            print(f"registered by {actor}; board: synergy agent list")
        elif act == "list":
            print(R.pool_report(conn))
        elif act == "discover":
            known = {o["window"] for o in R.list_operators(conn) if o["window"]}
            found = AG.parse_windows(AG.tmux_lines(), protected=(), known_windows=known)
            for f in found:
                print(f"discovered {f['id']:<20} {f['window']}")
            if not found:
                print("(no unregistered Claude windows found)")
            if args.register and found:
                print("To register one: synergy agent register <ID> --window <window> --skills ...")
        elif act == "request":
            op, tok = _me(conn, db)
            placed = R.request_agent(conn, op, args.text, needs=args.needs, agent_id=args.agent, priority=args.priority,
                                     model=args.model, request_id=args.request, token=tok)
            if placed["status"] == "RESERVED":
                print(f"{placed['id']} reserved for {placed['agent_id']}; it has been notified and you direct it until it finishes")
            else:
                print(f"{placed['id']} queued (position {placed['position']}); the next fitting agent picks it up. "
                      f"You will be notified: synergy inbox wait")
        elif act == "next":
            me, tok = _me(conn, db)
            job = R.agent_next(conn, me, token=tok)
            if job is None:
                print("nothing for you")
                return 5
            print(f"{job['id']} from {job['requester']} (priority {job['priority']}): {job['text']}")
        elif act == "done":
            me, tok = _me(conn, db)
            R.finish_job(conn, args.id, me, args.result, token=tok)
            print(f"{args.id.upper()} done; the requester was notified")
        elif act == "fail":
            me, tok = _me(conn, db)
            R.fail_job(conn, args.id, me, args.reason, token=tok)
            print(f"{args.id.upper()} marked failed; the requester was notified")
        elif act == "result":
            j = R.get_job(conn, args.id)
            print(f"{j['id']} {j['status']} requested by {j['requester']} agent {j['agent_id'] or '-'}\n"
                  f"asked: {j['text']}\nresult: {j['result'] or '(none yet)'}")
        elif act == "cancel":
            me, tok = _me(conn, db)
            R.cancel_job(conn, args.id, me, token=tok)
            print(f"{args.id.upper()} cancelled")
        elif act == "handoff":
            me, tok = _me(conn, db)
            R.handoff_job(conn, args.id, me, args.to, token=tok)
            print(f"{args.id.upper()} now directed by {args.to.upper()}")
        elif act == "requeue":
            me, tok = _me(conn, db)
            R.requeue_job(conn, args.id, me, stale_minutes=cfg.stale_after_minutes, token=tok)
            print(f"{args.id.upper()} is back in the queue")
        else:
            return EXIT_USAGE
    elif g == "onboard":
        if args.status:
            done, why = O.status(conn, args.id)
            print(("COMPLETE: " if done else "NOT COMPLETE: ") + why)
            return EXIT_OK if done else EXIT_REFUSED
        with _OnboardLock(db):
            actor = _owner_actor(conn, db)
            problems = O.check_prerequisites(conn, cfg)
            if problems:
                raise E.ValidationError("cannot onboard: " + "; ".join(problems))
            op = args.id.strip().upper()
            if actor != "owner" and R.get_setting(conn, "require_approval") == "1" and not args.approval_ref.strip():
                raise E.ValidationError("joining the team needs the owner's approval on record: pass --approval-ref with the "
                                        "record or message that shows it")
            existing = next((o for o in R.list_operators(conn) if o["id"] == op), None)
            if existing is not None and existing["status"] == "PENDING" and not existing["packet_hash"] and actor != "owner":
                token = R.reset_token(conn, op, decision_ref="onboard-resume", by=actor, token=_token_for(conn, db, actor))
                print(f"{op} was registered but never finished onboarding; resuming with a new token.")
            else:
                token = R.register_operator(conn, op, args.role)
            _write_token(db, op, token)
            R.approve_operator(conn, op, by=actor, approval_ref=args.approval_ref or "approved by the owner when installing Synergy",
                               scopes=args.scopes, token=_token_for(conn, db, actor) if actor != "owner" else "", admin=args.admin)
            packet, launcher, digest = O.write_files(conn, cfg, db, op)
            R.set_packet_hash(conn, op, digest)
        print(f"{op} is PENDING. Onboarding is NOT COMPLETE until the instance reads its packet and acknowledges it.")
        print(f"packet:   {packet}")
        print(f"launcher: {launcher}  (starts a session as {op}; run it yourself, nothing was started)")
        print("A separate Claude account must sign in on its own; this tool cannot do that for it.")
        print(f"check:    synergy onboard --status {op}")
    elif g == "rules":
        if act == "set":
            if args.file:
                with open(args.file, "rb") as handle:
                    value = hashlib.sha256(handle.read()).hexdigest()
            else:
                value = args.hash
            actor = _owner_actor(conn, db)
            if args.by not in ("owner", actor):
                raise E.AuthFailed(f"--by must be your own identity ({actor}); the audit log records who really ran this")
            R.set_rules_hash(conn, value, by=actor, token=_token_for(conn, db, actor) if actor != "owner" else "")
            print(f"rules hash set: {value}")
        elif act == "show":
            print(R.get_setting(conn, "rules_hash") or "(not set)")
        else:
            return EXIT_USAGE
    elif g == "operator":
        if act == "register":
            _owner_actor(conn, db)
            token = R.register_operator(conn, args.id, args.role, args.environment, args.account)
            path = _write_token(db, args.id.strip().upper(), token)
            print(f"registered {args.id.upper()} as PENDING; its token is stored owner-only at {path}")
            print(f"the session that acts as {args.id.upper()} has SYNERGY_OPERATOR={args.id.upper()} set by its "
                  "launcher, then runs `operator ack`")
        elif act == "ack":
            R.ack_operator(conn, args.id, args.hash, token=_token_for(conn, db, args.id))
            print(f"{args.id.upper()} is ACTIVE")
        elif act == "session":
            me, tok = _me(conn, db)
            R.set_session_name(conn, me, args.name, token=tok)
            print(f"session name for {me} recorded (a hint only; it is checked against the live session list before use)")
        elif act == "profile":
            me, tok = _me(conn, db)
            R.set_profile(conn, me, skills=args.skills, model_pref=args.model, max_tasks=args.max_tasks,
                          availability=args.availability, token=tok)
            print(f"profile updated for {me}")
        elif act == "heartbeat":
            R.heartbeat(conn, args.id, token=_token_for(conn, db, args.id))
        elif act == "token":
            if args.sub == "rotate":
                new = R.issue_token(conn, args.id, current_token=_token_for(conn, db, args.id))
                _write_token(db, args.id.strip().upper(), new)
                print(f"token for {args.id.upper()} replaced; the old one no longer works")
            elif args.sub == "reset":
                new = R.reset_token(conn, args.id, decision_ref=args.decision, by=args.by,
                                    token=_token_for(conn, db, args.by))
                _write_token(db, args.id.strip().upper(), new)
                print(f"token for {args.id.upper()} replaced by {args.by.upper()} (decision recorded); the old one no longer works")
            elif args.sub == "backfill":
                _owner_actor(conn, db)
                missing = [o["id"] for o in R.list_operators(conn) if not o["token_hash"]]
                for name in missing:
                    _write_token(db, name, R.issue_token(conn, name))
                print("tokens issued to: " + (", ".join(missing) if missing else "(nobody: all have one)"))
                R.set_require_tokens(conn, True)
                print("token checks are now required")
            else:
                return EXIT_USAGE
        elif act == "list":
            moment = datetime.now(UTC)
            rows = R.list_operators(conn)
            print("\n".join(R.describe_operator(conn, o, moment) for o in rows) if rows else "(none)")
        else:
            return EXIT_USAGE
    elif g == "task":
        if act == "create":
            R.create_task(conn, args.id, args.description, actor=args.actor, working_dir=args.dir,
                          branch=args.branch, deliverables=args.deliverables, depends_on=args.depends_on,
                          token=_token_for(conn, db, args.actor), request_id=args.request)
            print(f"task {args.id} created")
        elif act == "claim":
            now = R.claim_task(conn, args.id, args.operator, token=_token_for(conn, db, args.operator))
            print(f"task {args.id} claimed by {args.operator.upper()}" if now else f"task {args.id} is already yours")
        elif act == "assign":
            R.assign_task(conn, args.id, args.operator, by=args.by, token=_token_for(conn, db, args.by))
            print(f"task {args.id} assigned to {args.operator.upper()}")
        elif act == "release":
            R.release_task(conn, args.id, args.operator, token=_token_for(conn, db, args.operator))
            print(f"task {args.id} released")
        elif act == "start":
            me, tok = _me(conn, db)
            R.start_task(conn, args.id, me, token=tok)
            print(f"task {args.id.upper()} acknowledged and running; the operator that asked was told")
        elif act == "fail":
            me, tok = _me(conn, db)
            R.fail_task(conn, args.id, me, args.reason, token=tok)
            print(f"task {args.id.upper()} given back; it returns to the pool and the asker was told")
            _tick(conn, me, tok)
        elif act == "status":
            if len(args.rest) == 1:
                who, tok_for = _me(conn, db)
                new_status = args.rest[0]
            elif len(args.rest) == 2:
                who, new_status = args.rest
                tok_for = _token_for(conn, db, who)
            else:
                raise E.ValidationError("use `task status ID STATUS` or `task status ID OPERATOR STATUS`")
            R.set_task_status(conn, args.id, who, new_status, args.evidence, token=tok_for)
            print(f"task {args.id} is {new_status.upper()}")
            if new_status.upper() in ("DONE", "CANCELLED"):
                _tick(conn, who.upper(), tok_for)
        elif act == "reassign":
            R.reassign_task(conn, args.id, args.new_owner, decision_ref=args.decision, by=args.by,
                            token=_token_for(conn, db, args.by))
            print(f"task {args.id} reassigned to {args.new_owner.upper()}")
        elif act == "list":
            print(_table(R.list_tasks(conn), ("id", "status", "owner", "description")))
        else:
            return EXIT_USAGE
    elif g == "file":
        if act == "claim":
            now = R.claim_file(conn, args.path, args.operator, task_id=args.task, kind=args.kind,
                               token=_token_for(conn, db, args.operator))
            print("claimed" if now else "already yours")
        elif act == "release":
            R.release_file(conn, args.path, args.operator, token=_token_for(conn, db, args.operator))
            print("released")
        elif act == "list":
            print(_table(R.list_files(conn), ("owner", "kind", "path")))
        else:
            return EXIT_USAGE
    elif g == "worker":
        if act == "register":
            R.register_worker(conn, args.id, args.operator, task_id=args.task, window=args.window, directory=args.dir,
                              token=_token_for(conn, db, args.operator))
            print(f"worker {args.id} recorded (this does not start anything)")
        elif act == "state":
            by = args.by or os.environ.get("SYNERGY_OPERATOR", "")
            R.set_worker_state(conn, args.id, args.state, by=by, token=_token_for(conn, db, by) if by else "",
                               evidence=args.evidence)
            print(f"worker {args.id} is {args.state.upper()}")
        elif act == "list":
            print(_table(R.list_workers(conn), ("id", "state", "operator", "window")))
        elif act == "start":
            T.start_worker(conn, cfg, args.id, args.operator, directory=args.dir, argv=args.command,
                           task_id=args.task, allow_bypass=args.allow_bypass, token=_token_for(conn, db, args.operator))
            print(f"worker {args.id} started in {cfg.tmux_session} (registered first)")
        else:
            return EXIT_USAGE
    return EXIT_OK


def _remind(db: str) -> None:
    """After an operator's command, remind a session that is not listening, at most every ten minutes. Never raises."""
    try:
        op = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
        if not op or os.environ.get("SYNERGY_NO_REMIND") == "1" or not os.path.exists(db):
            return
        conn = R.open_registry(db, timeout=1.0)
        try:
            due = R.reminder_due(conn, op)
        finally:
            conn.close()
        if due:
            print("[synergy] You are not listening for assignments. Start `synergy inbox wait` as a background command so "
                  "work reaches you without polling; it costs no tokens while it waits.", file=sys.stderr)
    except Exception:  # noqa: BLE001
        return


def _session_start_hook() -> int:
    """Always exits 0 and never raises. For operator sessions only, it adds a short instruction to the session's context."""
    try:
        import json

        op = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
        if not op or os.environ.get("SYNERGY_HOOK_DISABLED") == "1":
            return EXIT_OK
        if os.path.exists(os.path.join(C.config_dir(os.environ), "hook.disabled")):
            return EXIT_OK
        cfg, db = _paths()
        if not os.path.exists(db):
            return EXIT_OK
        probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            version = probe.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            probe.close()
        if version != R.SCHEMA_VERSION:
            return EXIT_OK
        conn = R.open_registry(db, timeout=1.5)
        try:
            text = R.session_context(conn, op)
        finally:
            conn.close()
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:  # noqa: BLE001 - a hook must never break the session it serves
        return EXIT_OK
    return EXIT_OK


def _stop_hook() -> int:
    """Always exits 0 and never raises: a broken setup must never block a session. Prints a block decision only when the
    session's own operator has a pending assignment and every safety condition in the registry holds."""
    try:
        import json

        if os.environ.get("SYNERGY_HOOK_DISABLED") == "1":
            return EXIT_OK
        op = os.environ.get("SYNERGY_OPERATOR", "").strip().upper()
        if not op:
            return EXIT_OK
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict) or payload.get("stop_hook_active"):
            return EXIT_OK
        if os.path.exists(os.path.join(C.config_dir(os.environ), "hook.disabled")):
            return EXIT_OK
        cfg, db = _paths()
        if not os.path.exists(db):
            return EXIT_OK
        probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            version = probe.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            probe.close()
        if version != R.SCHEMA_VERSION:
            return EXIT_OK
        conn = R.open_registry(db, timeout=1.5)
        try:
            reason = R.hook_decision(conn, op)
        finally:
            conn.close()
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:  # noqa: BLE001 - a hook must never break the session it serves
        return EXIT_OK
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    command: list[str] = []
    if raw[:2] == ["worker", "start"] and "--" in raw:
        split = raw.index("--")
        raw, command = raw[:split], raw[split + 1:]
    try:
        args = parser.parse_args(raw)
        args.command = command
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if not args.group:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if args.group == "inbox" and args.action == "hook":
        return _stop_hook()
    if args.group == "inbox" and args.action == "session-start":
        return _session_start_hook()
    try:
        cfg, db = _paths()
        code = _run(args, cfg, db)
        if code == EXIT_OK and args.group not in ("init", "review") and not (args.group == "inbox" and args.action == "wait"):
            _remind(db)
        return code
    except E.SynergyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except sqlite3.DatabaseError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"refused: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        print(f"refused: unexpected input ({exc.__class__.__name__})", file=sys.stderr)
        return EXIT_REFUSED

