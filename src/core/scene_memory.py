"""Scenes and the user profile: how MYN turns raw lines into understanding.

A scene is a run of saved user lines close together in time (typed messages and
speech heard by the always-on microphone). The background processor analyses a
whole scene at once, then writes its summary and the memories it supports in
one transaction here. The profile ("what MYN knows about you") is kept as
versions, so every AI update and every user edit stays visible and reversible.
"""

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import backup
import message_log
import storage
import memory_evidence

SCENE_GAP = timedelta(minutes=10)  # a longer pause starts a new scene
SCENE_SETTLE = timedelta(minutes=3)  # wait this long after the last line before analysing
SCENE_MAX_WAIT = timedelta(minutes=30)  # but never hold a scene longer than this
SCENE_MAX_LINES = 30
PROFILE_MAX_CHARS = 4000


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or _now()).isoformat(timespec="microseconds")


def _parse(value):
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def ensure_schema(db_path):
    message_log.ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    if {"scenes", "user_profile_versions"} <= names:
        memory_evidence.ensure_schema(db_path)
        return
    backup.backup_database(db_path, label="pre-scene-memory")
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                analysed_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                setting TEXT NOT NULL,
                speakers TEXT NOT NULL,
                message_ids TEXT NOT NULL,
                analysis TEXT NOT NULL,
                profile_applied INTEGER NOT NULL DEFAULT 0
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS user_profile_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('ai','user')),
                scene_id INTEGER
            )""")
    memory_evidence.ensure_schema(db_path)


def claim_scene(db_path, now=None):
    """Lease the oldest finished scene's lines, or return None if none is ready."""
    ensure_schema(db_path)
    now = now or _now()
    stale = _stamp(now - timedelta(minutes=10))
    token = secrets.token_hex(16)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM sent_messages WHERE kind='utterance' AND ("
            "(status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR "
            "(status='processing' AND lease_at<=?)) ORDER BY julianday(sent_at),id LIMIT 200",
            (_stamp(now), stale))]
        if not rows:
            connection.commit()
            return None
        scene, closed = [rows[0]], False
        for row in rows[1:]:
            if _parse(row["sent_at"]) - _parse(scene[-1]["sent_at"]) > SCENE_GAP:
                closed = True  # a later line already belongs to the next scene
                break
            if len(scene) >= SCENE_MAX_LINES:
                closed = True
                break
            scene.append(row)
        last, first = _parse(scene[-1]["sent_at"]), _parse(scene[0]["sent_at"])
        ready = closed or len(scene) >= SCENE_MAX_LINES or now - last >= SCENE_SETTLE or \
            now - first >= SCENE_MAX_WAIT
        if not ready:
            connection.commit()
            return None
        ids = [row["id"] for row in scene]
        connection.executemany(
            "UPDATE sent_messages SET status='processing',attempts=attempts+1,lease_at=?,"
            "lease_token=? WHERE id=?", [(_stamp(now), token, row_id) for row_id in ids])
        connection.commit()
    for row in scene:
        row["attempts"] += 1
    return {"token": token, "rows": scene}


def fail_scene(db_path, claimed, code):
    attempts = max(row["attempts"] for row in claimed["rows"])
    delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
    retry_at = _stamp(_now() + timedelta(seconds=delay))
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            connection.executemany(
                "UPDATE sent_messages SET status='pending',next_attempt_at=?,lease_at=NULL,"
                "lease_token=NULL,last_error_code=? WHERE id=? AND status='processing' "
                "AND lease_token=?",
                [(retry_at, code, row["id"], claimed["token"]) for row in claimed["rows"]])


