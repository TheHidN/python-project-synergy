"""The shared coordination registry: one SQLite file, written only in BEGIN IMMEDIATE transactions.

What this enforces: atomic claims, unique ids, exclusive file and worktree ownership, a worker limit,
and an append-only hash-chained event log. What it does not enforce: any process of the same Unix user
can open the file directly (triggers stop ordinary UPDATEs, not a deliberate DROP TRIGGER), and it
cannot stop another process from editing the files whose ownership it records. The event log's hash chain
detects accidental corruption; it is not tamper evidence (the hash is unkeyed and the tail can be cut), so
keep a copy of the head hash that `synergy verify` prints somewhere else if that matters. Operator ids are
self-asserted: any process of the same user can name any operator (a trust-the-operators model).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import errors as E

SCHEMA_VERSION = 10
TASK_STATUSES = ("OPEN", "CLAIMED", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED")
WORKER_STATES = ("RUNNING", "WORKING", "IDLE", "WAITING", "BLOCKED", "FINISHED", "UNKNOWN")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_SCHEMA_V1 = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE operators(
  id TEXT PRIMARY KEY CHECK (id = upper(id) AND length(id) BETWEEN 1 AND 32),
  role TEXT NOT NULL DEFAULT '', environment TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','ACTIVE','SUSPENDED')),
  registered_at TEXT NOT NULL, last_seen TEXT NOT NULL, ack_hash TEXT NOT NULL DEFAULT '');
CREATE TABLE tasks(
  id TEXT PRIMARY KEY, description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLAIMED','IN_PROGRESS','BLOCKED','DONE','CANCELLED')),
  owner TEXT REFERENCES operators(id), working_dir TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
  deliverables TEXT NOT NULL DEFAULT '', completion_evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, claimed_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE task_deps(
  task_id TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on));
CREATE TABLE file_locks(
  path TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES operators(id), task_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('file','worktree')), claimed_at TEXT NOT NULL);
CREATE TABLE workers(
  id TEXT PRIMARY KEY, operator TEXT NOT NULL REFERENCES operators(id), task_id TEXT NOT NULL DEFAULT '',
  window TEXT NOT NULL DEFAULT '', directory TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('RUNNING','WORKING','IDLE','WAITING','BLOCKED','FINISHED','UNKNOWN')),
  created_at TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  subject TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER tasks_owner_active_ins BEFORE INSERT ON tasks WHEN NEW.owner IS NOT NULL
BEGIN SELECT RAISE(ABORT,'owner must be an ACTIVE operator')
  WHERE (SELECT status FROM operators WHERE id = NEW.owner) IS NOT 'ACTIVE'; END;
CREATE TRIGGER tasks_owner_active_upd BEFORE UPDATE OF owner ON tasks
  WHEN NEW.owner IS NOT NULL AND NEW.owner IS NOT OLD.owner
BEGIN SELECT RAISE(ABORT,'owner must be an ACTIVE operator')
  WHERE (SELECT status FROM operators WHERE id = NEW.owner) IS NOT 'ACTIVE'; END;
CREATE TRIGGER tasks_owner_change BEFORE UPDATE OF owner ON tasks
  WHEN OLD.owner IS NOT NULL AND NEW.owner IS NOT NULL AND NEW.owner IS NOT OLD.owner
BEGIN SELECT RAISE(ABORT,'task is owned: release it or reassign it with a decision reference'); END;
CREATE TRIGGER locks_owner_active BEFORE INSERT ON file_locks
BEGIN SELECT RAISE(ABORT,'owner must be an ACTIVE operator')
  WHERE (SELECT status FROM operators WHERE id = NEW.owner) IS NOT 'ACTIVE'; END;
"""


_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE operators ADD COLUMN token_hash TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN packet_hash TEXT NOT NULL DEFAULT ''",
    ],
    3: [
        "CREATE TABLE requests(id TEXT PRIMARY KEY, text TEXT NOT NULL, origin TEXT NOT NULL REFERENCES operators(id), "
        "status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','DONE','CANCELLED')), "
        "summary TEXT NOT NULL DEFAULT '', needs TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, closed_at TEXT)",
        "CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT NOT NULL REFERENCES operators(id), "
        "kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT)",
        "CREATE INDEX notifications_unread ON notifications(operator, read_at)",
        "ALTER TABLE tasks ADD COLUMN request_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN listening_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN skills TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN model_pref TEXT NOT NULL DEFAULT 'any'",
        "ALTER TABLE operators ADD COLUMN max_tasks INTEGER NOT NULL DEFAULT 3",
        "ALTER TABLE operators ADD COLUMN availability TEXT NOT NULL DEFAULT 'AVAILABLE'",
        "ALTER TABLE operators ADD COLUMN kind TEXT NOT NULL DEFAULT 'OPERATOR'",
        "ALTER TABLE operators ADD COLUMN current_job TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN controller TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN window TEXT NOT NULL DEFAULT ''",
        "CREATE TABLE jobs(id TEXT PRIMARY KEY, text TEXT NOT NULL, requester TEXT NOT NULL REFERENCES operators(id), "
        "agent_id TEXT NOT NULL DEFAULT '', needs TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT 'any', "
        "priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3), "
        "status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','RESERVED','RUNNING','DONE','FAILED','CANCELLED')), "
        "request_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)",
        "CREATE INDEX jobs_queue ON jobs(status, priority, created_at)",
    ],
    4: [
        "CREATE TABLE accounts(id TEXT PRIMARY KEY, percent REAL, resets_at TEXT NOT NULL DEFAULT '', "
        "reserve REAL NOT NULL DEFAULT 5, source TEXT NOT NULL DEFAULT 'manual', verified_at TEXT NOT NULL DEFAULT '')",
        "ALTER TABLE operators ADD COLUMN routed INTEGER NOT NULL DEFAULT 0",
    ],
    5: [
        "ALTER TABLE tasks ADD COLUMN needs TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN files TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN tried TEXT NOT NULL DEFAULT ''",
    ],
    6: [
        "ALTER TABLE tasks ADD COLUMN started_at TEXT",
        "ALTER TABLE tasks ADD COLUMN auto INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE operators ADD COLUMN session_name TEXT NOT NULL DEFAULT ''",
    ],
    7: [
        "ALTER TABLE operators ADD COLUMN trust TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN approved_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN approval_ref TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN approved_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE operators ADD COLUMN scopes TEXT NOT NULL DEFAULT '*'",
        "ALTER TABLE operators ADD COLUMN can_assign INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE operators ADD COLUMN can_receive INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE requests ADD COLUMN project TEXT NOT NULL DEFAULT 'general'",
        "UPDATE operators SET trust='APPROVED', approved_by='migration', approved_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), "
        "approval_ref='already active before the trust layer existed' WHERE status IN ('ACTIVE','SUSPENDED')",
        "INSERT OR REPLACE INTO settings SELECT 'require_approval','1' WHERE EXISTS (SELECT 1 FROM operators)",
    ],
    8: [
        "CREATE TABLE hook_blocks(notification_id INTEGER PRIMARY KEY REFERENCES notifications(id), blocks INTEGER NOT NULL DEFAULT 0, "
        "last_at TEXT NOT NULL DEFAULT '')",
    ],
    9: [
        "ALTER TABLE operators ADD COLUMN admin INTEGER NOT NULL DEFAULT 0",
        "UPDATE operators SET admin=1 WHERE status='ACTIVE' AND kind='OPERATOR' AND trust='APPROVED'",
        "INSERT OR REPLACE INTO settings SELECT 'bootstrapped','1' WHERE EXISTS (SELECT 1 FROM operators WHERE status IN ('ACTIVE','SUSPENDED'))",
    ],
    10: [
        "ALTER TABLE jobs ADD COLUMN project TEXT NOT NULL DEFAULT 'general'",
    ]
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _op(op_id: str) -> str:
    value = (op_id or "").strip().upper()
    if not _ID.match(value):
        raise E.ValidationError("an id is 1-32 letters, digits, '_' or '-'")
    return value


def _ident(value: str, what: str = "id") -> str:
    value = (value or "").strip()
    if not _ID.match(value):
        raise E.ValidationError(f"a {what} is 1-32 letters, digits, '_' or '-'")
    return value


def open_registry(path: str | os.PathLike[str], timeout: float = 30) -> sqlite3.Connection:
    """Open the registry, creating it (and its private folder) on first use. Safe to call repeatedly."""
    target = Path(path)
    folder = target.parent
    if folder.exists():
        if folder.stat().st_uid != os.getuid():
            raise E.ValidationError(f"{folder} is owned by another user; refusing to use it")
    else:
        old_umask = os.umask(0o077)
        try:
            folder.mkdir(mode=0o700, parents=True)
        finally:
            os.umask(old_umask)
        os.chmod(folder, 0o700)
    fresh = not target.exists()
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(str(target), timeout=timeout, isolation_level=None)
    finally:
        os.umask(old_umask)
    if fresh:
        os.chmod(target, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    for suffix in ("", "-wal", "-shm"):
        extra = Path(str(target) + suffix)
        if extra.exists() and extra.stat().st_uid == os.getuid():
            os.chmod(extra, stat.S_IMODE(extra.stat().st_mode) & 0o700)
    version = _current_version(conn)
    if version is not None and version > SCHEMA_VERSION:
        conn.close()
        raise E.ValidationError(f"this registry is schema {version}, newer than this program understands ({SCHEMA_VERSION}); "
                                "update Synergy instead of opening it")
    if version == SCHEMA_VERSION:
        return conn
    if version is not None and version < SCHEMA_VERSION:
        _backup(conn, target, version)
    with tx(conn):
        version = _current_version(conn)
        if version is None:
            for statement in _split(_SCHEMA_V1):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version VALUES (1)")
            conn.execute("INSERT INTO settings VALUES ('max_workers','2')")
            _log(conn, "system", "registry.init", "registry", "schema 1")
            _migrate(conn, 1)
            conn.execute("INSERT INTO settings VALUES ('require_tokens','1')")
        elif version < SCHEMA_VERSION:
            _migrate(conn, version)
            conn.execute("INSERT OR IGNORE INTO settings VALUES ('require_tokens','0')")
    return conn


def _current_version(conn: sqlite3.Connection) -> int | None:
    have = conn.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone()
    if have is None:
        return None
    return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])


def _backup(conn: sqlite3.Connection, target: Path, version: int) -> None:
    prefix = f"{target.name}.bak-v{version}-"
    if any(p.name.startswith(prefix) and not p.name.endswith(".tmp") for p in target.parent.iterdir()):
        return
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    final = target.with_name(f"{prefix}{stamp}")
    partial = target.with_name(f"{prefix}{stamp}.tmp")
    old_umask = os.umask(0o077)
    try:
        copy = sqlite3.connect(str(partial))
    finally:
        os.umask(old_umask)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    os.chmod(partial, 0o600)
    os.replace(partial, final)


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    for version in range(from_version + 1, SCHEMA_VERSION + 1):
        for statement in _MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute("UPDATE schema_version SET version=?", (version,))
        _log(conn, "system", "registry.migrate", "registry", f"schema {version - 1} to {version}")


def _split(script: str) -> Iterator[str]:
    buf: list[str] = []
    for line in script.splitlines():
        buf.append(line)
        joined = "\n".join(buf)
        if sqlite3.complete_statement(joined):
            yield joined
            buf = []


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _log(conn: sqlite3.Connection, actor: str, action: str, subject: str, detail: str = "") -> None:
    row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else "0" * 64
    ts = _ts()
    digest = hashlib.sha256("|".join((prev, ts, actor, action, subject, detail)).encode()).hexdigest()
    conn.execute(
        "INSERT INTO events(ts, actor, action, subject, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (ts, actor, action, subject, detail, prev, digest),
    )


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, str]:
    prev = "0" * 64
    count = 0
    for row in conn.execute("SELECT * FROM events ORDER BY seq"):
        expect = hashlib.sha256(
            "|".join((prev, row["ts"], row["actor"], row["action"], row["subject"], row["detail"])).encode()
        ).hexdigest()
        if row["prev_hash"] != prev or row["hash"] != expect:
            return False, f"event {row['seq']} does not match the chain"
        prev = row["hash"]
        count += 1
    return True, f"{count} events, chain intact, head {prev[:12]}"


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with tx(conn):
        conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        _log(conn, "system", "setting.set", key, "" if key.endswith("hash") else value)


def _require_active(conn: sqlite3.Connection, op_id: str) -> str:
    op = _op(op_id)
    row = conn.execute("SELECT status FROM operators WHERE id=?", (op,)).fetchone()
    if row is None:
        raise E.NotFound(f"operator {op} is not registered")
    if row["status"] != "ACTIVE":
        raise E.NotActive(f"operator {op} is {row['status']}, not ACTIVE: it must acknowledge the rules first")
    return op


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _authn(conn: sqlite3.Connection, op: str, token: str) -> None:
    """Prove the caller holds the operator's token. Call inside a transaction, before acting."""
    row = conn.execute("SELECT token_hash FROM operators WHERE id=?", (op,)).fetchone()
    if row is None:
        raise E.NotFound(f"operator {op} is not registered")
    required = get_setting(conn, "require_tokens") == "1"
    if not token:
        if required:
            raise E.AuthFailed(f"operator {op}: a token is required for this action")
        return
    stored = row["token_hash"]
    if not stored or not hmac.compare_digest(_hash_token(token), stored):
        raise E.AuthFailed(f"operator {op}: the token does not match")


def authenticate_active(conn: sqlite3.Connection, op_id: str, token: str = "") -> str:
    """Check the operator's token and that it is ACTIVE; returns its id."""
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        return _require_active(conn, op)


