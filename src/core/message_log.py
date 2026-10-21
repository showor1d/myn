"""Immutable local input journal and restartable memory-processing queue."""

import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import backup
import storage
import conversation_log


class SensitiveInputError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def is_sensitive(text):
    """Reject likely credentials before they reach the journal or a model."""
    # Prefixes must start a token: "risk-assessment" or "task-management" are words, not
    # Use a lookbehind rather than a word boundary so keys adjacent to Chinese text are detected.
    return bool(re.search(
        r"(?<![A-Za-z0-9])(?:"
        r"sk[-_][A-Za-z0-9_-]{12,}|"              # Anthropic / OpenAI sk-…, ElevenLabs sk_…
        r"AKIA[A-Z0-9]{12,}|"                      # AWS access key id
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"AIza[A-Za-z0-9_-]{30,}|"                 # Google API key
        r"xox[abprs]-[A-Za-z0-9-]{10,}|"           # Slack token
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})|"  # JWT
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"\bkey\s+[A-Za-z0-9_-]{16,}\b|"
        r"(?:api[ _-]?key|password|passwd|access[ _-]?token|密码|口令|密钥|令牌)"
        r"\s*(?:[:：=]|是|为|\bis\b)\s*\S{3,}", text, re.I))


def ensure_schema(db_path):
    db_path = Path(db_path)
    storage.init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_messages'").fetchone()
        if exists:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sent_messages)")}
    if exists:
        if not {"channel", "scene_id"} <= columns:
            # 0.20: where a line came from, and which analysed scene it belongs to.
            backup.backup_database(db_path, label="pre-scene-columns")
            with closing(sqlite3.connect(str(db_path))) as connection:
                with connection:
                    if "channel" not in columns:
                        connection.execute("ALTER TABLE sent_messages ADD COLUMN channel TEXT")
                    if "scene_id" not in columns:
                        connection.execute("ALTER TABLE sent_messages ADD COLUMN scene_id INTEGER")
        conversation_log.ensure_schema(db_path)
        return
    # Keep a separately verified copy before the first journal migration.
    backup.backup_database(db_path, label="pre-message-journal")
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('utterance','command')),
                status TEXT NOT NULL CHECK(status IN ('pending','processing','processed','skipped')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                lease_at TEXT,
                lease_token TEXT,
                processed_at TEXT,
                derived_memory_id INTEGER,
                last_error_code TEXT,
                channel TEXT,
                scene_id INTEGER
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_sent_messages_queue "
                               "ON sent_messages(status,next_attempt_at,id)")
    conversation_log.ensure_schema(db_path)


def record(db_path, raw_text, kind="utterance", session_id=None, channel="typed"):
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError('Message cannot be empty.')
    if is_sensitive(raw_text):
        raise SensitiveInputError('Possible key or password detected; nothing was sent or saved. Use the dedicated secure settings window.')
    if kind not in ("utterance", "command"):
        raise ValueError("Invalid message kind")
    if channel not in ("typed", "heard"):
        raise ValueError("Invalid message channel")
    ensure_schema(db_path)
    stamp = _now()
    status = "skipped" if kind == "command" else "pending"
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO sent_messages(raw_text,sent_at,kind,status,processed_at,channel) "
                "VALUES(?,?,?,?,?,?)",
                (raw_text, stamp, kind, status, stamp if kind == "command" else None, channel))
            conversation_log._insert_user(connection, raw_text, stamp, session_id,
                                          cursor.lastrowid, 'ambient-unconfirmed' if channel=='heard' else 'current')
            return cursor.lastrowid


def get(db_path, message_id):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM sent_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None


def list_messages(db_path, period="recent", limit=20):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        query = ("SELECT id,raw_text,sent_at,kind,status,derived_memory_id "
                 "FROM sent_messages WHERE kind='utterance' ORDER BY id DESC")
        params = ()
        if period == "recent":
            query += " LIMIT ?"
            params = (limit,)
        rows = [dict(row) for row in connection.execute(
            query, params)]
    if period == "recent":
        return rows
    now = datetime.now().astimezone()
    def matches(row):
        when = datetime.fromisoformat(row["sent_at"]).astimezone()
        if period == "today":
            return when.date() == now.date()
        if period == "week":
            return when.date().isocalendar()[:2] == now.date().isocalendar()[:2]
        if period == "month":
            return (when.year, when.month) == (now.year, now.month)
        raise ValueError("Invalid period")
    return [row for row in rows if matches(row)][:limit]


