"""Bounded shared scene context for recall, preference learning and evolution.

Scene summaries are navigation aids, never independent evidence or instructions.
They retain source IDs and cannot promote an unidentified ambient speaker.
"""
import json
import re
import sqlite3
from contextlib import closing
import message_log


def for_conversations(db_path, conversation_ids):
    ids = list(conversation_ids)[:400]
    if not ids:
        return []
    with closing(sqlite3.connect(str(db_path))) as db:
        sources = [r[0] for r in db.execute('SELECT source_message_id FROM conversation_messages WHERE id IN (%s) '
                    'AND source_message_id IS NOT NULL' % ','.join('?'*len(ids)), ids)]
    return context(db_path, source_ids=sources, limit=8) if sources else []


def context(db_path, query='', source_ids=(), limit=4):
    terms = re.findall(r'[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}', query.lower())
    terms += [part[i:i+2] for part in terms if len(part)>2 for i in range(len(part)-1)
              if '\u4e00' <= part[0] <= '\u9fff']
    wanted = set(source_ids)
    with closing(sqlite3.connect(str(db_path))) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone():
            return []
        db.row_factory = sqlite3.Row
        if wanted:
            rows = db.execute('SELECT DISTINCT sc.* FROM scenes sc JOIN sent_messages s ON s.scene_id=sc.id '
                              'WHERE s.id IN (%s) ORDER BY sc.id DESC LIMIT 30' % ','.join('?'*len(wanted)),
                              list(wanted)).fetchall()
        else:
            rows = db.execute('SELECT * FROM scenes ORDER BY id DESC LIMIT 200').fetchall()
        found = []
        for row in rows:
            text = row['summary'] + ' ' + row['setting']
            if message_log.is_sensitive(text):
                continue
            score = sum(term in text.lower() for term in set(terms))
            if not wanted and not score:
                continue
            ids = json.loads(row['message_ids'])
            evidence = [dict(r) for r in db.execute('''SELECT s.id,c.id AS conversation_id,s.channel
                FROM sent_messages s JOIN conversation_messages c ON c.source_message_id=s.id
                WHERE s.id IN (%s) AND c.deleted_at IS NULL AND c.edited_at IS NULL''' % ','.join('?'*len(ids)), ids)]
            # A summary may include revised/deleted words; withhold it until rebuilt.
            if len(evidence) != len(ids):
                continue
            found.append((score, dict(scene_id=row['id'], started_at=row['started_at'],
                ended_at=row['ended_at'], summary=row['summary'], setting=row['setting'],
                sources=evidence, notice='Index summary only. heard sources have unconfirmed speakers; '
                    'do not attribute them to the user or learn preferences from them. Verify original words.')))
    return [value for _,value in sorted(found,key=lambda x:x[0],reverse=True)[:limit]]