def authenticate_operator(conn: sqlite3.Connection, op_id: str, token: str = "") -> str:
    """Like authenticate_active, but the session must be an operator, not a pooled agent."""
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        return _require_operator(conn, op)


def issue_token(conn: sqlite3.Connection, op_id: str, current_token: str = "") -> str:
    """Issue the first token, or rotate: replacing an existing token needs the current one."""
    op = _op(op_id)
    with tx(conn):
        row = conn.execute("SELECT token_hash FROM operators WHERE id=?", (op,)).fetchone()
        if row is None:
            raise E.NotFound(f"operator {op} is not registered")
        rotating = bool(row["token_hash"])
        if rotating and not (current_token and hmac.compare_digest(_hash_token(current_token), row["token_hash"])):
            raise E.AuthFailed(f"operator {op}: rotating needs the current token")
        token = secrets.token_urlsafe(32)
        conn.execute("UPDATE operators SET token_hash=? WHERE id=?", (_hash_token(token), op))
        _log(conn, op, "token.rotate" if rotating else "token.issue", op, "")
        return token


def reset_token(conn: sqlite3.Connection, op_id: str, *, decision_ref: str, by: str, token: str = "") -> str:
    """Replace a lost token. Needs a decision reference and an authenticated, active decider."""
    op = _op(op_id)
    if not decision_ref.strip():
        raise E.ValidationError("resetting a token needs a decision reference")
    with tx(conn):
        decider = _op(by)
        _authn(conn, decider, token)
        _require_operator(conn, decider)
        _require_admin(conn, decider)
        if decider == op:
            raise E.NotOwner("nobody resets their own token through this path; rotate it with the current token instead")
        if conn.execute("SELECT 1 FROM operators WHERE id=?", (op,)).fetchone() is None:
            raise E.NotFound(f"operator {op} is not registered")
        new = secrets.token_urlsafe(32)
        conn.execute("UPDATE operators SET token_hash=? WHERE id=?", (_hash_token(new), op))
        _log(conn, decider, "token.reset", op, f"decision={decision_ref.strip()}")
        _notify(conn, op, "token_reset", op, f"{decider} reset your token")
        return new


