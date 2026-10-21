"""Persistent, editable view of the complete two-role conversation archive.

The original text is immutable. Edits change display_text and leave an audit
record; the older sent_messages table remains the classifier's source queue.
"""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ensure_schema(db_path):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                raw_text TEXT NOT NULL,
                display_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_message_id INTEGER UNIQUE,
                reply_to_id INTEGER,
                deleted_at TEXT,
                edited_at TEXT,
                origin TEXT NOT NULL DEFAULT 'current'
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_conversation_time "
                       "ON conversation_messages(created_at,id)")
            db.execute("""CREATE TABLE IF NOT EXISTS conversation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                at_utc TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                actor TEXT NOT NULL
            )""")
            # Older releases saved only user messages. Preserve their IDs via
            # source_message_id and mark the session as unknown, not invented.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_messages'").fetchone():
                db.execute("""INSERT INTO conversation_messages
                    (session_id,role,raw_text,display_text,created_at,source_message_id,origin)
                    SELECT NULL,'user',raw_text,raw_text,sent_at,id,'legacy-user-journal'
                    FROM sent_messages WHERE NOT EXISTS
                    (SELECT 1 FROM conversation_messages c WHERE c.source_message_id=sent_messages.id)""")


def _insert_user(db, text, stamp, session_id, source_message_id=None, origin="current"):
    return db.execute("""INSERT INTO conversation_messages
        (session_id,role,raw_text,display_text,created_at,source_message_id,origin)
        VALUES(?,'user',?,?,?,?,?)""",
        (session_id, text, text, stamp, source_message_id, origin)).lastrowid


def append_user(db_path, text, session_id, source_message_id=None):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db:
        with db:
            return _insert_user(db, text, _now(), session_id, source_message_id)


def conversation_id_for_source(db_path, source_message_id):
    with closing(sqlite3.connect(str(db_path))) as db:
        row = db.execute("SELECT id FROM conversation_messages WHERE source_message_id=?",
                         (source_message_id,)).fetchone()
    return row[0] if row else None


def append_assistant(db_path, text, session_id, reply_to_id=None, raw_text=None):
    """raw_text (optional) is what MYN remembers of the turn for the model: the reply
    plus a note of the actions it really took; display_text is what the user saw."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Reply cannot be empty.')
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db:
        with db:
            if reply_to_id is not None:
                prior = db.execute("SELECT id FROM conversation_messages WHERE reply_to_id=? AND role='assistant'",
                                   (reply_to_id,)).fetchone()
                if prior:
                    return prior[0]
            return db.execute("""INSERT INTO conversation_messages
                (session_id,role,raw_text,display_text,created_at,reply_to_id)
                VALUES(?,'assistant',?,?,?,?)""",
                (session_id, raw_text or text, text, _now(), reply_to_id)).lastrowid


def get(db_path, message_id):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM conversation_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None


def latest_assistant(db_path, within_minutes=30, now=None):
    "MYN's most recent reply still in the current conversation (or None), to which communication feedback refers."
    from datetime import timedelta
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM conversation_messages WHERE role='assistant' AND deleted_at IS NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    try:
        stamp = datetime.fromisoformat(row["created_at"])
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if (now or datetime.now(timezone.utc)) - stamp > timedelta(minutes=within_minutes):
        return None
    return dict(row)


def list_messages(db_path, deleted=False, limit=1000, query=None):
    ensure_schema(db_path)
    if type(limit) is not int or not 1 <= limit <= 1000000:
        raise ValueError("Invalid history limit")
    statement = "SELECT * FROM conversation_messages WHERE deleted_at IS " + ("NOT NULL" if deleted else "NULL")
    params = []
    if query:
        if len(query) > 200:
            raise ValueError('Query is too long.')
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement += " AND display_text LIKE ? ESCAPE '\\'"
        params.append("%" + escaped + "%")
    statement += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(sqlite3.connect(str(db_path))) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, params)]


def recent_pairs(db_path, limit=3):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        rows = db.execute("""SELECT u.display_text,a.display_text,u.created_at FROM conversation_messages a
            JOIN conversation_messages u ON u.id=a.reply_to_id
            WHERE a.role='assistant' AND a.deleted_at IS NULL AND u.deleted_at IS NULL
            AND u.source_message_id IS NOT NULL
            ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"question": question[:300], "answer": answer[:400], "asked_at": asked_at}
            for question, answer, asked_at in reversed(rows)]


