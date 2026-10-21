"""Read bounded, chronological evidence for evolution without modifying either database."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import conversation_log


def read(db_path, config, after_id=0, feedback_after=0, max_chars=40000, first_days=14):
    lines, used = [], 0
    feedback_cursor = int(feedback_after or 0)
    feedback = Path(config).parent / "preferences" / "feedback.db"
    if feedback.is_file():
        with closing(sqlite3.connect(feedback.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT e.id,e.at,e.value FROM feedback_events e JOIN feedback f ON f.id=e.feedback_id "
                "WHERE e.id>? AND f.status='active' AND e.kind IN ('reason','ideal','natural') "
                "AND TRIM(e.value)<>'' ORDER BY e.id LIMIT 50", (feedback_cursor,)).fetchall()
        for ident, stamp, text in rows:
            text = " ".join(str(text).split())[:1000]
            if used + len(text) + 30 > max_chars // 3:
                break
            lines.append((-ident, "user", conversation_log.local_time(stamp), text))
            used += len(text) + 30
            feedback_cursor = ident
    source = Path(db_path)
    if source.is_file():
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            has_journal = db.execute("SELECT 1 FROM sqlite_master WHERE name='sent_messages'").fetchone()
            since = (datetime.now(timezone.utc) - timedelta(days=first_days)).isoformat()
            where = "c.id > ?" if after_id else "c.created_at >= ?"
            value = int(after_id) if after_id else since
            rows = db.execute("SELECT c.id,c.role,c.created_at,c.display_text,c.origin," +
                              ("s.channel,s.status" if has_journal else "NULL,NULL") + " FROM conversation_messages c " +
                              ("LEFT JOIN sent_messages s ON s.id=c.source_message_id " if has_journal else '') + "WHERE " + where +
                              " AND c.deleted_at IS NULL ORDER BY c.id ASC LIMIT 400", (value,)).fetchall()
        for ident, role, stamp, text, origin, channel, status in rows:
            if origin in ('ambient-unconfirmed','voice-directed') and status in ('pending','processing'):
                break  # keep the cursor before unfinished scene analysis
            if channel == 'heard':
                role = 'ambient'  # may be nearby people/media, never admissible user evidence
            text = " ".join(str(text or "").split())[:1000]
            if used + len(text) + 30 > max_chars:
                break
            lines.append((ident, role, conversation_log.local_time(stamp), text))
            used += len(text) + 30
    return sorted(lines, key=lambda row: row[2]), feedback_cursor
