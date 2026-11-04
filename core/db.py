"""ARES CODE — SQLite-хранилище: пользователи, архивы, журнал расхода токенов, kv."""
import sqlite3
import threading
import time

from core import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect():
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
          tg_id INTEGER PRIMARY KEY,
          name TEXT,
          approved INTEGER DEFAULT 0,
          approved_at REAL,
          requested_at REAL
        );
        CREATE TABLE IF NOT EXISTS archives(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          seq INTEGER,
          project TEXT,
          path TEXT,
          sha256 TEXT,
          size INTEGER,
          created_at REAL,
          mailbox TEXT,
          message_id TEXT,
          cold INTEGER DEFAULT 0,
          parts INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL, day TEXT,
          provider TEXT, model TEXT,
          prompt INTEGER, completion INTEGER, total INTEGER,
          kind TEXT, latency_ms INTEGER, ok INTEGER
        );
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE INDEX IF NOT EXISTS idx_ledger_day ON ledger(day);
        CREATE INDEX IF NOT EXISTS idx_archives_project ON archives(project);
        """
    )
    _conn.commit()
    return _conn


def q(sql, params=(), one=False):
    assert _conn is not None, "db.connect() не вызван"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, params=()):
    assert _conn is not None, "db.connect() не вызван"
    with _lock:
        _conn.execute(sql, params)
        _conn.commit()


# ---------------- пользователи / доступ ----------------

def upsert_user(tg_id: int, name: str):
    ex("INSERT INTO users(tg_id,name) VALUES(?,?) ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
       (tg_id, name))


def is_owner(tg_id: int) -> bool:
    return int(tg_id) in config.OWNER_TELEGRAM_IDS


def is_approved(tg_id: int) -> bool:
    r = q("SELECT approved FROM users WHERE tg_id=?", (tg_id,), one=True)
    return bool(r and r["approved"])


def set_approved(tg_id: int, flag: bool):
    ex("INSERT INTO users(tg_id,approved,approved_at) VALUES(?,?,?) "
       "ON CONFLICT(tg_id) DO UPDATE SET approved=excluded.approved, approved_at=excluded.approved_at",
       (tg_id, int(flag), time.time() if flag else None))


def mark_requested(tg_id: int):
    ex("UPDATE users SET requested_at=? WHERE tg_id=?", (time.time(), tg_id))


def get_user(tg_id: int):
    return q("SELECT * FROM users WHERE tg_id=?", (tg_id,), one=True)


# ---------------- архивы ----------------

def next_seq() -> int:
    r = q("SELECT COALESCE(MAX(seq),0)+1 AS n FROM archives", one=True)
    return r["n"]


def add_archive(project: str, path: str, sha: str, size: int, parts: int = 1):
    seq = next_seq()
    ex("INSERT INTO archives(seq,project,path,sha256,size,created_at,parts) VALUES(?,?,?,?,?,?,?)",
       (seq, project, path, sha, size, time.time(), parts))
    return get_archive_by_seq(seq)


def get_archive_by_seq(seq: int):
    return q("SELECT * FROM archives WHERE seq=?", (seq,), one=True)


def get_archive(arch_id: int):
    return q("SELECT * FROM archives WHERE id=?", (arch_id,), one=True)


def list_archives(limit: int = 50):
    return q("SELECT * FROM archives ORDER BY id DESC LIMIT ?", (limit,))


def latest_local_archive(project: str):
    return q("SELECT * FROM archives WHERE project=? AND cold=0 ORDER BY id DESC LIMIT 1",
             (project,), one=True)


def set_cold(arch_id: int, mailbox: str, subject: str, parts: int):
    ex("UPDATE archives SET cold=1, mailbox=?, message_id=?, parts=? WHERE id=?",
       (mailbox, subject, parts, arch_id))


# ---------------- журнал расхода ----------------

def add_usage(provider: str, model: str, prompt: int, completion: int,
              kind: str, latency_ms: int, ok: bool):
    day = time.strftime("%Y-%m-%d")
    ex("INSERT INTO ledger(ts,day,provider,model,prompt,completion,total,kind,latency_ms,ok) "
       "VALUES(?,?,?,?,?,?,?,?,?,?)",
       (time.time(), day, provider, model, prompt or 0, completion or 0,
        (prompt or 0) + (completion or 0), kind, latency_ms, int(bool(ok))))


def day_totals(day: str | None = None):
    day = day or time.strftime("%Y-%m-%d")
    r = q("SELECT COALESCE(SUM(total),0) t, COUNT(*) n FROM ledger WHERE day=? AND ok=1",
          (day,), one=True)
    return int(r["t"]), int(r["n"])


def errors_today(day: str | None = None) -> int:
    day = day or time.strftime("%Y-%m-%d")
    r = q("SELECT COUNT(*) n FROM ledger WHERE day=? AND ok=0", (day,), one=True)
    return int(r["n"])


# ---------------- kv ----------------

def kv_set(k: str, v: str):
    ex("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def kv_get(k: str, d=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return r["v"] if r else d