def set_require_tokens(conn: sqlite3.Connection, on: bool, by: str = "", token: str = "") -> None:
    with tx(conn):
        if on:
            missing = [r["id"] for r in conn.execute("SELECT id FROM operators WHERE token_hash=''")]
            if missing:
                raise E.ValidationError("these operators have no token yet: " + ", ".join(missing))
        elif get_setting(conn, "require_tokens") == "1" or get_setting(conn, "bootstrapped", "0") == "1":
            if not by:
                raise E.AuthFailed("turning token checks off needs an authenticated administrator")
            decider = _op(by)
            _authn(conn, decider, token)
            _require_operator(conn, decider)
            _require_admin(conn, decider)
        conn.execute("INSERT INTO settings VALUES ('require_tokens',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("1" if on else "0",))
        _log(conn, by or "system", "identity.require", "require_tokens", "on" if on else "off")


def register_operator(
    conn: sqlite3.Connection, op_id: str, role: str = "", environment: str = "", account: str = "",
    kind: str = "OPERATOR", skills: str = "", model_pref: str = "any", window: str = "",
) -> str:
    op = _op(op_id)
    if kind not in ("OPERATOR", "AGENT"):
        raise E.ValidationError("kind is OPERATOR or AGENT")
    skills = _tags(skills)
    if not _MODEL.fullmatch(model_pref):
        raise E.ValidationError("a model preference is any, sonnet, opus, haiku or a full model id")
    now = _ts()
    token = secrets.token_urlsafe(32)
    with tx(conn):
        if conn.execute("SELECT 1 FROM operators WHERE id=?", (op,)).fetchone():
            raise E.DuplicateOperator(f"operator {op} is already registered")
        conn.execute(
            "INSERT INTO operators(id, role, environment, account, registered_at, last_seen, token_hash, kind, skills, "
            "model_pref, window) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (op, role, environment, account, now, now, _hash_token(token), kind, skills, model_pref, window),
        )
        _log(conn, op, "operator.register", op, f"kind={kind} role={role}")
    return token


def set_rules_hash(conn: sqlite3.Connection, value: str, by: str = "owner", token: str = "") -> None:
    """Record the hash of the rules packet operators must have read. Acknowledgement is refused until set."""
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,128}", value):
        raise E.ValidationError("a rules hash is 8-128 letters and digits (a sha256 is 64 hex characters)")
    with tx(conn):
        if get_setting(conn, "bootstrapped", "0") == "1":
            actor = _op(by)
            _authn(conn, actor, token)
            _require_operator(conn, actor)
            _require_admin(conn, actor)
        conn.execute("INSERT INTO settings VALUES ('rules_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        _log(conn, by, "rules.set", "rules_hash", value)


def ack_operator(conn: sqlite3.Connection, op_id: str, packet_hash: str, token: str = "") -> None:
    op = _op(op_id)
    if not packet_hash.strip():
        raise E.BadAck("an acknowledgement needs the hash of the rules packet that was read")
    with tx(conn):
        row = conn.execute("SELECT status, packet_hash FROM operators WHERE id=?", (op,)).fetchone()
        if row is None:
            raise E.NotFound(f"operator {op} is not registered")
        _authn(conn, op, token)
        if row["status"] == "SUSPENDED":
            raise E.NotActive(f"operator {op} is SUSPENDED and cannot re-activate itself")
        if _approval_required(conn) and conn.execute("SELECT trust FROM operators WHERE id=?", (op,)).fetchone()["trust"] != "APPROVED":
            raise E.NotActive(f"operator {op} has no owner approval on record; the owner approves joining first "
                              "(`synergy onboard ID --approval-ref ...` or `synergy team approve`)")
        expected = row["packet_hash"] or get_setting(conn, "rules_hash")
        if not expected:
            raise E.BadAck("no rules hash is set yet: the owner records it first (`synergy rules set`)")
        if packet_hash.strip() != expected:
            raise E.BadAck("the hash does not match this operator's packet; read it again and use its hash")
        conn.execute(
            "UPDATE operators SET status='ACTIVE', ack_hash=?, last_seen=? WHERE id=?",
            (packet_hash.strip(), _ts(), op),
        )
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('bootstrapped','1')")
        _log(conn, op, "operator.ack", op, "acknowledged")


def set_packet_hash(conn: sqlite3.Connection, op_id: str, packet_hash: str) -> None:
    """Record the hash of the onboarding packet this operator must read; its acknowledgement must match it."""
    op = _op(op_id)
    with tx(conn):
        row = conn.execute("SELECT status FROM operators WHERE id=?", (op,)).fetchone()
        if row is not None and row["status"] == "ACTIVE":
            raise E.NotActive(f"{op} is already active; its packet hash can no longer be changed")
        if conn.execute("UPDATE operators SET packet_hash=? WHERE id=?", (packet_hash, op)).rowcount != 1:
            raise E.NotFound(f"operator {op} is not registered")
        _log(conn, "system", "operator.packet", op, packet_hash)


def heartbeat(conn: sqlite3.Connection, op_id: str, token: str = "") -> None:
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        if conn.execute("UPDATE operators SET last_seen=? WHERE id=?", (_ts(), op)).rowcount != 1:
            raise E.NotFound(f"operator {op} is not registered")


def list_operators(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM operators ORDER BY id"))


def stale_operators(conn: sqlite3.Connection, minutes: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """Report only. A stale operator never loses a task automatically."""
    cutoff = _ts((now or _now()) - timedelta(minutes=minutes))
    return list(conn.execute("SELECT * FROM operators WHERE last_seen < ? ORDER BY id", (cutoff,)))


def create_task(
    conn: sqlite3.Connection, task_id: str, description: str, *, actor: str, working_dir: str = "",
    branch: str = "", deliverables: str = "", depends_on: Iterable[str] = (), token: str = "", request_id: str = "",
) -> None:
    tid = _ident(task_id, "task id")
    if not description.strip():
        raise E.ValidationError("a task needs a description")
    now = _ts()
    if task_id.strip().upper().startswith(("REQ-", "JOB-")):
        raise E.ValidationError("the ids REQ-... and JOB-... are reserved for requests and jobs")
    with tx(conn):
        _authn(conn, _op(actor), token)
        who = _require_operator(conn, actor)
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise E.TaskExists(f"task {tid} already exists")
        deps = list(dict.fromkeys(_ident(d, "task id") for d in depends_on))
        for dep in deps:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (dep,)).fetchone():
                raise E.NotFound(f"dependency {dep} does not exist")
        if request_id:
            owner_row = conn.execute("SELECT origin FROM requests WHERE id=?", (request_id,)).fetchone()
            if owner_row is None:
                raise E.NotFound(f"request {request_id} does not exist")
            taker = conn.execute("SELECT owner FROM tasks WHERE id=?", (request_id,)).fetchone()
            if who != owner_row["origin"] and not (taker and taker["owner"] == who):
                raise E.NotOwner(f"{request_id} was asked by {owner_row['origin']}; only that operator, or the one that took it, "
                                 "attaches tasks to it")
        conn.execute(
            "INSERT INTO tasks(id, description, working_dir, branch, deliverables, created_at, updated_at, request_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, description.strip(), working_dir, branch, deliverables, now, now, request_id),
        )
        for dep in deps:
            conn.execute("INSERT INTO task_deps VALUES (?,?)", (tid, dep))
        _log(conn, who, "task.create", tid, description.strip()[:200])


def _approval_required(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "require_approval", "0") == "1"


def _scope_ok(scopes: str, project: str) -> bool:
    items = {x for x in scopes.split(",") if x}
    return "*" in items or project in items


def _held(conn: sqlite3.Connection, project: str) -> bool:
    return get_setting(conn, f"hold.{project}", "") == "1"


def _task_project(conn: sqlite3.Connection, tid: str) -> str:
    row = conn.execute("SELECT r.project FROM tasks t LEFT JOIN requests r ON r.id=t.request_id WHERE t.id=?", (tid,)).fetchone()
    return (row["project"] if row and row["project"] else "general")


def _check_member(conn: sqlite3.Connection, op: str, project: str, receiving: bool) -> None:
    row = conn.execute("SELECT * FROM operators WHERE id=?", (op,)).fetchone()
    if row is None:
        raise E.NotFound(f"operator {op} is not registered")
    if _approval_required(conn) and row["trust"] != "APPROVED":
        raise E.NotActive(f"{op} is not an approved team member")
    if not _scope_ok(row["scopes"], project):
        raise E.NotActive(f"{op} is not authorized for the project {project!r}")
    if receiving and not row["can_receive"]:
        raise E.NotActive(f"{op} is suspended from receiving new work")
    if not receiving and not row["can_assign"]:
        raise E.NotActive(f"{op} is not allowed to assign work")


def _check_assignment(conn: sqlite3.Connection, tid: str, receiver: str, actor: str) -> None:
    """Every assignment is checked: both sides approved, in scope, allowed to send or receive, project not on hold."""
    project = _task_project(conn, tid)
    if _held(conn, project):
        raise E.NotActive(f"the project {project!r} is on hold; no new work is assigned")
    _check_member(conn, receiver, project, receiving=True)
    if actor != receiver:
        _check_member(conn, actor, project, receiving=False)


def _claim(conn: sqlite3.Connection, tid: str, op: str, actor: str, action: str) -> bool:
    row = conn.execute("SELECT status, owner, claimed_at FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        raise E.NotFound(f"task {tid} does not exist")
    if row["owner"] == op:
        return False
    if row["owner"] is not None:
        raise E.AlreadyOwned(tid, row["owner"], row["claimed_at"] or "?")
    if row["status"] != "OPEN":
        raise E.NotClaimable(f"task {tid} is {row['status']}, not OPEN")
    _check_assignment(conn, tid, op, actor)
    pending = conn.execute(
        "SELECT d.depends_on FROM task_deps d JOIN tasks t ON t.id = d.depends_on "
        "WHERE d.task_id=? AND t.status != 'DONE'", (tid,),
    ).fetchall()
    if pending:
        raise E.DependencyNotDone(f"task {tid} waits for: " + ", ".join(r["depends_on"] for r in pending))
    now = _ts()
    cur = conn.execute(
        "UPDATE tasks SET owner=?, status='CLAIMED', claimed_at=?, updated_at=? "
        "WHERE id=? AND owner IS NULL AND status='OPEN'", (op, now, now, tid),
    )
    if cur.rowcount != 1:
        raise E.AlreadyOwned(tid, "another operator", now)
    _log(conn, actor, action, tid, f"owner={op}")
    return True


def claim_task(conn: sqlite3.Connection, task_id: str, op_id: str, token: str = "") -> bool:
    """Atomically claim a task. True when claimed now, False when the caller already owned it."""
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        _agent_internal(conn, op, task_id)
        return _claim(conn, tid, op, op, "task.claim")


def assign_task(conn: sqlite3.Connection, task_id: str, op_id: str, *, by: str, token: str = "") -> bool:
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    with tx(conn):
        _authn(conn, _op(by), token)
        actor = _require_operator(conn, by)
        _require_active(conn, op)
        _may_direct(conn, actor, op)
        claimed = _claim(conn, tid, op, actor, "task.assign")
        if claimed and op != actor:
            row = conn.execute("SELECT description FROM tasks WHERE id=?", (tid,)).fetchone()
            _notify(conn, op, "assigned", tid, f"{actor} assigned you: {row['description'][:160]}")
        return claimed


def _owned(conn: sqlite3.Connection, tid: str, op: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        raise E.NotFound(f"task {tid} does not exist")
    if row["owner"] != op:
        raise E.NotOwner(f"task {tid} is owned by {row['owner'] or 'nobody'}, not {op}")
    return row


def set_task_status(
    conn: sqlite3.Connection, task_id: str, op_id: str, status: str, evidence: str = "", token: str = ""
) -> None:
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    status = status.strip().upper()
    if status not in TASK_STATUSES or status == "OPEN":
        raise E.ValidationError("status must be one of " + ", ".join(s for s in TASK_STATUSES if s != "OPEN"))
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        _agent_internal(conn, op, tid)
        row = _owned(conn, tid, op)
        if row["status"] in ("DONE", "CANCELLED"):
            raise E.NotClaimable(f"task {tid} is {row['status']}, which is final")
        if status == "DONE" and not evidence.strip():
            raise E.MissingEvidence("DONE needs completion evidence (--evidence)")
        conn.execute(
            "UPDATE tasks SET status=?, completion_evidence=CASE WHEN ?!='' THEN ? ELSE completion_evidence END, "
            "updated_at=? WHERE id=?", (status, evidence.strip(), evidence.strip(), _ts(), tid),
        )
        _log(conn, op, "task.status", tid, status)
        if status in ("DONE", "BLOCKED", "CANCELLED") and row["request_id"]:
            origin = conn.execute("SELECT origin FROM requests WHERE id=?", (row["request_id"],)).fetchone()
            if origin and origin["origin"] != op:
                note = evidence.strip()[:160] if evidence.strip() else "no evidence given"
                _notify(conn, origin["origin"], "task_update", tid, f"{op} set {tid} to {status} ({row['request_id']}): {note}")
        if status == "DONE" and row["request_id"] and tid != row["request_id"]:
            waiting = conn.execute("SELECT count(*) FROM tasks WHERE request_id=? AND id != ? AND status NOT IN ('DONE','CANCELLED')",
                                   (row["request_id"], row["request_id"])).fetchone()[0]
            req = conn.execute("SELECT origin, status FROM requests WHERE id=?", (row["request_id"],)).fetchone()
            if waiting == 0 and req and req["status"] == "OPEN":
                _notify(conn, req["origin"], "ready_for_review", row["request_id"],
                        f"every part of {row['request_id']} is done; review the results and close it")


def release_task(conn: sqlite3.Connection, task_id: str, op_id: str, token: str = "") -> None:
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        row = _owned(conn, tid, op)
        if row["status"] in ("DONE", "CANCELLED"):
            raise E.NotClaimable(f"task {tid} is {row['status']}, which is final")
        conn.execute(
            "UPDATE tasks SET owner=NULL, status='OPEN', claimed_at=NULL, updated_at=? WHERE id=?", (_ts(), tid)
        )
        _log(conn, op, "task.release", tid, "")


def reassign_task(
    conn: sqlite3.Connection, task_id: str, new_owner: str, *, decision_ref: str, by: str, token: str = ""
) -> None:
    """Move an owned, unfinished task. The decision reference is recorded, not verified."""
    tid = _ident(task_id, "task id")
    op = _op(new_owner)
    if not decision_ref.strip():
        raise E.ValidationError("reassigning an owned task needs a decision reference (the record of who decided it)")
    with tx(conn):
        _authn(conn, _op(by), token)
        decider = _require_operator(conn, by)
        _require_active(conn, op)
        _may_direct(conn, decider, op)
        _check_assignment(conn, tid, op, decider)
        row = conn.execute("SELECT owner, status FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise E.NotFound(f"task {tid} does not exist")
        if row["owner"] is None or row["status"] in ("DONE", "CANCELLED"):
            raise E.NotClaimable(f"task {tid} is {row['status']} with owner {row['owner'] or 'nobody'}: nothing to reassign")
        now = _ts()
        conn.execute("UPDATE tasks SET owner=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE tasks SET owner=?, status='CLAIMED', claimed_at=?, updated_at=? WHERE id=?",
                     (op, now, now, tid))
        _log(conn, decider, "task.reassign", tid, f"from={row['owner']} to={op} decision={decision_ref.strip()}")


def _notify(conn: sqlite3.Connection, operator: str, kind: str, subject: str, body: str) -> None:
    """Queue a notification for an operator inside the caller's transaction. It waits durably until read."""
    conn.execute("INSERT INTO notifications(operator, kind, subject, body, created_at) VALUES (?,?,?,?,?)",
                 (operator, kind, subject, body[:400], _ts()))


def _others(conn: sqlite3.Connection, origin: str) -> list[str]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM operators WHERE status='ACTIVE' AND kind='OPERATOR' AND id != ? ORDER BY id", (origin,))]


def _not_suspended(conn: sqlite3.Connection, op: str) -> None:
    row = conn.execute("SELECT status FROM operators WHERE id=?", (op,)).fetchone()
    if row is not None and row["status"] == "SUSPENDED":
        raise E.NotActive(f"{op} is SUSPENDED and cannot change anything")


def _may_direct(conn: sqlite3.Connection, sender: str, target: str) -> None:
    """A busy agent takes instructions only from the operator directing its job."""
    row = conn.execute("SELECT kind, current_job, controller FROM operators WHERE id=?", (target,)).fetchone()
    if row is not None and row["kind"] == "AGENT" and row["current_job"] and row["controller"] != sender:
        raise E.NotOwner(f"{target} is directed by {row['controller']} until {row['current_job']} finishes; "
                         "queue a request instead of sending it instructions")


def _agent_internal(conn: sqlite3.Connection, op: str, task_id: str) -> None:
    kind = conn.execute("SELECT kind FROM operators WHERE id=?", (op,)).fetchone()
    if kind is not None and kind["kind"] == "AGENT" and task_id.upper().startswith(("REQ-", "JOB-")):
        raise E.NotActive("agents work through their jobs and never touch request tasks")


def _require_operator(conn: sqlite3.Connection, op: str) -> str:
    """ACTIVE and a human-facing operator (an agent session cannot make or take requests)."""
    _require_active(conn, op)
    if conn.execute("SELECT kind FROM operators WHERE id=?", (op,)).fetchone()["kind"] != "OPERATOR":
        raise E.NotActive(f"{op} is an agent, not an operator, so it cannot do this")
    return op


def create_request(conn: sqlite3.Connection, text: str, origin: str, token: str = "", needs: str = "",
                   project: str = "general") -> str:
    """Record a plain-English request, give it an id and an unowned root task, and notify the other ACTIVE operators."""
    if not text.strip():
        raise E.ValidationError("a request needs some text")
    needs = _tags(needs)
    project = _tags(project) or "general"
    if "," in project:
        raise E.ValidationError("a request belongs to one project")
    op = _op(origin)
    now = _ts()
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        if _held(conn, project):
            raise E.NotActive(f"the project {project!r} is on hold; no new requests")
        _check_member(conn, op, project, receiving=False)
        number = (conn.execute("SELECT max(CAST(substr(id, 5) AS INTEGER)) FROM requests").fetchone()[0] or 0) + 1
        rid = f"REQ-{number:04d}"
        conn.execute("INSERT INTO requests(id, text, origin, needs, project, created_at) VALUES (?,?,?,?,?,?)",
                     (rid, text.strip(), op, needs, project, now))
        conn.execute("INSERT INTO tasks(id, description, created_at, updated_at, request_id) VALUES (?,?,?,?,?)",
                     (rid, text.strip(), now, now, rid))
        _log(conn, op, "request.create", rid, text.strip()[:200])
        for other in _others(conn, op):
            _notify(conn, other, "request", rid, f"{op} asks: {text.strip()[:300]}")
        return rid


_AVAILABILITY = ("AVAILABLE", "BUSY", "OUT_OF_USAGE")
_TAG = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
_MODEL = re.compile(r"(?:any|sonnet|opus|haiku|claude-[a-z0-9][a-z0-9.-]{2,60})")


def _tags(text: str) -> str:
    items = [x.strip().lower() for x in (text or "").split(",") if x.strip()]
    for item in items:
        if not _TAG.fullmatch(item):
            raise E.ValidationError(f"a skill is lowercase letters, digits, '_' or '-': {item!r}")
    return ",".join(dict.fromkeys(items))


def set_profile(conn: sqlite3.Connection, op_id: str, skills: str | None = None, model_pref: str | None = None,
                max_tasks: int | None = None, availability: str | None = None, token: str = "") -> None:
    """What this operator is good at, which model suits it, how much it will take, and whether it can work now."""
    op = _op(op_id)
    changes: dict[str, object] = {}
    if skills is not None:
        changes["skills"] = _tags(skills)
    if model_pref is not None:
        if not _MODEL.fullmatch(model_pref):
            raise E.ValidationError("a model preference is any, sonnet, opus, haiku or a full model id")
        changes["model_pref"] = model_pref
    if max_tasks is not None:
        if not isinstance(max_tasks, int) or max_tasks < 1:
            raise E.ValidationError("max tasks is a whole number of at least 1")
        changes["max_tasks"] = max_tasks
    if availability is not None:
        if availability not in _AVAILABILITY:
            raise E.ValidationError("availability is " + ", ".join(_AVAILABILITY))
        changes["availability"] = availability
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        if not changes:
            return
        sets = ", ".join(f"{k}=?" for k in changes)
        if conn.execute(f"UPDATE operators SET {sets} WHERE id=?", (*changes.values(), op)).rowcount != 1:
            raise E.NotFound(f"operator {op} is not registered")
        _log(conn, op, "operator.profile", op, ",".join(changes))


_TUNABLES = {"usage": 1.0, "skill": 5.0, "listening": 3.0, "available": 2.0, "load": 5.0, "limit": 100.0,
             "fairness": 0.05, "assume": 50.0, "ack_minutes": 30.0, "stale_hours": 6.0, "quiet_minutes": 240.0}
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_HARD_MODEL = {"security", "architecture", "concurrency", "crypto"}


def get_tunables(conn: sqlite3.Connection) -> dict[str, float]:
    out = dict(_TUNABLES)
    for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'w.%'"):
        name = row["key"][2:]
        if name in out:
            try:
                out[name] = float(row["value"])
            except ValueError:
                pass
    return out


def set_tunable(conn: sqlite3.Connection, name: str, value: float, by: str = "", token: str = "") -> None:
    if name not in _TUNABLES:
        raise E.ValidationError("a scheduling setting is one of: " + ", ".join(_TUNABLES))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise E.ValidationError("a scheduling value is a number") from None
    if number < 0 or number != number or number > 1e6:
        raise E.ValidationError("a scheduling value is a number from 0 up")
    with tx(conn):
        if by:
            _authn(conn, _op(by), token)
            _require_operator(conn, _op(by))
            _require_admin(conn, _op(by))
        elif get_setting(conn, "require_tokens") == "1":
            raise E.AuthFailed("changing a scheduling weight needs an authenticated operator")
        conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"w.{name}", repr(number)))
        _log(conn, by or "system", "schedule.set", name, repr(number))


def _account_of(row: sqlite3.Row) -> str:
    return row["account"] or f"solo-{row['id'].lower()}"


def set_usage(conn: sqlite3.Connection, account: str, percent: float, resets_at: str | None = None,
              source: str = "manual", by: str = "", reserve: float | None = None, token: str = "",
              verified_at: str | None = None) -> None:
    """Record an account's weekly utilisation as reported by an authorised source. Nothing is estimated."""
    if not _ACCOUNT.fullmatch(account or ""):
        raise E.ValidationError("an account name is letters, digits, '.', '_' or '-'")
    try:
        number = float(percent)
    except (TypeError, ValueError):
        raise E.ValidationError("utilisation is a number from 0 to 100") from None
    if not 0 <= number <= 100:
        raise E.ValidationError("utilisation is a number from 0 to 100")
    if reserve is not None and not 0 <= float(reserve) <= 100:
        raise E.ValidationError("a reserve is a percentage from 0 to 100")
    if source not in ("manual", "file"):
        raise E.ValidationError("source is manual or file")
    if resets_at:
        try:
            parsed = datetime.fromisoformat(resets_at)
        except ValueError:
            raise E.ValidationError("the reset time is an ISO date and time") from None
        resets_at = _ts(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
    with tx(conn):
        if by:
            _authn(conn, _op(by), token)
            _require_operator(conn, _op(by))
            who = conn.execute("SELECT admin, account, id FROM operators WHERE id=?", (_op(by),)).fetchone()
            own = (who["account"] or f"solo-{who['id'].lower()}") == account
            if not who["admin"] and not own:
                raise E.NotActive(f"{who['id']} may record usage only for its own account")
        elif get_setting(conn, "require_tokens") == "1":
            raise E.AuthFailed("recording usage needs an authenticated operator")
        old = conn.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
        conn.execute("INSERT INTO accounts(id, percent, resets_at, reserve, source, verified_at) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET percent=excluded.percent, resets_at=excluded.resets_at, "
                     "reserve=excluded.reserve, source=excluded.source, verified_at=excluded.verified_at",
                     (account, number, resets_at or (old["resets_at"] if old else ""),
                      float(reserve) if reserve is not None else (old["reserve"] if old else 5.0), source,
                      verified_at or _ts()))
        _log(conn, by or "system", "usage.set", account, f"{number}% source={source}")


def ingest_usage(conn: sqlite3.Connection, path: str, account: str, reset_epoch: int, by: str = "",
                 token: str = "") -> float:
    """Read the latest weekly figure for one account from a local usage history (tab separated). Rows are matched on
    the week's reset time, because one history file can hold several accounts."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 4_000_000:
            raise E.ValidationError("the usage history must be a regular file under 4 MB")
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as exc:
        raise E.NotFound(f"cannot read the usage history: {exc.__class__.__name__}") from None
    if not lines:
        raise E.NotFound("the usage history is empty")
    head = lines[0].split("\t")
    try:
        c_at, c_pct, c_reset = head.index("captured_at"), head.index("week_pct"), head.index("week_resets_at")
    except ValueError:
        raise E.ValidationError("the usage history has no captured_at, week_pct and week_resets_at columns") from None
    found = None
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) <= max(c_at, c_pct, c_reset) or not cells[c_pct]:
            continue
        if cells[c_reset] == str(reset_epoch):
            found = cells
    if found is None:
        raise E.NotFound(f"no figure for the week that resets at {reset_epoch}")
    try:
        stamp = datetime.fromtimestamp(int(found[c_at]), timezone.utc)
        reset = datetime.fromtimestamp(int(reset_epoch), timezone.utc)
        percent = float(found[c_pct])
    except (ValueError, OverflowError, OSError):
        raise E.ValidationError("the usage history row has a value that is not a number or a time") from None
    set_usage(conn, account, percent, resets_at=_ts(reset), source="file", by=by, token=token, verified_at=_ts(stamp))
    return percent


def _aware(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_usage(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[str, object]]:
    """Per account (operators sharing one account are one pool): the figure, how far to trust it, the room left."""
    moment = now or _now()
    stale_after = timedelta(hours=get_tunables(conn)["stale_hours"])
    pools: dict[str, dict[str, object]] = {}
    for o in conn.execute("SELECT id, account FROM operators ORDER BY id"):
        pools.setdefault(_account_of(o), {"operators": []})["operators"].append(o["id"])  # type: ignore[union-attr]
    for row in conn.execute("SELECT * FROM accounts"):
        pools.setdefault(row["id"], {"operators": []})
    for name, pool in pools.items():
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (name,)).fetchone()
        pool.update({"percent": None, "state": "unknown", "source": "", "verified_at": "", "resets_at": "",
                     "reserve": 5.0, "left": None})
        if row is None or row["percent"] is None:
            continue
        pool.update({"percent": row["percent"], "source": row["source"], "verified_at": row["verified_at"],
                     "resets_at": row["resets_at"], "reserve": row["reserve"], "left": round(100 - row["percent"], 2)})
        try:
            rolled = bool(row["resets_at"]) and _aware(row["resets_at"]) < moment
            old = not row["verified_at"] or moment - _aware(row["verified_at"]) > stale_after
        except (ValueError, TypeError):
            rolled, old = False, True
        pool["state"] = "reset" if rolled else ("stale" if old else "fresh")
    return pools


def choose_model(needs: str, text: str = "", explicit: str = "") -> str:
    """Local rule, no model tokens: hard reasoning and security go to opus, routine work to sonnet."""
    if explicit and explicit != "any":
        return explicit
    wanted = {x.strip() for x in (needs or "").split(",")}
    return "opus" if wanted & _HARD_MODEL else "sonnet"


def _candidates(conn: sqlite3.Connection, req: sqlite3.Row | dict[str, object], moment: datetime, model: str = "any",
                strict: bool = False, exclude: Iterable[str] = (), only: str = "",
                include_origin: bool = False) -> list[dict[str, object]]:
    w = get_tunables(conn)
    usage = get_usage(conn, moment)
    needs = {x for x in req["needs"].split(",") if x}
    quiet_cutoff = _ts(moment - timedelta(minutes=w["quiet_minutes"]))
    heard_cutoff = _ts(moment - timedelta(seconds=90))
    ranking = []
    skip = set(exclude)
    for o in conn.execute("SELECT * FROM operators WHERE status='ACTIVE' AND kind='OPERATOR' ORDER BY id"):
        if (o["id"] == req["origin"] and not include_origin) or o["id"] in skip or (only and o["id"] != only):
            continue
        if o["availability"] != "AVAILABLE" or not o["can_receive"]:
            continue
        if _approval_required(conn) and o["trust"] != "APPROVED":
            continue
        if not _scope_ok(o["scopes"], str(req["project"]) if "project" in req.keys() else "general"):  # type: ignore[union-attr]
            continue
        listening = bool(o["listening_at"]) and o["listening_at"] >= heard_cutoff
        if o["last_seen"] < quiet_cutoff and not listening:
            continue
        if model != "any" and o["model_pref"] not in ("any", model):
            continue
        open_tasks = conn.execute("SELECT count(*) FROM tasks WHERE owner=? AND status IN ('CLAIMED','IN_PROGRESS','BLOCKED')",
                                  (o["id"],)).fetchone()[0]
        if open_tasks >= o["max_tasks"]:
            continue
        have = {x for x in o["skills"].split(",") if x}
        match = sorted(needs & have)
        pool = usage[_account_of(o)]
        trusted = pool["state"] == "fresh"
        pct = float(pool["percent"]) if trusted else w["assume"]  # type: ignore[arg-type]
        near = trusted and pct >= 100 - float(pool["reserve"])  # type: ignore[arg-type]
        if strict and near:
            continue
        score = (len(match) * w["skill"] + (w["listening"] if listening else 0.0) + w["available"]
                 + w["usage"] * (100 - pct) / 10 - w["load"] * open_tasks - (w["limit"] if near else 0.0)
                 - w["fairness"] * o["routed"])
        shown = f"{pool['percent']}% ({pool['state']})" if pool["percent"] is not None else "unknown"
        ranking.append({"id": o["id"], "score": round(score, 3),
                        "reason": f"skills: {','.join(match) or 'none matching'}; account {_account_of(o)} at {shown}"
                                  f"{'; NEAR ITS LIMIT' if near else ''}; {'listening' if listening else 'not listening'}; "
                                  f"{open_tasks} open of {o['max_tasks']}; model {o['model_pref']}"})
    ranking.sort(key=lambda r: (-float(r["score"]), str(r["id"])))  # type: ignore[arg-type]
    return ranking


def suggest_assignees(conn: sqlite3.Connection, request_id: str, now: datetime | None = None) -> list[dict[str, object]]:
    """Rank the operators that could take a request: skills, weekly utilisation, load, listening and availability.
    Local and deterministic, no model tokens. It only suggests; the operator coordinating decides."""
    return _candidates(conn, get_request(conn, request_id), now or _now())


def explain_candidates(conn: sqlite3.Connection, request_id: str) -> str:
    rows = suggest_assignees(conn, request_id)
    return "\n".join(f"{r['id']:<10} score {r['score']:>7}  {r['reason']}" for r in rows) or "(no eligible operator)"


def route_request(conn: sqlite3.Connection, request_id: str, by: str, model: str = "any",
                  token: str = "") -> dict[str, object] | None:
    """Give an unowned request to the best eligible operator. Never moves work that already has an owner. The claim is
    atomic, so concurrent routers produce one assignment. Returns None, changing nothing, when nobody is eligible."""
    rid = get_request(conn, request_id)["id"]
    actor = _op(by)
    with tx(conn):
        _authn(conn, actor, token)
        _require_operator(conn, actor)
        req = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if req["status"] != "OPEN":
            raise E.NotClaimable(f"{rid} is already {req['status']}")
        task = conn.execute("SELECT owner FROM tasks WHERE id=?", (rid,)).fetchone()
        if task["owner"]:
            raise E.NotClaimable(f"{rid} is already owned by {task['owner']}; balancing never moves owned work")
        ranking = _candidates(conn, req, _now(), model)
        if not ranking:
            return None
        best = str(ranking[0]["id"])
        if not _claim(conn, rid, best, actor, "task.assign"):
            raise E.NotClaimable(f"{rid} was taken while routing")
        conn.execute("UPDATE operators SET routed = routed + 1 WHERE id=?", (best,))
        explanation = f"{best} chosen for {rid}: {ranking[0]['reason']}; score {ranking[0]['score']}"
        if len(ranking) > 1:
            explanation += f"; next {ranking[1]['id']} score {ranking[1]['score']}"
        _log(conn, actor, "schedule.route", rid, f"to={best} {explanation}"[:480])
        if best != actor:
            _notify(conn, best, "assigned", rid, f"{actor} routed {rid} to you: {req['text'][:160]}")
        return {"assignee": best, "explanation": explanation, "ranking": ranking}


def split_text(text: str) -> list[dict[str, object]]:
    """Deterministic, local: bullets are independent parts, numbered items are ordered. Fewer than two items means the
    request is simple and is not split."""
    items, ordered = [], False
    for line in text.splitlines():
        bullet = re.match(r"\s*[-*\u2022]\s+(\S.*)", line)
        number = re.match(r"\s*\d{1,2}[.)]\s+(\S.*)", line)
        if number:
            ordered = True
        match = number or bullet
        if match:
            items.append(match.group(1).strip())
    if len(items) < 2:
        return []
    return [{"title": title, "depends": [n] if ordered and n else []} for n, title in enumerate(items)]


def plan_request(conn: sqlite3.Connection, request_id: str, by: str, steps: list[dict[str, object]],
                 token: str = "") -> list[str]:
    """The operator that was asked records the plan once. Steps: title, needs, files, criteria, depends (1-based)."""
    rid = get_request(conn, request_id)["id"]
    actor = _op(by)
    if not 1 <= len(steps) <= 30:
        raise E.ValidationError("a plan has 1 to 30 steps")
    now = _ts()
    with tx(conn):
        _authn(conn, actor, token)
        _require_operator(conn, actor)
        row = conn.execute("SELECT origin, status, project FROM requests WHERE id=?", (rid,)).fetchone()
        if _held(conn, row["project"]):
            raise E.NotActive(f"the project {row['project']!r} is on hold")
        if row["origin"] != actor:
            raise E.NotOwner(f"{rid} was asked by {row['origin']}; only that operator plans it")
        if row["status"] != "OPEN":
            raise E.NotClaimable(f"{rid} is already {row['status']}")
        if conn.execute("SELECT 1 FROM tasks WHERE request_id=? AND id != ?", (rid, rid)).fetchone():
            raise E.NotClaimable(f"{rid} already has a plan")
        ids = [f"{rid}-S{n}" for n in range(1, len(steps) + 1)]
        for n, step in enumerate(steps, start=1):
            title = str(step.get("title", "")).strip()
            if not title:
                raise E.ValidationError(f"step {n} needs a title")
            deps = [int(d) for d in step.get("depends", []) or []]  # type: ignore[union-attr]
            if any(d < 1 or d >= n for d in deps):
                raise E.ValidationError(f"step {n} can only depend on earlier steps")
            files = ",".join(f.strip() for f in str(step.get("files", "") or "").split(",") if f.strip())
            conn.execute("INSERT INTO tasks(id, description, deliverables, needs, files, created_at, updated_at, request_id) "
                         "VALUES (?,?,?,?,?,?,?,?)", (ids[n - 1], title[:300], str(step.get("criteria", "") or "")[:300],
                                                      _tags(str(step.get("needs", "") or "")), files, now, now, rid))
            for d in deps:
                conn.execute("INSERT INTO task_deps VALUES (?,?)", (ids[n - 1], ids[d - 1]))
        _log(conn, actor, "request.plan", rid, f"{len(steps)} steps")
        return ids


def dispatch_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "dispatch", "on") != "paused"


def set_dispatch(conn: sqlite3.Connection, on: bool, by: str, token: str = "") -> None:
    actor = _op(by)
    with tx(conn):
        _authn(conn, actor, token)
        _require_operator(conn, actor)
        _require_admin(conn, actor)
        conn.execute("INSERT INTO settings VALUES ('dispatch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("on" if on else "paused",))
        _log(conn, actor, "dispatch.set", "dispatch", "on" if on else "paused")


def _assign_auto(conn: sqlite3.Connection, tid: str, ranking: list[dict[str, object]], actor: str, text: str) -> dict[str, object] | None:
    best = str(ranking[0]["id"])
    if not _claim(conn, tid, best, actor, "task.assign"):
        return None
    conn.execute("UPDATE operators SET routed = routed + 1 WHERE id=?", (best,))
    conn.execute("UPDATE tasks SET auto=1 WHERE id=?", (tid,))
    explanation = f"{best} chosen for {tid}: {ranking[0]['reason']}; score {ranking[0]['score']}"
    if len(ranking) > 1:
        explanation += f"; next {ranking[1]['id']} score {ranking[1]['score']}"
    _log(conn, actor, "schedule.route", tid, f"to={best} {explanation}"[:480])
    _notify(conn, best, "assigned", tid, f"{actor} dispatched {tid} to you: {text[:160]}")
    return {"task": tid, "assignee": best, "explanation": explanation}


def _reclaim_unacknowledged(conn: sqlite3.Connection, actor: str, moment: datetime) -> None:
    """An automatic assignment that was never acknowledged, whose owner has gone quiet, returns to the pool for someone
    else. Work an operator took itself, started work, or an owner that is listening are never touched."""
    cutoff = _ts(moment - timedelta(minutes=get_tunables(conn)["ack_minutes"]))
    heard = _ts(moment - timedelta(seconds=90))
    for row in list(conn.execute("SELECT t.*, o.listening_at FROM tasks t JOIN operators o ON o.id=t.owner "
                                 "WHERE t.auto=1 AND t.status='CLAIMED' AND t.claimed_at < ?", (cutoff,))):
        if row["listening_at"] and row["listening_at"] >= heard:
            continue
        tried = ",".join(dict.fromkeys([x for x in row["tried"].split(",") if x] + [row["owner"]]))
        conn.execute("UPDATE tasks SET owner=NULL, status='OPEN', claimed_at=NULL, auto=0, tried=?, updated_at=? WHERE id=?",
                     (tried, _ts(), row["id"]))
        _log(conn, actor, "task.reclaim", row["id"], f"from={row['owner']} unacknowledged")
        _notify(conn, row["owner"], "reclaimed", row["id"], f"{row['id']} was not acknowledged in time and went back to the pool")
        req = conn.execute("SELECT origin FROM requests WHERE id=?", (row["request_id"],)).fetchone() if row["request_id"] else None
        if req is not None and req["origin"] != row["owner"]:
            _notify(conn, req["origin"], "task_reclaimed", row["id"], f"{row['id']} went back to the pool: {row['owner']} did not acknowledge it")


def dispatch_tick(conn: sqlite3.Connection, by: str, token: str = "") -> list[dict[str, object]]:
    """Offer waiting work to eligible operators. Event driven (called when a request arrives, a task finishes or an
    operator becomes idle), local and deterministic, no model tokens. Never moves owned work, never picks a pending
    operator, an agent, a busy or out-of-usage one, or an account near its reserve, and every claim is atomic."""
    actor = _op(by)
    made: list[dict[str, object]] = []
    with tx(conn):
        _authn(conn, actor, token)
        _require_operator(conn, actor)
        if not dispatch_enabled(conn):
            return made
        moment = _now()
        _reclaim_unacknowledged(conn, actor, moment)
        for req in list(conn.execute("SELECT * FROM requests WHERE status='OPEN' ORDER BY created_at, id")):
            rid = req["id"]
            if _held(conn, req["project"]):
                continue
            try:
                _check_member(conn, actor, req["project"], receiving=False)
            except E.NotActive:
                continue
            subs = list(conn.execute("SELECT * FROM tasks WHERE request_id=? AND id != ? ORDER BY id", (rid, rid)))
            if not subs:
                root = conn.execute("SELECT owner FROM tasks WHERE id=?", (rid,)).fetchone()
                if root is not None and not root["owner"]:
                    ranking = _candidates(conn, {"origin": req["origin"], "needs": req["needs"], "project": req["project"]}, moment, strict=True)
                    if ranking:
                        try:
                            got = _assign_auto(conn, rid, ranking, actor, req["text"])
                        except (E.NotActive, E.AlreadyOwned, E.NotClaimable, E.DependencyNotDone):
                            got = None
                        if got:
                            made.append(got)
                continue
            for sub in subs:
                if sub["owner"] or sub["status"] != "OPEN" or sub["attempts"] >= 3:
                    continue
                undone = conn.execute("SELECT count(*) FROM task_deps d JOIN tasks t ON t.id=d.depends_on "
                                      "WHERE d.task_id=? AND t.status != 'DONE'", (sub["id"],)).fetchone()[0]
                if undone:
                    continue
                only = ""
                paths = [_norm(p) for p in sub["files"].split(",") if p]
                if paths:
                    holders = {r["owner"] for r in conn.execute(
                        f"SELECT owner FROM file_locks WHERE path IN ({','.join('?' * len(paths))})", paths)}
                    if len(holders) > 1:
                        continue
                    only = next(iter(holders), "")
                spec = {"origin": req["origin"], "needs": sub["needs"], "project": req["project"]}
                tried = [x for x in sub["tried"].split(",") if x]
                ranking = _candidates(conn, spec, moment, strict=True, exclude=tried, only=only)
                if not ranking and not only:
                    ranking = _candidates(conn, spec, moment, strict=True, exclude=tried, include_origin=True)
                if ranking:
                    try:
                        got = _assign_auto(conn, sub["id"], ranking, actor, sub["description"])
                    except (E.NotActive, E.AlreadyOwned, E.NotClaimable, E.DependencyNotDone):
                        got = None
                    if got:
                        made.append(got)
    return made


def fail_task(conn: sqlite3.Connection, task_id: str, op_id: str, reason: str, token: str = "") -> None:
    """The owner gives a task back as failed. It returns to the pool for someone else; after three tries it waits for a
    person and the asker is told."""
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    if not reason.strip():
        raise E.MissingEvidence("a failed task needs the reason")
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        row = _owned(conn, tid, op)
        if row["status"] in ("DONE", "CANCELLED"):
            raise E.NotClaimable(f"task {tid} is {row['status']}, which is final")
        tried = ",".join(dict.fromkeys([x for x in row["tried"].split(",") if x] + [op]))
        attempts = row["attempts"] + 1
        conn.execute("UPDATE tasks SET owner=NULL, status='OPEN', claimed_at=NULL, attempts=?, tried=?, updated_at=? "
                     "WHERE id=?", (attempts, tried, _ts(), tid))
        _log(conn, op, "task.fail", tid, reason.strip()[:200])
        origin = conn.execute("SELECT origin FROM requests WHERE id=?", (row["request_id"],)).fetchone() if row["request_id"] else None
        if origin is not None:
            _notify(conn, origin["origin"], "task_failed", tid, f"{op} could not do {tid} (try {attempts}): {reason.strip()[:200]}")
            if attempts >= 3:
                _notify(conn, origin["origin"], "needs_attention", tid, f"{tid} failed {attempts} times; it now waits for you")


def start_task(conn: sqlite3.Connection, task_id: str, op_id: str, token: str = "") -> None:
    """The owner acknowledges an assignment and starts work. The asker is told."""
    tid = _ident(task_id, "task id")
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        row = _owned(conn, tid, op)
        if row["status"] != "CLAIMED":
            raise E.NotClaimable(f"task {tid} is {row['status']}; only an assigned task is started")
        now = _ts()
        conn.execute("UPDATE tasks SET status='IN_PROGRESS', started_at=?, updated_at=? WHERE id=?", (now, now, tid))
        conn.execute("UPDATE notifications SET read_at=? WHERE operator=? AND subject=? AND kind='assigned' AND read_at IS NULL",
                     (now, op, tid))
        _log(conn, op, "task.start", tid, "")
        origin = conn.execute("SELECT origin FROM requests WHERE id=?", (row["request_id"],)).fetchone() if row["request_id"] else None
        if origin is not None and origin["origin"] != op:
            _notify(conn, origin["origin"], "task_started", tid, f"{op} acknowledged and started {tid}")


def set_session_name(conn: sqlite3.Connection, op_id: str, name: str, token: str = "") -> None:
    """The name this operator's Claude session shows in the session list, declared by the operator itself. It is only a
    hint for sending a message; it must be checked against the live session list before use."""
    op = _op(op_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        raise E.ValidationError("a session name is letters, digits, '.', '_' or '-' (no spaces, no addresses)")
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        conn.execute("UPDATE operators SET session_name=? WHERE id=?", (name, op))
        _log(conn, op, "operator.session", op, name)


def pending_nudges(conn: sqlite3.Connection, min_age_seconds: int = 120, now: datetime | None = None) -> list[dict[str, object]]:
    """Operators that have unread notifications but are not listening. A running session is only woken by a finished
    background command; a message from a peer session is the supported way to prompt a quiet one."""
    moment = now or _now()
    heard = _ts(moment - timedelta(seconds=90))
    old = _ts(moment - timedelta(seconds=min_age_seconds))
    out = []
    for o in conn.execute("SELECT id, session_name, listening_at FROM operators WHERE status='ACTIVE' AND kind='OPERATOR' ORDER BY id"):
        if o["listening_at"] and o["listening_at"] >= heard:
            continue
        rows = conn.execute("SELECT count(*), min(created_at) FROM notifications WHERE operator=? AND read_at IS NULL "
                            "AND created_at <= ?", (o["id"], old)).fetchone()
        if rows[0]:
            out.append({"operator": o["id"], "session": o["session_name"], "unread": rows[0], "oldest": rows[1]})
    return out


def _require_admin(conn: sqlite3.Connection, actor: str) -> None:
    row = conn.execute("SELECT admin FROM operators WHERE id=?", (actor,)).fetchone()
    if row is None or not row["admin"]:
        raise E.NotActive(f"{actor} is not an administrator; only an administrator records these decisions")


def is_bootstrapped(conn: sqlite3.Connection) -> bool:
    """True once the registry has had an active operator: from then on owner-level actions need an administrator."""
    return get_setting(conn, "bootstrapped", "0") == "1" or bool(
        conn.execute("SELECT 1 FROM operators WHERE status='ACTIVE' AND kind='OPERATOR'").fetchone())


def require_admin(conn: sqlite3.Connection, actor: str) -> None:
    _require_admin(conn, _op(actor))


def _decider(conn: sqlite3.Connection, by: str, token: str, target: str) -> str:
    """The administrator recording an approval decision about another operator. The installer's owner shortcut works only
    until the registry has had its first active operator."""
    if by == "owner":
        if get_setting(conn, "bootstrapped", "0") == "1" or conn.execute(
                "SELECT 1 FROM operators WHERE status='ACTIVE' AND kind='OPERATOR'").fetchone():
            raise E.NotActive("the owner shortcut is only for the first setup; an administrator records approval decisions")
        return "owner"
    actor = _op(by)
    _authn(conn, actor, token)
    _require_operator(conn, actor)
    _require_admin(conn, actor)
    if actor == target:
        raise E.NotOwner("nobody approves, suspends or revokes themselves")
    return actor


def _ref(text: str) -> str:
    cleaned = " ".join((text or "").split())[:300]
    if not cleaned:
        raise E.ValidationError("an approval needs a reference (the record or message that shows an administrator approved it)")
    return cleaned


def _scopes(text: str) -> str:
    items = [x.strip().lower() for x in (text or "*").split(",") if x.strip()]
    for item in items:
        if item != "*" and not _TAG.fullmatch(item):
            raise E.ValidationError(f"a project scope is lowercase letters, digits, '_' or '-', or '*': {item!r}")
    return ",".join(dict.fromkeys(items)) or "*"


def approve_operator(conn: sqlite3.Connection, op_id: str, by: str, approval_ref: str, scopes: str = "*",
                     can_assign: bool = True, can_receive: bool = True, token: str = "", admin: bool = False) -> None:
    """Record the owner's one-time approval for an operator to join the team, with its project scopes."""
    op = _op(op_id)
    ref, scope = _ref(approval_ref), _scopes(scopes)
    with tx(conn):
        actor = _decider(conn, by, token, op)
        if conn.execute("SELECT 1 FROM operators WHERE id=?", (op,)).fetchone() is None:
            raise E.NotFound(f"operator {op} is not registered")
        if admin and conn.execute("SELECT kind FROM operators WHERE id=?", (op,)).fetchone()["kind"] != "OPERATOR":
            raise E.ValidationError("a pooled agent is never an administrator")
        is_admin = 1 if (actor == "owner" and conn.execute("SELECT kind FROM operators WHERE id=?", (op,)).fetchone()["kind"]
                         == "OPERATOR") or admin else 0
        conn.execute("UPDATE operators SET trust='APPROVED', approved_by=?, approval_ref=?, approved_at=?, scopes=?, "
                     "can_assign=?, can_receive=?, admin=? WHERE id=?",
                     (actor, ref, _ts(), scope, int(can_assign), int(can_receive), is_admin, op))
        _log(conn, actor, "team.approve", op, f"scopes={scope} ref={ref[:120]}")


def suspend_work(conn: sqlite3.Connection, op_id: str, by: str, reason: str, token: str = "") -> None:
    """No new work for this operator. What it is doing, its session and its access are untouched."""
    op, why = _op(op_id), _ref(reason)
    with tx(conn):
        actor = _decider(conn, by, token, op)
        if conn.execute("UPDATE operators SET can_receive=0 WHERE id=?", (op,)).rowcount != 1:
            raise E.NotFound(f"operator {op} is not registered")
        _log(conn, actor, "team.suspend", op, why[:200])
        _notify(conn, op, "team", op, f"{actor} stopped new work for you: {why[:160]}")


def resume_work(conn: sqlite3.Connection, op_id: str, by: str, token: str = "") -> None:
    op = _op(op_id)
    with tx(conn):
        actor = _decider(conn, by, token, op)
        row = conn.execute("SELECT trust FROM operators WHERE id=?", (op,)).fetchone()
        if row is None:
            raise E.NotFound(f"operator {op} is not registered")
        if row["trust"] == "REVOKED":
            raise E.NotActive(f"{op} was revoked; reauthorize it instead")
        conn.execute("UPDATE operators SET can_receive=1 WHERE id=?", (op,))
        _log(conn, actor, "team.resume", op, "")


def revoke_operator(conn: sqlite3.Connection, op_id: str, by: str, reason: str, token: str = "") -> None:
    """Remove the operator from the team: no new work, no new access. Nothing is killed, deleted or reassigned."""
    op, why = _op(op_id), _ref(reason)
    with tx(conn):
        actor = _decider(conn, by, token, op)
        row = conn.execute("SELECT status FROM operators WHERE id=?", (op,)).fetchone()
        if row is None:
            raise E.NotFound(f"operator {op} is not registered")
        conn.execute("UPDATE operators SET trust='REVOKED', can_assign=0, can_receive=0, admin=0, "
                     "status=CASE WHEN status='ACTIVE' THEN 'SUSPENDED' ELSE status END WHERE id=?", (op,))
        _log(conn, actor, "team.revoke", op, why[:200])


def reauthorize_operator(conn: sqlite3.Connection, op_id: str, by: str, approval_ref: str, scopes: str | None = None,
                         token: str = "", admin: bool = False) -> None:
    op, ref = _op(op_id), _ref(approval_ref)
    with tx(conn):
        actor = _decider(conn, by, token, op)
        row = conn.execute("SELECT status, ack_hash, scopes, kind FROM operators WHERE id=?", (op,)).fetchone()
        if row is None:
            raise E.NotFound(f"operator {op} is not registered")
        kind = row["kind"]
        if admin and kind != "OPERATOR":
            raise E.ValidationError("a pooled agent is never an administrator")
        scope = _scopes(scopes) if scopes is not None else row["scopes"]
        status = "PENDING"
        conn.execute("UPDATE operators SET trust='APPROVED', approved_by=?, approval_ref=?, approved_at=?, scopes=?, "
                     "can_assign=1, can_receive=1, status=?, admin=? WHERE id=?",
                     (actor, ref, _ts(), scope, status, int(bool(admin) and kind == "OPERATOR"), op))
        _log(conn, actor, "team.reauthorize", op, f"scopes={scope} ref={ref[:120]}")


def set_hold(conn: sqlite3.Connection, project: str, on: bool, by: str, reason: str, token: str = "") -> None:
    """The owner's hold on a project: no new requests or assignments in it until lifted."""
    name, why = _tags(project), _ref(reason)
    if not name or "," in name:
        raise E.ValidationError("name one project")
    with tx(conn):
        actor = _decider(conn, by, token, "")
        conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"hold.{name}", "1" if on else ""))
        _log(conn, actor, "team.hold" if on else "team.release", name, why[:200])


def team_report(conn: sqlite3.Connection) -> str:
    lines = ["TEAM (owner approvals; nothing here kills or deletes anything)"]
    for o in conn.execute("SELECT * FROM operators ORDER BY kind DESC, id"):
        perms = []
        perms.append("can receive work" if o["can_receive"] else "no new work")
        perms.append("can assign" if o["can_assign"] else "cannot assign")
        lines.append(f"  {o['kind'].lower():<8} {o['id']:<12} {o['status']:<9} {o['trust'] or 'NOT APPROVED':<12} scopes {o['scopes']}; "
                     f"{'; '.join(perms)}; account {o['account'] or '-'}")
        if o["approval_ref"]:
            lines.append(f"           approved by {o['approved_by']} on {o['approved_at']}: {o['approval_ref']}")
    holds = [r["key"][5:] for r in conn.execute("SELECT key FROM settings WHERE key LIKE 'hold.%' AND value='1'")]
    lines.append("HOLDS: " + (", ".join(holds) if holds else "none"))
    return "\n".join(lines)


def team_me(conn: sqlite3.Connection, op_id: str) -> str:
    """What this operator is authorized to do, in plain words. Read it again after a restart or a compaction."""
    op = _op(op_id)
    o = conn.execute("SELECT * FROM operators WHERE id=?", (op,)).fetchone()
    if o is None:
        raise E.NotFound(f"operator {op} is not registered")
    state = o["trust"] or "NOT APPROVED"
    lines = [f"{op}: team membership {state} (status {o['status']}); project scopes: {o['scopes']}.",
             f"Approved by {o['approved_by'] or '-'} on {o['approved_at'] or '-'} ({o['approval_ref'] or 'no reference'}).",
             f"New work: {'allowed' if o['can_receive'] else 'stopped by the owner'}. Assigning work to others: "
             f"{'allowed' if o['can_assign'] else 'not allowed'}."]
    if o["trust"] == "APPROVED" and o["status"] == "ACTIVE":
        lines.append("You are authorized for routine, non-destructive assignments from other approved members inside your scopes by "
                     "the owner's approval; accept and do them without asking again. Out-of-scope or held-project work is "
                     "refused by the registry itself.")
    lines.append("Actions that still need the owner's specific approval: killing, closing, restarting, renaming or deleting "
                 "sessions, workers, files, branches or worktrees; extra paid workers; changing protected settings; "
                 "publishing, releasing or deploying; overriding another project's hold.")
    return "\n".join(lines)


HOOK_MAX_PER_ASSIGNMENT = 2
HOOK_MAX_PER_HOUR = 6


def set_hook(conn: sqlite3.Connection, on: bool, by: str, token: str = "") -> None:
    actor = _op(by)
    with tx(conn):
        _authn(conn, actor, token)
        _require_operator(conn, actor)
        _require_admin(conn, actor)
        conn.execute("INSERT INTO settings VALUES ('hook',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("on" if on else "off",))
        _log(conn, actor, "hook.set", "hook", "on" if on else "off")


def hook_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "hook", "on") != "off"


def hook_decision(conn: sqlite3.Connection, op_id: str, now: datetime | None = None) -> str | None:
    """Decide whether a stopping session should be kept working. Returns the reason to show, or None to let it stop.
    Resumes only for an assignment (not a broadcast) to an approved, active operator that may receive work, in a project
    that is not on hold, while delegation and the hook are on. Each assignment blocks at most twice and one operator at
    most six times an hour, so a session that never acts is let go. Writes only the counters."""
    op = _op(op_id)
    moment = now or _now()
    with tx(conn):
        if not hook_enabled(conn) or not dispatch_enabled(conn):
            return None
        o = conn.execute("SELECT * FROM operators WHERE id=?", (op,)).fetchone()
        if o is None or o["kind"] != "OPERATOR" or o["status"] != "ACTIVE" or not o["can_receive"]:
            return None
        if _approval_required(conn) and o["trust"] != "APPROVED":
            return None
        hour_ago = _ts(moment - timedelta(hours=1))
        used = conn.execute("SELECT COALESCE(sum(h.blocks),0) FROM hook_blocks h JOIN notifications n ON n.id=h.notification_id "
                            "WHERE n.operator=? AND h.last_at >= ?", (op, hour_ago)).fetchone()[0]
        if used >= HOOK_MAX_PER_HOUR:
            return None
        pending = []
        for n in conn.execute("SELECT n.* FROM notifications n JOIN tasks t ON t.id=n.subject WHERE n.operator=? AND n.read_at IS NULL "
                              "AND n.kind IN ('assigned','handoff') AND t.owner=n.operator AND t.status='CLAIMED' ORDER BY n.id", (op,)):
            if _held(conn, _task_project(conn, n["subject"])):
                continue
            seen = conn.execute("SELECT blocks FROM hook_blocks WHERE notification_id=?", (n["id"],)).fetchone()
            if seen is not None and seen["blocks"] >= HOOK_MAX_PER_ASSIGNMENT:
                continue
            pending.append(n)
        if not pending:
            return None
        for n in pending:
            conn.execute("INSERT INTO hook_blocks(notification_id, blocks, last_at) VALUES (?,1,?) "
                         "ON CONFLICT(notification_id) DO UPDATE SET blocks=blocks+1, last_at=excluded.last_at", (n["id"], _ts(moment)))
        return (f"Synergy: {len(pending)} assignment(s) for {op} are waiting. Run `synergy inbox list`, do the work within "
                "the request's scope, acknowledge with `synergy task start ID`, then re-arm `synergy inbox wait`. "
                "This reminder is shown at most twice per assignment.")


def send_message(conn: sqlite3.Connection, from_op: str, to: str | None, text: str, subject: str = "",
                 token: str = "") -> None:
    """A durable message to one operator, or to every other ACTIVE operator when `to` is None."""
    if not text.strip():
        raise E.ValidationError("a message needs some text")
    sender = _op(from_op)
    target = _op(to) if to else None
    with tx(conn):
        _authn(conn, sender, token)
        _require_active(conn, sender)
        if target is not None:
            _require_active(conn, target)
            t_row = conn.execute("SELECT kind, current_job, controller FROM operators WHERE id=?", (target,)).fetchone()
            if t_row["kind"] == "AGENT" and t_row["current_job"] and t_row["controller"] != sender:
                raise E.NotOwner(f"{target} is directed by {t_row['controller']} until {t_row['current_job']} finishes; "
                                 "queue a request instead of sending it instructions")
            recipients = [target]
        else:
            recipients = _others(conn, sender)
        label = (subject.strip() or "general")[:32]
        for r in recipients:
            _notify(conn, r, "message", label, f"{sender}: {text.strip()[:300]}")
        _log(conn, sender, "message.send", target or "*", label)


def handoff_request(conn: sqlite3.Connection, request_id: str, from_op: str, to_op: str, token: str = "") -> None:
    """The operator that was asked passes the final response to another ACTIVE operator. History is untouched."""
    rid = get_request(conn, request_id)["id"]
    sender, receiver = _op(from_op), _op(to_op)
    with tx(conn):
        _authn(conn, sender, token)
        _not_suspended(conn, sender)
        row = conn.execute("SELECT origin, status FROM requests WHERE id=?", (rid,)).fetchone()
        if row["origin"] != sender:
            raise E.NotOwner(f"{rid} was asked by {row['origin']}; only that operator hands it off")
        if row["status"] != "OPEN":
            raise E.NotClaimable(f"{rid} is already {row['status']}")
        _require_operator(conn, receiver)
        proj = conn.execute("SELECT project FROM requests WHERE id=?", (rid,)).fetchone()["project"]
        if _held(conn, proj):
            raise E.NotActive(f"the project {proj!r} is on hold")
        _check_member(conn, receiver, proj, receiving=True)
        conn.execute("UPDATE requests SET origin=? WHERE id=?", (receiver, rid))
        _log(conn, sender, "request.handoff", rid, f"to={receiver}")
        _notify(conn, receiver, "handoff", rid, f"{sender} handed {rid} to you; you now give the final response")


def adopt_request(conn: sqlite3.Connection, request_id: str, op_id: str, stale_minutes: int = 60, token: str = "") -> None:
    """Another ACTIVE operator takes over the final response when the operator that was asked has gone quiet."""
    rid = get_request(conn, request_id)["id"]
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        proj = conn.execute("SELECT project FROM requests WHERE id=?", (rid,)).fetchone()["project"]
        if _held(conn, proj):
            raise E.NotActive(f"the project {proj!r} is on hold")
        _check_member(conn, op, proj, receiving=True)
        row = conn.execute("SELECT origin, status FROM requests WHERE id=?", (rid,)).fetchone()
        if row["status"] != "OPEN":
            raise E.NotClaimable(f"{rid} is already {row['status']}")
        if row["origin"] == op:
            raise E.NotClaimable(f"{rid} is already yours")
        o = conn.execute("SELECT last_seen, listening_at, status FROM operators WHERE id=?", (row["origin"],)).fetchone()
        cutoff = _ts(_now() - timedelta(minutes=stale_minutes))
        quiet = o["last_seen"] < cutoff and not (o["listening_at"] and o["listening_at"] >= _ts(_now() - timedelta(seconds=90)))
        if not (quiet or o["status"] == "SUSPENDED"):
            raise E.NotClaimable(f"{row['origin']} was seen within {stale_minutes} minutes; ask it to hand {rid} off instead")
        conn.execute("UPDATE requests SET origin=? WHERE id=?", (op, rid))
        _log(conn, op, "request.adopt", rid, f"from={row['origin']}")
        _notify(conn, row["origin"], "adopted", rid, f"{op} took over the final response for {rid} because you were quiet")


def get_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id.strip().upper(),)).fetchone()
    if row is None:
        raise E.NotFound(f"request {request_id} does not exist")
    return row