def recent_turns(db_path, limit=8, gap_minutes=30, now=None, question_chars=2000, answer_chars=4000):
    """The current conversation's complete user/assistant exchanges, oldest first.

    A pause longer than gap_minutes starts a new conversation, the way people
    treat it: turns before the pause are not sent as context (they stay
    searchable through the memory and history tools).
    """
    from datetime import timedelta, timezone

    def moment(value):
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    ensure_schema(db_path)
    gap = timedelta(minutes=gap_minutes)
    with closing(sqlite3.connect(str(db_path))) as db:
        rows = db.execute("""SELECT u.display_text,a.raw_text,u.created_at,a.created_at
            FROM conversation_messages a
            JOIN conversation_messages u ON u.id=a.reply_to_id
            WHERE a.role='assistant' AND a.deleted_at IS NULL AND u.deleted_at IS NULL
            AND u.source_message_id IS NOT NULL
            ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
    turns = []
    later = now or datetime.now(timezone.utc)
    for question, answer, asked_at, answered_at in rows:
        try:
            if later - moment(answered_at) > gap:
                break
            later = moment(asked_at)
        except (TypeError, ValueError):
            break
        turns.append({"question": question[:question_chars], "answer": answer[:answer_chars],
                      "asked_at": asked_at, "answered_at": answered_at})
    return list(reversed(turns))


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _moment(value):
    from datetime import timezone
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def local_time(value, seconds=False):
    "Format a stored UTC timestamp in the user's local time with weekday."
    import memory_query
    try:
        moment = _moment(value).astimezone(memory_query.TOKYO)
    except (TypeError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d ") + WEEKDAYS[moment.weekday()] + \
        moment.strftime(" %H:%M:%S" if seconds else " %H:%M")


def say_gap(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = (['%d days' % days] if days else []) + (['%d hours' % hours] if hours else []) + \
        ['%d minutes' % minutes]
    return " ".join(parts)


def last_break(db_path, now=None, gap_minutes=30, exclude_source_id=None):
    """When the user's previous conversation ended and how long the silence was: the
    latest pause of more than gap_minutes (including the one up to now), so MYN can tell
    e.g. how long the user slept. None when there is no earlier conversation."""
    from datetime import timedelta, timezone
    ensure_schema(db_path)
    gap = timedelta(minutes=gap_minutes)
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(str(db_path))) as db:
        rows = db.execute("""SELECT role, display_text, created_at FROM conversation_messages
            WHERE deleted_at IS NULL AND origin!='ambient-unconfirmed'
            AND (source_message_id IS NULL OR source_message_id!=?)
            ORDER BY id DESC LIMIT 600""", (exclude_source_id or -1,)).fetchall()
    import presence
    later, started = now, None
    for role, text, created in rows:
        if role == "user" and not presence.meaningful(text):
            continue  # microphone noise ("you", "Yeah.") is not the user being there
        try:
            moment = _moment(created)
        except (TypeError, ValueError):
            continue
        if later - moment > gap:
            result = {"previous_conversation_ended_at": local_time(created),
                      "last_message_then": {"role": 'User' if role == "user" else "MYN", "text": text[:80]},
                      "silence": say_gap(later - moment)}
            result["this_conversation_started_at"] = local_time(started) if started else '(This current message)'
            return result
        later, started = moment, created
    return None


def messages_between(db_path, start, end, limit=40):
    """Both roles in a time window, oldest first, with the user's local times."""
    import message_log
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        rows = db.execute("""SELECT role, display_text, created_at,origin FROM conversation_messages
            WHERE deleted_at IS NULL AND julianday(created_at)>=julianday(?)
            AND julianday(created_at)<julianday(?) ORDER BY id LIMIT ?""",
                          (start.isoformat(), end.isoformat(), limit + 1)).fetchall()
    found = [{"time": local_time(created, seconds=True), "role": 'unconfirmed_speaker' if origin=='ambient-unconfirmed' else role,
              "text": text[:400]}
             for role, text, created, origin in rows[:limit] if not message_log.is_sensitive(text)]
    return found, len(rows) > limit


def period_messages(db_path, period="recent", limit=20):
    if period == "recent":
        return list_messages(db_path, limit=limit)
    import memory_query
    from datetime import time, timedelta
    now = datetime.now(memory_query.TOKYO)
    if period == "today":
        start = datetime.combine(now.date(), time.min, memory_query.TOKYO)
        end = start + timedelta(days=1)
    elif period == "week":
        start = datetime.combine(now.date() - timedelta(days=now.weekday()),
                                 time.min, memory_query.TOKYO)
        end = start + timedelta(days=7)
    elif period == "month":
        start = datetime(now.year, now.month, 1, tzinfo=memory_query.TOKYO)
        end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1,
                       tzinfo=memory_query.TOKYO)
    else:
        raise ValueError("Invalid history period")
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT * FROM conversation_messages WHERE deleted_at IS NULL "
            "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) "
            "ORDER BY id DESC LIMIT ?", (start.isoformat(), end.isoformat(), limit))]