def search_messages(db_path, query, limit=6, exclude_id=None):
    """Search saved user messages without changing queue or memory state."""
    query = query.strip()
    if not query or not 1 <= limit <= 20:
        raise ValueError("Invalid history query or limit")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT id,raw_text,sent_at FROM sent_messages "
            "WHERE kind='utterance' AND id != ? AND raw_text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?", (exclude_id or -1, "%" + escaped + "%", limit))]


def pending_count(db_path):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        return connection.execute("SELECT count(*) FROM sent_messages WHERE status IN ('pending','processing')").fetchone()[0]


def retry_pending_now(db_path):
    """A newly saved key may resolve an earlier authorization failure."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            connection.execute("UPDATE sent_messages SET next_attempt_at=NULL "
                               "WHERE status='pending'")


def claim_next(db_path):
    ensure_schema(db_path)
    now = _now()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="microseconds")
    token = secrets.token_hex(16)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM sent_messages WHERE "
            "(status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR "
            "(status='processing' AND lease_at<=?) ORDER BY id LIMIT 1", (now, stale)).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute("UPDATE sent_messages SET status='processing',attempts=attempts+1,"
                           "lease_at=?,lease_token=? WHERE id=?", (now, token, row["id"]))
        connection.commit()
        result = dict(row)
        result["lease_token"] = token
        result["attempts"] += 1
        return result


def fail(db_path, message_id, lease_token, attempts, code):
    delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            connection.execute("UPDATE sent_messages SET status='pending',next_attempt_at=?,"
                               "lease_at=NULL,lease_token=NULL,last_error_code=? "
                               "WHERE id=? AND status='processing' AND lease_token=?",
                               (retry_at, code, message_id, lease_token))


def complete(db_path, message_id, lease_token, result):
    """Store at most one grounded memory and mark its source done atomically."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM sent_messages WHERE id=?", (message_id,)).fetchone()
        if row is None or row["status"] != "processing" or row["lease_token"] != lease_token:
            connection.rollback()
            return None
        memory_id = None
        if result["kind"] != "none":
            if result["kind"] not in ("event", "profile", "goal") or \
                    result["category"] not in storage.CATEGORIES or \
                    not result["memory"] or result["memory"] not in row["raw_text"] or \
                    is_sensitive(result["memory"]):
                raise ValueError("Unverified derived memory")
            existing = connection.execute(
                "SELECT id FROM memories WHERE source_message_id=?", (message_id,)).fetchone()
            if existing:
                memory_id = existing["id"]
            else:
                kind = result["kind"]
                category = "goal" if kind == "goal" else result["category"]
                analysis = {"should_save": True, "category": category, "memory_type": kind,
                            "summary": "", "tags": [], "reason": "", "source": "claude-sonnet",
                            "fallback_reason": "", "suggested_replaces_id": None,
                            "source_message_id": message_id}
                cursor = connection.execute(
                    "INSERT INTO memories(content,created_at,category,raw_text,memory_type,"
                    "metadata,brain_data,source_message_id) VALUES(?,?,?,?,?,?,?,?)",
                    (result["memory"], row["sent_at"], category, row["raw_text"], kind,
                     "{}", json.dumps(analysis, ensure_ascii=False), message_id))
                memory_id = cursor.lastrowid
                storage._audit_memory(connection, memory_id, "derive",
                                      after=storage._audit_row(connection, memory_id),
                                      actor="memory_processor")
        connection.execute("UPDATE sent_messages SET status='processed',processed_at=?,"
                           "derived_memory_id=?,lease_at=NULL,lease_token=NULL,"
                           "next_attempt_at=NULL,last_error_code=NULL WHERE id=?",
                           (_now(), memory_id, message_id))
        connection.commit()
    if memory_id is not None:
        try:
            storage._refresh_search_index(db_path)
        except (OSError, ValueError):
            # The index is rebuildable from committed memories on the next query.
            pass
    return memory_id