def list_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM requests ORDER BY id"))


def take_request(conn: sqlite3.Connection, request_id: str, op_id: str, token: str = "") -> bool:
    """Claim the request's root task (atomic: one winner) and tell the operator who asked."""
    rid = get_request(conn, request_id)["id"]
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        taken = _claim(conn, rid, op, op, "request.take")
        origin = conn.execute("SELECT origin FROM requests WHERE id=?", (rid,)).fetchone()["origin"]
        if taken and origin != op:
            _notify(conn, origin, "claimed", rid, f"{op} took {rid} and is working on it")
        return taken


def close_request(conn: sqlite3.Connection, request_id: str, op_id: str, summary: str, token: str = "") -> None:
    rid = get_request(conn, request_id)["id"]
    op = _op(op_id)
    if not summary.strip():
        raise E.MissingEvidence("closing a request needs a short summary of the result")
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        row = conn.execute("SELECT origin, status FROM requests WHERE id=?", (rid,)).fetchone()
        if row["origin"] != op:
            raise E.NotOwner(f"{rid} was asked by {row['origin']}; only that operator closes it")
        if row["status"] != "OPEN":
            raise E.NotClaimable(f"{rid} is already {row['status']}")
        conn.execute("UPDATE requests SET status='DONE', summary=?, closed_at=? WHERE id=?", (summary.strip(), _ts(), rid))
        conn.execute("UPDATE tasks SET status='DONE', updated_at=? WHERE id=? AND status NOT IN ('DONE','CANCELLED')",
                     (_ts(), rid))
        _log(conn, op, "request.close", rid, summary.strip()[:200])
        for other in _others(conn, op):
            _notify(conn, other, "request_done", rid, f"{op} closed {rid}: {summary.strip()[:300]}")