def search(db_path, query, limit=6, exclude_source_id=None):
    """Search both roles, with Tokyo date windows and bilingual concepts."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or not 1 <= limit <= 20:
        raise ValueError("Invalid history query or limit")
    import memory_query
    import semantic_search
    import message_log

    window = memory_query.saved_window(query)
    with closing(sqlite3.connect(str(db_path))) as db:
        db.row_factory = sqlite3.Row
        sql = "SELECT * FROM conversation_messages WHERE deleted_at IS NULL AND (source_message_id IS NULL OR source_message_id!=?)"
        params = [exclude_source_id or -1]
        if window:
            sql += " AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)"
            params.extend(value.isoformat() for value in window)
        sql += " ORDER BY id DESC LIMIT 2000"
        rows = [dict(row) for row in db.execute(sql, params)]
    reduced = memory_query.DATE_RE.sub("", query)
    reduced = re.sub(r"昨天|昨日|今天|今日|前天|上周|上星期|本周|这周|这星期|"
                     r"搜索|查找|查询|有没有|提到|说过|记忆|记录|原文|我|的|\b(?:yesterday|today|last week|search|find|history|mention)\b",
                     " ", reduced, flags=re.I)
    terms = set(semantic_search._terms(reduced))
    concepts = set(semantic_search._concepts(reduced))
    broad_date = bool(window and memory_query._broad_date_request(query))
    ranked = []
    for row in rows:
        text = row["display_text"]
        if message_log.is_sensitive(text):
            continue
        if row["role"] == "assistant" and not re.search(
                r"MYN|AI|助手|回复|回答|你之前说|\b(?:assistant|your reply)\b",
                query, re.I):
            # An earlier AI reply is not independent evidence of a user fact.
            continue
        if row["role"] == "user" and re.search(r"[?？]\s*$|(?:吗|么|什么|谁|哪里|哪天|哪次|有没有|是否)\s*$", text) and not re.search(
                r"我.{0,8}问过|问题|提问|\b(?:asked|question)\b", query, re.I):
            # An earlier question is not evidence that its premise happened.
            continue
        if row["role"] == "user" and re.match(
                r"\s*20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}.*\[[a-z]+/[a-z]+\]", text, re.I) and not re.search(
                r"引用|转发|贴过|发过|原文|\b(?:quoted|pasted)\b", query, re.I):
            # A pasted older record is evidence of a quote, not a new event
            # on the day the quote was sent.
            continue
        matches = len(terms & set(semantic_search._terms(text)))
        related = len(concepts & set(semantic_search._concepts(text)))
        literal = reduced.strip().casefold() in text.casefold() if reduced.strip() else False
        score = matches * 2 + related * 3 + (3 if literal else 0)
        if score or broad_date:
            row["confidence"] = round(min(1.0, score / 8 if score else .6), 3)
            row["confidence_kind"] = "retrieval_relevance"
            ranked.append((score, row["id"], row))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in ranked[:limit]]


def change(db_path, message_id, action, expected=None, text=None):
    """Edit display copy or soft-delete/restore; audit every successful change."""
    if action not in ("edit", "delete", "restore"):
        raise ValueError("Unsupported history change")
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM conversation_messages WHERE id=?", (message_id,)).fetchone()
        if not row or expected is not None and row["display_text"] != expected:
            db.rollback()
            return False
        before = {"display_text": row["display_text"], "deleted_at": row["deleted_at"]}
        stamp = _now()
        if action == "edit":
            import message_log
            if row["deleted_at"] or not isinstance(text, str) or not text.strip() or len(text) > 5000 or message_log.is_sensitive(text):
                raise ValueError('Invalid content or sensitive credentials detected.')
            db.execute("UPDATE conversation_messages SET display_text=?,edited_at=? WHERE id=?",
                       (text, stamp, message_id))
            if row["role"] == "user" and row["source_message_id"] is not None:
                db.execute("""UPDATE sent_messages SET status='skipped',processed_at=?,
                    lease_at=NULL,lease_token=NULL,last_error_code='conversation_corrected'
                    WHERE id=? AND status IN ('pending','processing')""",
                    (stamp, row["source_message_id"]))
            after = {"display_text": text, "deleted_at": None}
        elif action == "delete":
            if row["deleted_at"]:
                db.rollback()
                return False
            db.execute("UPDATE conversation_messages SET deleted_at=? WHERE id=?", (stamp, message_id))
            if row["role"] == "user" and row["source_message_id"] is not None:
                db.execute("""UPDATE sent_messages SET status='skipped',processed_at=?,
                    lease_at=NULL,lease_token=NULL,last_error_code='conversation_archived'
                    WHERE id=? AND status IN ('pending','processing')""",
                    (stamp, row["source_message_id"]))
            after = {"display_text": row["display_text"], "deleted_at": stamp}
        else:
            if not row["deleted_at"]:
                db.rollback()
                return False
            db.execute("UPDATE conversation_messages SET deleted_at=NULL WHERE id=?", (message_id,))
            if row["role"] == "user" and row["source_message_id"] is not None and \
                    row["display_text"] == row["raw_text"]:
                db.execute("""UPDATE sent_messages SET status='pending',processed_at=NULL,
                    next_attempt_at=NULL,last_error_code=NULL WHERE id=? AND status='skipped'
                    AND kind='utterance' AND last_error_code='conversation_archived'""",
                    (row["source_message_id"],))
            after = {"display_text": row["display_text"], "deleted_at": None}
        db.execute("""INSERT INTO conversation_audit
            (message_id,action,at_utc,before_json,after_json,actor) VALUES(?,?,?,?,?,'user')""",
            (message_id, action, stamp, json.dumps(before, ensure_ascii=False),
             json.dumps(after, ensure_ascii=False)))
        db.commit()
        return True
