"""Capture receipts and gaps beside the existing immutable journal.

No model calls, actions, summarisation or speaker identification. The capture
id, not the text, deduplicates delivery; two identical utterances are two rows.
"""
import sqlite3
import math
from contextlib import closing
from datetime import datetime, timezone
import backup
import message_log
import conversation_log


def ensure_schema(db_path):
    message_log.ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='speech_captures'").fetchone()
    if exists:
        return
    backup.backup_database(db_path, label='pre-speech-journal')
    with closing(sqlite3.connect(str(db_path))) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS speech_captures (
            capture_id TEXT PRIMARY KEY, source_message_id INTEGER UNIQUE NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            speaker TEXT NOT NULL DEFAULT 'unconfirmed', addressed INTEGER NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TABLE IF NOT EXISTS speech_gaps (
            id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT NOT NULL, reason TEXT NOT NULL)''')


def stamp(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 32503680000:
        raise ValueError('Invalid capture time')
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds')


def record(db_path, event, session_id=None):
    text, ident = event.get('text'), event.get('capture_id')
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError('Invalid captured text')
    if message_log.is_sensitive(text):
        raise message_log.SensitiveInputError('Sensitive captured text')
    if not isinstance(ident, str) or not 8 <= len(ident) <= 80 or not ident.isalnum():
        raise ValueError('Invalid capture identity')
    start, end = stamp(event['start']), stamp(event['end'])
    if event['end'] < event['start']:
        raise ValueError('Invalid capture interval')
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_message_id FROM speech_captures WHERE capture_id=?', (ident,)).fetchone()
        if old:
            return old[0]
        source_id = db.execute("INSERT INTO sent_messages(raw_text,sent_at,kind,status,channel) "
                               "VALUES(?,?,'utterance','pending','heard')", (text, start)).lastrowid
        conversation_log._insert_user(db, text, start, session_id, source_id, 'ambient-unconfirmed')
        db.execute('INSERT INTO speech_captures(capture_id,source_message_id,started_at,ended_at) VALUES(?,?,?,?)',
                   (ident, source_id, start, end))
        return source_id


def address(db_path, source_id, command=False):
    """Link a live interaction to its original audio receipt, never rewrite the words."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db, db:
        if not db.execute('SELECT 1 FROM speech_captures WHERE source_message_id=?', (source_id,)).fetchone():
            raise ValueError('Missing capture receipt')
        db.execute('UPDATE speech_captures SET addressed=1 WHERE source_message_id=?', (source_id,))
        db.execute("UPDATE sent_messages SET channel='typed' WHERE id=?", (source_id,))
        if command:
            db.execute("UPDATE sent_messages SET kind='command',status='skipped' WHERE id=? AND status='pending'", (source_id,))
        db.execute("UPDATE conversation_messages SET origin='voice-directed' WHERE source_message_id=?", (source_id,))


def gap(db_path, event):
    ensure_schema(db_path)
    allowed = {'capture_overflow', 'capture_processing', 'input_restarted',
               'transcription_backlog', 'transcription_failed', 'session_started', 'session_stopped'}
    reason = event.get('reason')
    if reason not in allowed:
        return
    with closing(sqlite3.connect(str(db_path))) as db, db:
        db.execute('INSERT INTO speech_gaps(started_at,ended_at,reason) VALUES(?,?,?)',
                   (stamp(event['start']) if event.get('start') else None, stamp(event['end']), reason))


def performance(db_path, source_id):
    """Keep verbatim lyrics, but revoke tentative direction and pending personal learning."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as db, db:
        db.execute("UPDATE sent_messages SET channel='heard',status='skipped',lease_token=NULL,lease_at=NULL "
                   "WHERE id=? AND status IN ('pending','processing')", (source_id,))
        db.execute('UPDATE speech_captures SET addressed=0 WHERE source_message_id=?', (source_id,))
        db.execute("UPDATE conversation_messages SET origin='ambient-unconfirmed' WHERE source_message_id=? "
                   "AND role='user'", (source_id,))


def status(db_path):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as db:
        return dict(captures=db.execute('SELECT COUNT(*) FROM speech_captures').fetchone()[0],
                    unprocessed=db.execute("SELECT COUNT(*) FROM sent_messages WHERE channel='heard' AND status IN ('pending','processing')").fetchone()[0],
                    recent_gaps=[dict(start=a,end=b,reason=c) for a,b,c in db.execute(
                        'SELECT started_at,ended_at,reason FROM speech_gaps ORDER BY id DESC LIMIT 8')])