def list_notifications(conn: sqlite3.Connection, operator: str, unread_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM notifications WHERE operator=?" + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY id"
    return list(conn.execute(sql, (_op(operator),)))


def mark_read(conn: sqlite3.Connection, operator: str, ids: Iterable[int], token: str = "") -> None:
    op = _op(operator)
    wanted = [int(i) for i in ids]
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        for nid in wanted:
            row = conn.execute("SELECT operator FROM notifications WHERE id=?", (nid,)).fetchone()
            if row is None:
                raise E.NotFound(f"notification {nid} does not exist")
            if row["operator"] != op:
                raise E.NotOwner(f"notification {nid} belongs to {row['operator']}")
        now = _ts()
        for nid in wanted:
            conn.execute("UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL", (now, nid))


def set_listening(conn: sqlite3.Connection, operator: str, token: str = "", on: bool = True) -> None:
    """Record that this operator has a waiter running (it will wake on new work). Refresh while waiting."""
    op = _op(operator)
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        conn.execute("UPDATE operators SET listening_at=?, last_seen=? WHERE id=?", (_ts() if on else "", _ts(), op))


def _stage(task: sqlite3.Row, request_status: str, moment: datetime) -> str:
    """queued, assigned, running, completed, failed or accepted."""
    status = task["status"]
    if status == "DONE":
        return "accepted" if request_status == "DONE" else "completed"
    if status == "CANCELLED":
        return "cancelled"
    if status == "BLOCKED":
        return "blocked"
    if status == "IN_PROGRESS":
        return "running"
    if status == "CLAIMED":
        try:
            mins = int((moment - datetime.fromisoformat(task["claimed_at"])).total_seconds() // 60)
        except (TypeError, ValueError):
            mins = 0
        return f"assigned (unacknowledged {mins} min)" if mins >= 1 else "assigned"
    return f"queued (try {task['attempts'] + 1}, failed before)" if task["attempts"] else "queued"


def request_report(conn: sqlite3.Connection, request_id: str, now: datetime | None = None) -> str:
    """One consolidated, read-only view of a request for the operator who asked."""
    req = get_request(conn, request_id)
    rid = req["id"]
    moment = now or _now()
    lines = [f"{rid}  {req['status']}  asked by {req['origin']} at {req['created_at']}", f'"{req["text"]}"', ""]
    root = conn.execute("SELECT * FROM tasks WHERE id=?", (rid,)).fetchone()
    subs = list(conn.execute("SELECT * FROM tasks WHERE request_id=? AND id != ? ORDER BY created_at, id", (rid, rid)))
    if root["owner"]:
        lines.append(f"Taken by {root['owner']} ({_stage(root, req['status'], moment)}).")
    elif subs:
        done = sum(1 for s in subs if s["status"] == "DONE")
        lines.append(f"Split into {len(subs)} parts; {done} completed. You review the results and close the request.")
    else:
        lines.append("Status: nobody has taken it yet." if req["status"] == "OPEN" else "Closed without being taken.")
    lines.append("Subtasks:" if subs else "Subtasks: none yet.")
    for s in subs:
        evidence = f"  evidence: {s['completion_evidence']}" if s["completion_evidence"] else ""
        stage = _stage(s, req["status"], moment)
        lines.append(f"  {s['id']:<12} {stage:<30} {s['owner'] or '-':<8} {s['description'][:50]}{evidence}")
    lines.append("Operators:")
    related = [rid] + [s["id"] for s in subs]
    for o in conn.execute("SELECT id, listening_at, last_seen FROM operators WHERE status='ACTIVE' ORDER BY id"):
        unread = conn.execute(
            f"SELECT count(*) FROM notifications WHERE operator=? AND read_at IS NULL AND subject IN ({','.join('?' * len(related))})",
            (o["id"], *related)).fetchone()[0]
        try:
            heard = (moment - datetime.fromisoformat(o["listening_at"])).total_seconds() if o["listening_at"] else None
        except ValueError:
            heard = None
        if heard is not None and heard < 90:
            state = f"listening (waiter alive, {int(heard)} s ago)"
        else:
            state = "not listening (it will see this when its session next checks its inbox)"
        lines.append(f"  {o['id']:<8} {state}; {unread} unread")
    if req["summary"]:
        lines.append(f"Closing summary: {req['summary']}")
    return "\n".join(lines)


def _model_ok(job_model: str, agent_pref: str) -> bool:
    return job_model == "any" or agent_pref == "any" or job_model == agent_pref


def _fits(agent: sqlite3.Row, needs: str, model: str) -> bool:
    have = {x for x in agent["skills"].split(",") if x}
    return {x for x in needs.split(",") if x} <= have and _model_ok(model, agent["model_pref"])


def _free(agent: sqlite3.Row) -> bool:
    return (agent["status"] == "ACTIVE" and agent["availability"] == "AVAILABLE" and not agent["current_job"]
            and bool(agent["can_receive"]))


def _reserve(conn: sqlite3.Connection, job_id: str, agent_id: str, requester: str) -> bool:
    """Atomically give an agent exactly one job. The WHERE clause makes a second reservation impossible."""
    pair = conn.execute("SELECT a.scopes AS scopes, j.project AS project FROM operators a, jobs j WHERE a.id=? AND j.id=?",
                        (agent_id, job_id)).fetchone()
    if pair is not None and not _scope_ok(pair["scopes"], pair["project"]):
        return False
    got = conn.execute("UPDATE operators SET current_job=?, controller=? WHERE id=? AND current_job='' AND status='ACTIVE' "
                       "AND kind='AGENT' AND availability='AVAILABLE' AND can_receive=1 "
                       "AND (trust='APPROVED' OR COALESCE((SELECT value FROM settings WHERE key='require_approval'),'0')!='1')", (job_id, requester, agent_id)).rowcount == 1
    if got:
        conn.execute("UPDATE jobs SET status='RESERVED', agent_id=? WHERE id=?", (agent_id, job_id))
        job = conn.execute("SELECT text, requester FROM jobs WHERE id=?", (job_id,)).fetchone()
        _notify(conn, agent_id, "job", job_id, f"{job['requester']} asks: {job['text'][:300]}")
    return got


def _promote(conn: sqlite3.Connection, agent_id: str) -> None:
    """When an agent is free, give it the best waiting job it can do: priority first, then oldest."""
    agent = conn.execute("SELECT * FROM operators WHERE id=?", (agent_id,)).fetchone()
    if agent is None or not _free(agent):
        return
    for job in conn.execute("SELECT * FROM jobs WHERE status='QUEUED' AND (agent_id=? OR agent_id='') "
                            "ORDER BY priority, created_at, id", (agent_id,)):
        if _held(conn, job["project"]):
            continue
        if job["agent_id"] == agent_id or _fits(agent, job["needs"], job["model"]):
            if _reserve(conn, job["id"], agent_id, job["requester"]):
                return


def request_agent(conn: sqlite3.Connection, requester: str, text: str, needs: str = "", agent_id: str = "",
                  priority: int = 2, model: str = "any", request_id: str = "", task_id: str = "",
                  token: str = "") -> dict[str, object]:
    """Ask the shared pool for a specialist. Reuses an existing agent: a free fitting one is reserved atomically,
    otherwise the request waits in a priority queue. Nothing is ever launched and no busy agent is overwritten."""
    if not text.strip():
        raise E.ValidationError("a job needs some text")
    if priority not in (1, 2, 3):
        raise E.ValidationError("priority is 1 (urgent), 2 (normal) or 3 (low)")
    if not _MODEL.fullmatch(model):
        raise E.ValidationError("a model is any, sonnet, opus, haiku or a full model id")
    needs = _tags(needs)
    op = _op(requester)
    wanted = _op(agent_id) if agent_id else ""
    now = _ts()
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        project = "general"
        if request_id:
            prow = conn.execute("SELECT project FROM requests WHERE id=?", (request_id,)).fetchone()
            if prow is None:
                raise E.NotFound(f"request {request_id} does not exist")
            project = prow["project"] or "general"
        elif task_id:
            project = _task_project(conn, _ident(task_id, "task id"))
        if _held(conn, project):
            raise E.NotActive(f"the project {project!r} is on hold; no new jobs")
        _check_member(conn, op, project, receiving=False)
        chosen = ""
        if wanted:
            row = conn.execute("SELECT * FROM operators WHERE id=? AND kind='AGENT'", (wanted,)).fetchone()
            if row is None:
                raise E.NotFound(f"agent {wanted} is not registered")
            if row["status"] != "ACTIVE":
                raise E.NotActive(f"agent {wanted} is registered but not attached: its session has not acknowledged its "
                                  "packet, so it cannot take work; its owner must attach it")
            if row["availability"] == "OUT_OF_USAGE":
                raise E.NotActive(f"agent {wanted} is out of usage")
            chosen = wanted
        number = (conn.execute("SELECT max(CAST(substr(id, 5) AS INTEGER)) FROM jobs").fetchone()[0] or 0) + 1
        jid = f"JOB-{number:04d}"
        conn.execute("INSERT INTO jobs(id, text, requester, agent_id, needs, model, priority, request_id, task_id, created_at, project) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (jid, text.strip(), op, chosen, needs, model, priority, request_id,
                                                        task_id, now, project))
        _log(conn, op, "job.create", jid, text.strip()[:200])
        reserved = False
        if chosen:
            reserved = _reserve(conn, jid, chosen, op)
        else:
            candidates = [a for a in conn.execute("SELECT * FROM operators WHERE kind='AGENT' ORDER BY id")
                          if _free(a) and _fits(a, needs, model)]
            usage = get_usage(conn)
            assume = get_tunables(conn)["assume"]

            def _rank(a: sqlite3.Row) -> tuple[object, ...]:
                pool = usage[_account_of(a)]
                trusted = pool["state"] == "fresh"
                pct = float(pool["percent"]) if trusted else assume  # type: ignore[arg-type]
                near = trusted and pct >= 100 - float(pool["reserve"])  # type: ignore[arg-type]
                heard = bool(a["listening_at"] and a["listening_at"] >= _ts(_now() - timedelta(seconds=90)))
                match = len({x for x in needs.split(",") if x} & {x for x in a["skills"].split(",") if x})
                return (near, -match, round(pct, 1), not heard, a["id"])

            candidates.sort(key=_rank)
            for a in candidates:
                if _reserve(conn, jid, a["id"], op):
                    reserved = True
                    break
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        ahead = 0 if reserved else conn.execute(
            "SELECT count(*) FROM jobs WHERE status='QUEUED' AND id != ? AND (priority < ? OR (priority = ? AND created_at <= ?))",
            (jid, priority, priority, now)).fetchone()[0]
        return {**dict(job), "position": ahead + 1 if not reserved else 0}


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id.strip().upper(),)).fetchone()
    if row is None:
        raise E.NotFound(f"job {job_id} does not exist")
    return row