def complete_scene(db_path, claimed, analysis):
    """Store the scene and its validated memories, and mark its lines done, atomically.

    `analysis` must already be validated by memory_processor. Returns
    (scene_id, [memory ids]) or None if the lease was lost.
    """
    rows = {row["id"]: row for row in claimed["rows"]}
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT id FROM sent_messages WHERE status='processing' AND lease_token=? AND id IN (%s)"
            % ",".join("?" * len(rows)), [claimed["token"], *rows]).fetchall()
        if len(current) != len(rows):
            connection.rollback()
            return None
        ordered = sorted(rows.values(), key=lambda row: (_parse(row['sent_at']), row['id']))
        scene = analysis["scene"]
        cursor = connection.execute(
            "INSERT INTO scenes(started_at,ended_at,analysed_at,summary,setting,speakers,"
            "message_ids,analysis) VALUES(?,?,?,?,?,?,?,?)",
            (ordered[0]["sent_at"], ordered[-1]["sent_at"], _stamp(), scene["summary"],
             scene["setting"], json.dumps(scene["speakers"], ensure_ascii=False),
             json.dumps(sorted(rows)), json.dumps(analysis, ensure_ascii=False)))
        scene_id = cursor.lastrowid
        for topic in analysis.get('topics', []):
            for row_id in topic['evidence']:
                for category in topic['categories']:
                    memory_evidence.add_link(connection, scene_id, row_id, category)
        memory_ids, first_memory = [], {}
        for item in analysis["memories"]:
            evidence = [rows[row_id] for row_id in item["evidence"]]
            text = "\n".join(row['raw_text'] for row in evidence)
            metadata = {"scene_id": scene_id, "kind": item["kind"],
                        "confidence": item["confidence"], "why": item["why"],
                        "summary": item['content'], "text_source": "verbatim_user",
                        "evidence_message_ids": item["evidence"],
                        "evidence_quotes": [row["raw_text"] for row in evidence]}
            timing = memory_evidence.temporal(item, rows)
            metadata.update(timing)
            for row_id in item['evidence']:
                memory_evidence.add_link(connection, scene_id, row_id, item['category'], **timing)
                if item['type']=='goal' and item['category']!='goal':
                    memory_evidence.add_link(connection, scene_id, row_id, 'goal', **timing)
            if item['kind'] == 'inference':
                connection.execute('''INSERT INTO memory_analyses
                    (scene_id,content,category,metadata,created_at) VALUES(?,?,?,?,?)''',
                    (scene_id,item['content'],item['category'],json.dumps(metadata,ensure_ascii=False),
                     max(row['sent_at'] for row in evidence)))
                continue
            brain = {"should_save": True, "category": item["category"],
                     "memory_type": item["type"], "summary": "", "tags": [],
                     "reason": item["why"], "source": "claude-sonnet-scene",
                     "fallback_reason": "", "suggested_replaces_id": None}
            cursor = connection.execute(
                "INSERT INTO memories(content,created_at,category,raw_text,memory_type,"
                "metadata,brain_data,source_message_id) VALUES(?,?,?,?,?,?,?,NULL)",
                (text, max(row["sent_at"] for row in evidence), item["category"],
                 text, item["type"], json.dumps(metadata, ensure_ascii=False),
                 json.dumps(brain, ensure_ascii=False)))
            memory_ids.append(cursor.lastrowid)
            storage._audit_memory(connection, cursor.lastrowid, "derive",
                                  after=storage._audit_row(connection, cursor.lastrowid),
                                  actor="scene_processor")
            for row_id in item["evidence"]:
                first_memory.setdefault(row_id, cursor.lastrowid)
        stamp = _stamp()
        connection.executemany(
            "UPDATE sent_messages SET status='processed',processed_at=?,scene_id=?,"
            "derived_memory_id=COALESCE(?,derived_memory_id),lease_at=NULL,lease_token=NULL,next_attempt_at=NULL,"
            "last_error_code=NULL WHERE id=?",
            [(stamp, scene_id, first_memory.get(row_id), row_id) for row_id in rows])
        connection.commit()
    if memory_ids:
        try:
            storage._refresh_search_index(db_path)
        except (OSError, ValueError):
            pass  # rebuildable from committed memories on the next query
    return scene_id, memory_ids


