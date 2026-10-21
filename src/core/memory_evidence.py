"""Additive evidence indexes. Originals and legacy memories are never rewritten.

Inferences live separately; scene tags point at original sent_messages. Times in
this index are model interpretations with a verbatim time phrase, never proof an
event occurred. The conversation timestamp is always kept separately.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime

import backup
import storage


def object_value(text):
    try:
        value = json.loads(text or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def is_inference(row):
    return object_value(row.get('metadata')).get('kind') == 'inference' or \
        (row.get('raw_text') or '').startswith(('(Inference)', '（推断）'))


def ensure_schema(path):
    with closing(sqlite3.connect(str(path))) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_evidence_meta'").fetchone():
            return
    backup.backup_database(path, label='pre-memory-evidence')
    with closing(sqlite3.connect(str(path), timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_analyses (
            id INTEGER PRIMARY KEY, scene_id INTEGER, source_memory_id INTEGER UNIQUE,
            content TEXT NOT NULL, category TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_evidence_links (
            scene_id INTEGER NOT NULL, message_id INTEGER NOT NULL, category TEXT NOT NULL,
            temporal_kind TEXT NOT NULL DEFAULT 'unknown', event_at TEXT,
            time_text TEXT NOT NULL DEFAULT '',
            UNIQUE(scene_id,message_id,category,temporal_kind,event_at,time_text))''')
        db.execute('CREATE INDEX IF NOT EXISTS evidence_source ON memory_evidence_links(message_id,category)')
        # Old inference records remain byte-for-byte available via their old IDs;
        # default factual retrieval excludes them. Copy them to a read-only analysis view.
        for record in db.execute('SELECT id,raw_text,created_at,category,metadata FROM memories').fetchall():
            row = dict(record)
            if is_inference(row):
                meta = object_value(row['metadata'])
                db.execute('''INSERT OR IGNORE INTO memory_analyses
                    (scene_id,source_memory_id,content,category,metadata,created_at) VALUES(?,?,?,?,?,?)''',
                    (meta.get('scene_id'),row['id'],row['raw_text'],row['category'],
                     row['metadata'] or '{}',row['created_at']))
        # Backfill scene-category links without re-running the model or replacing text.
        for row in db.execute('SELECT id,analysis FROM scenes').fetchall():
            analysis = object_value(row['analysis'])
            for item in analysis.get('memories', []):
                if isinstance(item, dict) and item.get('category') in storage.CATEGORIES:
                    for ident in item.get('evidence', []):
                        if type(ident) is int:
                            add_link(db, row['id'], ident, item['category'],
                                     'plan' if item.get('type')=='goal' else 'unknown')
        db.execute('CREATE TABLE IF NOT EXISTS memory_evidence_meta (version INTEGER NOT NULL)')
        db.execute('INSERT INTO memory_evidence_meta VALUES(1)')
        db.commit()


def add_link(db, scene_id, message_id, category, temporal_kind='unknown', event_at=None, time_text=''):
    # SQLite UNIQUE considers NULL values distinct; use a stable empty sentinel.
    db.execute('''INSERT OR IGNORE INTO memory_evidence_links
        (scene_id,message_id,category,temporal_kind,event_at,time_text) VALUES(?,?,?,?,?,?)''',
        (scene_id,message_id,category,temporal_kind,event_at or '',time_text))


def temporal(item, rows):
    """Accept only explicit time evidence; never default event time to speech time."""
    kind = item.get('temporal_kind', 'plan' if item['type']=='goal' else 'unknown')
    if kind not in ('reported_event','plan','trait','unknown'):
        raise ValueError('temporal kind')
    if item['type']=='goal':
        kind = 'plan'
    stamp, phrase = item.get('event_at'), item.get('time_text','')
    if not isinstance(phrase,str) or len(phrase)>120:
        raise ValueError('time phrase')
    if phrase and not any(phrase in rows[i]['raw_text'] for i in item['evidence']):
        raise ValueError('time phrase has no evidence')
    if stamp is not None:
        if not isinstance(stamp,str) or not phrase or kind not in ('reported_event','plan'):
            raise ValueError('time without evidence')
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            raise ValueError('event time needs timezone')
        stamp = moment.isoformat()
    return dict(temporal_kind=kind,event_at=stamp,time_text=phrase)


def annotations(path, source_id):
    if not source_id:
        return []
    with closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('''SELECT scene_id,category,temporal_kind,
            NULLIF(event_at,'') AS event_at,time_text FROM memory_evidence_links
            WHERE message_id=? ORDER BY scene_id,category''',(source_id,))]


def analyses(path, ident=None, query='', offset=0):
    with closing(sqlite3.connect(str(path))) as db:
        db.row_factory = sqlite3.Row
        clauses = ['(a.source_memory_id IS NULL OR (m.id IS NOT NULL AND m.deleted_at IS NULL))']
        params = []
        if ident is not None:
            clauses.append('a.id=?'); params.append(ident)
        if query:
            clauses.append('instr(lower(a.content),lower(?))>0'); params.append(query)
        return [dict(r) for r in db.execute('''SELECT a.* FROM memory_analyses a
            LEFT JOIN memories m ON m.id=a.source_memory_id WHERE '''+' AND '.join(clauses)+
            ' ORDER BY a.id LIMIT 21 OFFSET ?',params+[offset])]