def list_jobs(conn: sqlite3.Connection, agent: str = "", requester: str = "", status: str = "") -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    for column, value in (("agent_id", agent), ("requester", requester), ("status", status)):
        if value:
            sql += f" AND {column}=?"
            args.append(value.strip().upper())
    return list(conn.execute(sql + " ORDER BY id", args))


def agent_next(conn: sqlite3.Connection, agent_id: str, token: str = "") -> sqlite3.Row | None:
    """The agent starts its reserved job (or picks up the best waiting one). None when there is nothing for it."""
    agent = _op(agent_id)
    with tx(conn):
        _authn(conn, agent, token)
        row = _require_active(conn, agent)
        conn.execute("UPDATE operators SET last_seen=? WHERE id=?", (_ts(), row))
        _promote(conn, agent)
        job = conn.execute("SELECT * FROM jobs WHERE agent_id=? AND status IN ('RESERVED','RUNNING') ORDER BY id LIMIT 1",
                           (agent,)).fetchone()
        if job is None:
            return None
        if job["status"] == "RESERVED":
            conn.execute("UPDATE jobs SET status='RUNNING', started_at=? WHERE id=?", (_ts(), job["id"]))
            _log(conn, agent, "job.start", job["id"], "")
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()


def _end_job(conn: sqlite3.Connection, job_id: str, agent: str, status: str, text: str, kind: str) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise E.NotFound(f"job {job_id} does not exist")
    if job["agent_id"] != agent or job["status"] not in ("RESERVED", "RUNNING"):
        raise E.NotOwner(f"{job_id} is {job['status']} and assigned to {job['agent_id'] or 'nobody'}, not {agent}")
    conn.execute("UPDATE jobs SET status=?, result=?, finished_at=? WHERE id=?", (status, text.strip(), _ts(), job_id))
    conn.execute("UPDATE operators SET current_job='', controller='' WHERE id=?", (agent,))
    _log(conn, agent, f"job.{status.lower()}", job_id, text.strip()[:200])
    _notify(conn, job["requester"], kind, job_id, f"{agent} {status.lower()} {job_id}: {text.strip()[:300]}")
    _promote(conn, agent)