def _scene_memories(connection):
    """Map scene id -> its derived memory rows (metadata is plain JSON text)."""
    found = {}
    for row in connection.execute(
            "SELECT id,raw_text,category,memory_type,metadata,deleted_at FROM memories "
            "WHERE metadata LIKE '%\"scene_id\"%' AND " + storage.FACTUAL_MEMORY + " ORDER BY id"):
        memory = dict(zip(("id", "raw_text", "category", "memory_type", "metadata", "deleted_at"), row))
        try:
            scene_id = json.loads(memory["metadata"]).get("scene_id")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(scene_id, int):
            found.setdefault(scene_id, []).append(memory)
    return found


def requeue_history(db_path):
    """Queue every earlier line that was never part of a scene for scene analysis.

    Lines processed one by one before 0.20 get understood again in context. Their
    old memories stay; the analysis sees them as known and adds what is new.
    Deleted or command lines ('skipped') are left alone. Returns the line count.
    """
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        count = connection.execute(
            "SELECT count(*) FROM sent_messages WHERE kind='utterance' AND status='processed' "
            "AND scene_id IS NULL").fetchone()[0]
    if not count:
        return 0
    backup.backup_database(db_path, label="pre-history-reanalysis")
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            return connection.execute(
                "UPDATE sent_messages SET status='pending',next_attempt_at=NULL,attempts=0,"
                "last_error_code=NULL,lease_at=NULL,lease_token=NULL WHERE kind='utterance' "
                "AND status='processed' AND scene_id IS NULL").rowcount


def history_needing_scenes(db_path):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        return connection.execute(
            "SELECT count(*) FROM sent_messages WHERE kind='utterance' AND status='processed' "
            "AND scene_id IS NULL").fetchone()[0]


def scene(db_path, scene_id):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        ids = json.loads(result["message_ids"])
        result["lines"] = [dict(line) for line in connection.execute(
            "SELECT id,raw_text,sent_at,channel FROM sent_messages WHERE id IN (%s) ORDER BY id"
            % ",".join("?" * len(ids)), ids)] if ids else []
        result["memories"] = _scene_memories(connection).get(scene_id, [])
        result['analyses'] = [dict(r) for r in connection.execute(
            'SELECT a.* FROM memory_analyses a LEFT JOIN memories m ON m.id=a.source_memory_id '
            'WHERE a.scene_id=? AND (a.source_memory_id IS NULL OR '
            '(m.id IS NOT NULL AND m.deleted_at IS NULL)) ORDER BY a.id',(scene_id,))]
    result["speakers"] = json.loads(result["speakers"])
    return result


def recent_scenes(db_path, limit=50):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            "SELECT id,started_at,ended_at,summary,setting FROM scenes ORDER BY id DESC LIMIT ?",
            (limit,))]
        memories = _scene_memories(connection)
    for row in rows:
        row["memory_count"] = sum(1 for memory in memories.get(row["id"], [])
                                  if memory["deleted_at"] is None)
    return rows


def scenes_needing_profile(db_path, limit=1):
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        return [row[0] for row in connection.execute(
            "SELECT id FROM scenes WHERE profile_applied=0 ORDER BY id LIMIT ?", (limit,))]


def mark_profile_applied(db_path, scene_id):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            connection.execute("UPDATE scenes SET profile_applied=1 WHERE id=?", (scene_id,))


def profile(db_path):
    """The current profile text ("" before the first one) and its version row."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM user_profile_versions ORDER BY id DESC LIMIT 1").fetchone()
    return (row["content"], dict(row)) if row else ("", None)


def save_profile(db_path, content, source, scene_id=None):
    if source not in ("ai", "user") or not isinstance(content, str):
        raise ValueError('Invalid profile source or content.')
    content = content.strip()
    if len(content) > PROFILE_MAX_CHARS:
        raise ValueError('Profile exceeds %d characters.' % PROFILE_MAX_CHARS)
    if message_log.is_sensitive(content):
        raise ValueError('Profile contains a possible key or password and was not saved.')
    ensure_schema(db_path)
    with closing(sqlite3.connect(str(db_path), timeout=10)) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO user_profile_versions(content,created_at,source,scene_id) VALUES(?,?,?,?)",
                (content, _stamp(), source, scene_id))
            return cursor.lastrowid