def finish_job(conn: sqlite3.Connection, job_id: str, agent_id: str, result: str, token: str = "") -> None:
    if not result.strip():
        raise E.MissingEvidence("finishing a job needs the result (what was done and where to find it)")
    agent = _op(agent_id)
    with tx(conn):
        _authn(conn, agent, token)
        _require_active(conn, agent)
        _end_job(conn, job_id.strip().upper(), agent, "DONE", result, "job_done")


def fail_job(conn: sqlite3.Connection, job_id: str, agent_id: str, reason: str, token: str = "") -> None:
    if not reason.strip():
        raise E.MissingEvidence("failing a job needs the reason")
    agent = _op(agent_id)
    with tx(conn):
        _authn(conn, agent, token)
        _require_active(conn, agent)
        _end_job(conn, job_id.strip().upper(), agent, "FAILED", reason, "job_failed")


def cancel_job(conn: sqlite3.Connection, job_id: str, op_id: str, token: str = "") -> None:
    """The requester withdraws a job that has not started."""
    op, jid = _op(op_id), job_id.strip().upper()
    with tx(conn):
        _authn(conn, op, token)
        _not_suspended(conn, op)
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if job is None:
            raise E.NotFound(f"job {jid} does not exist")
        if job["requester"] != op:
            raise E.NotOwner(f"{jid} belongs to {job['requester']}")
        if job["status"] not in ("QUEUED", "RESERVED"):
            raise E.NotClaimable(f"{jid} is {job['status']}; a running job is finished or failed by its agent")
        conn.execute("UPDATE jobs SET status='CANCELLED', finished_at=? WHERE id=?", (_ts(), jid))
        _log(conn, op, "job.cancel", jid, "")
        if job["status"] == "RESERVED":
            conn.execute("UPDATE operators SET current_job='', controller='' WHERE id=?", (job["agent_id"],))
            _notify(conn, job["agent_id"], "job_cancelled", jid, f"{op} cancelled {jid}")
            _promote(conn, job["agent_id"])


def requeue_job(conn: sqlite3.Connection, job_id: str, op_id: str, stale_minutes: int = 60, token: str = "") -> None:
    """Recover a job from an agent that has gone quiet: back to the shared queue, history kept. The quiet agent can no
    longer finish it."""
    op, jid = _op(op_id), job_id.strip().upper()
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if job is None:
            raise E.NotFound(f"job {jid} does not exist")
        if job["requester"] != op and not conn.execute("SELECT admin FROM operators WHERE id=?", (op,)).fetchone()["admin"]:
            raise E.NotOwner(f"{jid} belongs to {job['requester']}; only that operator or an administrator requeues it")
        if job["status"] not in ("RESERVED", "RUNNING"):
            raise E.NotClaimable(f"{jid} is {job['status']}; only a reserved or running job is requeued")
        agent = conn.execute("SELECT last_seen, listening_at FROM operators WHERE id=?", (job["agent_id"],)).fetchone()
        listening = agent["listening_at"] and agent["listening_at"] >= _ts(_now() - timedelta(seconds=90))
        if agent["last_seen"] >= _ts(_now() - timedelta(minutes=stale_minutes)) or listening:
            raise E.NotClaimable(f"{job['agent_id']} was seen within {stale_minutes} minutes; it may still be working")
        conn.execute("UPDATE jobs SET status='QUEUED', agent_id='' WHERE id=?", (jid,))
        conn.execute("UPDATE operators SET current_job='', controller='' WHERE id=?", (job["agent_id"],))
        _log(conn, op, "job.requeue", jid, f"from={job['agent_id']}")
        _notify(conn, job["requester"], "job_requeued", jid, f"{jid} went back to the queue because {job['agent_id']} was quiet")


def handoff_job(conn: sqlite3.Connection, job_id: str, from_op: str, to_op: str, token: str = "") -> None:
    """The controller passes control of a job (and the agent working on it) to another operator."""
    sender, receiver, jid = _op(from_op), _op(to_op), job_id.strip().upper()
    with tx(conn):
        _authn(conn, sender, token)
        _not_suspended(conn, sender)
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if job is None:
            raise E.NotFound(f"job {jid} does not exist")
        if job["requester"] != sender:
            raise E.NotOwner(f"{jid} is directed by {job['requester']}")
        if job["status"] not in ("QUEUED", "RESERVED", "RUNNING"):
            raise E.NotClaimable(f"{jid} is {job['status']}")
        _require_operator(conn, receiver)
        if _held(conn, job["project"]):
            raise E.NotActive(f"the project {job['project']!r} is on hold")
        _check_member(conn, receiver, job["project"], receiving=True)
        conn.execute("UPDATE jobs SET requester=? WHERE id=?", (receiver, jid))
        if job["agent_id"]:
            conn.execute("UPDATE operators SET controller=? WHERE id=? AND current_job=?", (receiver, job["agent_id"], jid))
        _log(conn, sender, "job.handoff", jid, f"to={receiver}")
        _notify(conn, receiver, "handoff", jid, f"{sender} handed {jid} to you; you now direct it")


def pool_report(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """One read-only board of every agent, what it is doing, and what waits."""
    moment = now or _now()
    lines = ["AGENTS"]
    agents = list(conn.execute("SELECT * FROM operators WHERE kind='AGENT' ORDER BY id"))
    for a in agents:
        if a["status"] != "ACTIVE":
            state = "not attached (registered; its session has not acknowledged its packet)"
        elif a["availability"] == "OUT_OF_USAGE":
            state = "OUT_OF_USAGE"
        elif a["current_job"]:
            state = f"BUSY on {a['current_job']} (directed by {a['controller']})"
        elif a["availability"] == "BUSY":
            state = "do not disturb"
        else:
            state = "AVAILABLE"
        try:
            heard = (moment - datetime.fromisoformat(a["listening_at"])).total_seconds() if a["listening_at"] else None
        except ValueError:
            heard = None
        listening = "listening" if heard is not None and heard < 90 else "not listening"
        lines.append(f"  {a['id']:<12} {state}; skills {a['skills'] or '-'}; model {a['model_pref']}; {listening}; {a['role'] or '-'}")
    if not agents:
        lines.append("  (no agents registered)")
    lines.append("QUEUE")
    waiting = list(conn.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY priority, created_at, id"))
    for j in waiting:
        target = j["agent_id"] or f"any agent with {j['needs'] or 'no particular skill'}"
        lines.append(f"  {j['id']}  queued  priority {j['priority']}  from {j['requester']}  for {target}: {j['text'][:60]}")
    if not waiting:
        lines.append("  (nothing queued)")
    return "\n".join(lines)


def list_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM tasks ORDER BY created_at, id"))


def _norm(path: str) -> str:
    if not path.strip():
        raise E.ValidationError("a path is required")
    return os.path.realpath(os.path.expanduser(path))


def _covers(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def claim_file(
    conn: sqlite3.Connection, path: str, op_id: str, *, task_id: str = "", kind: str = "file", token: str = ""
) -> bool:
    """Exclusive ownership of a file or a whole worktree. This records intent; it does not stop a shell."""
    if kind not in ("file", "worktree"):
        raise E.ValidationError("kind must be 'file' or 'worktree'")
    target = _norm(path)
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_active(conn, op)
        for row in conn.execute("SELECT path, owner, kind FROM file_locks"):
            held = row["path"]
            clash = target == held or (row["kind"] == "worktree" and _covers(held, target)) or (
                kind == "worktree" and _covers(target, held)
            )
            if clash:
                if row["owner"] == op and target == held:
                    return False
                raise E.FileOwned(target, row["owner"], held)
        conn.execute("INSERT INTO file_locks VALUES (?,?,?,?,?)", (target, op, task_id, kind, _ts()))
        _log(conn, op, "file.claim", target, kind)
        return True


def release_file(conn: sqlite3.Connection, path: str, op_id: str, token: str = "") -> None:
    target = _norm(path)
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        row = conn.execute("SELECT owner FROM file_locks WHERE path=?", (target,)).fetchone()
        if row is None:
            raise E.NotFound(f"no claim on {target}")
        if row["owner"] != op:
            raise E.NotOwner(f"{target} is claimed by {row['owner']}, not {op}")
        conn.execute("DELETE FROM file_locks WHERE path=?", (target,))
        _log(conn, op, "file.release", target, "")


def list_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM file_locks ORDER BY path"))


def register_worker(
    conn: sqlite3.Connection, worker_id: str, op_id: str, *, task_id: str = "", window: str = "",
    directory: str = "", token: str = "",
) -> None:
    wid = _ident(worker_id, "worker id")
    op = _op(op_id)
    with tx(conn):
        _authn(conn, op, token)
        _require_operator(conn, op)
        if conn.execute("SELECT 1 FROM workers WHERE id=?", (wid,)).fetchone():
            raise E.WorkerExists(f"worker {wid} already exists")
        if task_id:
            _owned(conn, task_id, op)
        limit = int(get_setting(conn, "max_workers", "2"))
        live = conn.execute("SELECT count(*) FROM workers WHERE state != 'FINISHED'").fetchone()[0]
        if live >= limit:
            raise E.WorkerLimit(f"{live} workers are active; the limit is {limit} (only the owner raises it)")
        now = _ts()
        conn.execute("INSERT INTO workers VALUES (?,?,?,?,?,?,?,?)", (wid, op, task_id, window, directory, "RUNNING", now, now))
        _log(conn, op, "worker.register", wid, f"window={window}")


def set_worker_state(conn: sqlite3.Connection, worker_id: str, state: str, by: str = "", token: str = "",
                     evidence: str = "") -> None:
    wid = _ident(worker_id, "worker id")
    state = state.strip().upper()
    if state not in WORKER_STATES:
        raise E.ValidationError("state must be one of " + ", ".join(WORKER_STATES))
    if state == "FINISHED" and not evidence.strip():
        raise E.MissingEvidence("FINISHED needs evidence that the deliverables were checked (--evidence)")
    with tx(conn):
        row = conn.execute("SELECT state, operator FROM workers WHERE id=?", (wid,)).fetchone()
        if row is None:
            raise E.NotFound(f"worker {wid} does not exist")
        if by or get_setting(conn, "require_tokens") == "1":
            actor = _op(by)
            _authn(conn, actor, token)
            if row["operator"] != actor:
                raise E.NotOwner(f"worker {wid} belongs to {row['operator']}, not {actor}")
        if row["state"] == "FINISHED" and state != "FINISHED":
            raise E.ValidationError(f"worker {wid} is FINISHED, which is final: register a new worker instead")
        conn.execute("UPDATE workers SET state=?, last_seen=? WHERE id=?", (state, _ts(), wid))
        _log(conn, "system", "worker.state", wid, f"{state} {evidence.strip()[:200]}".strip())


def list_workers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM workers ORDER BY created_at, id"))


def describe_operator(conn: sqlite3.Connection, o: sqlite3.Row, moment: datetime, offline_minutes: int = 60) -> str:
    """One line a person can read: is it connected, what is it doing, which tasks, when was it last active."""
    try:
        age = int((moment - datetime.fromisoformat(o["last_seen"])).total_seconds() // 60)
    except ValueError:
        age = -1
    listening = bool(o["listening_at"]) and o["listening_at"] >= _ts(moment - timedelta(seconds=90))
    if o["status"] != "ACTIVE":
        link, work = "not onboarded", o["status"]
    else:
        link = "connected" if listening else ("recent" if 0 <= age < offline_minutes else "offline")
        open_rows = list(conn.execute("SELECT id, status FROM tasks WHERE owner=? AND status IN "
                                      "('CLAIMED','IN_PROGRESS','BLOCKED') ORDER BY id", (o["id"],)))
        if o["availability"] == "OUT_OF_USAGE":
            work = "OUT_OF_USAGE"
        elif any(r["status"] == "BLOCKED" for r in open_rows):
            work = "BLOCKED"
        elif open_rows or o["current_job"] or o["availability"] == "BUSY":
            work = "BUSY"
        else:
            work = "AVAILABLE"
    ids = [r["id"] for r in conn.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('CLAIMED','IN_PROGRESS','BLOCKED') "
                                         "ORDER BY id", (o["id"],))]
    if o["current_job"]:
        ids.append(o["current_job"])
    kind = "agent " if o["kind"] == "AGENT" else ""
    return (f"{kind}{o['id']:<10} {o['status']:<9} {link:<13} {work:<12} tasks {','.join(ids) or '-':<16} "
            f"last activity {o['last_seen']} ({age} min ago)  {o['role'] or ''}".rstrip())


def status_report(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """Read-only text report. Writes nothing, including no event."""
    moment = now or _now()
    if get_setting(conn, "require_tokens") == "1":
        lines = ["tokens: required"]
    else:
        lines = ["tokens: NOT required (run `synergy operator token backfill` to require them)"]
    lines.append("OPERATORS (connection, work state, open tasks, last activity)")
    for o in list_operators(conn):
        lines.append("  " + describe_operator(conn, o, moment))
    if len(lines) == 2:
        lines.append("  (none)")
    lines.append("TASKS")
    tasks = list_tasks(conn)
    for t in tasks:
        lines.append(f"  {t['id']:<12} {t['status']:<12} {t['owner'] or '-':<10} {t['description'][:60]}")
    if not tasks:
        lines.append("  (none)")
    lines.append("USAGE (weekly, per Claude account; accounts shared by several operators count once)")
    for name, pool in sorted(get_usage(conn, moment).items()):
        load = conn.execute("SELECT count(*) FROM tasks WHERE status IN ('CLAIMED','IN_PROGRESS','BLOCKED') AND owner IN "
                            f"({','.join('?' * len(pool['operators']))})", pool["operators"]).fetchone()[0]  # type: ignore[arg-type]
        figure = "unknown (no verified figure)" if pool["percent"] is None else (
            f"{pool['percent']}% used, {pool['left']}% left, {pool['state']} ({pool['source']}, verified {pool['verified_at'] or '-'})")
        lines.append(f"  {name:<14} {figure}; operators {','.join(pool['operators'])}; {load} open task(s)")  # type: ignore[arg-type]
    lines.append("FILE AND WORKTREE CLAIMS")
    files = list_files(conn)
    for f in files:
        lines.append(f"  {f['owner']:<10} {f['kind']:<9} {f['path']}")
    if not files:
        lines.append("  (none)")
    lines.append("WORKERS")
    workers = list_workers(conn)
    for w in workers:
        lines.append(f"  {w['id']:<12} {w['state']:<9} {w['operator']:<10} {w['window'] or '-'}")
    if not workers:
        lines.append("  (none)")
    live = sum(1 for w in workers if w["state"] != "FINISHED")
    lines.append(f"  active workers {live} of limit {get_setting(conn, 'max_workers', '2')}")
    ok, msg = verify_chain(conn)
    lines.append(f"EVENT LOG  {'ok' if ok else 'PROBLEM'}: {msg}")
    return "\n".join(lines)
