"""Controlled backup, restore and explicit local exports for MYN personal data."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import backup
import data_location


def _read_rows(db, table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")]


def status(db_path):
    db_path = Path(db_path)
    with closing(sqlite3.connect("file:" + db_path.resolve().as_posix() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError('Database integrity check failed.')
        counts = {}
        for table in ("memories", "sent_messages", "conversation_messages",
                      "conversation_audit", "memory_audit"):
            counts[table] = (db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                             if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                           (table,)).fetchone() else 0)
        active = db.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
    backups = sorted(data_location.backups_for_db(db_path).glob("*.db"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    return {"database": str(db_path), "size_bytes": db_path.stat().st_size,
            "counts": counts, "active_memories": active,
            "backup_count": len(backups),
            "latest_backup": str(backups[0]) if backups else None}


def export_profile(db_path):
    """Export readable data locally; never include DPAPI keys or settings."""
    db_path = Path(db_path)
    root = data_location.root_for_db(db_path)
    for part in ("conversations", "memories", "exports"):
        (root / part).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    with closing(sqlite3.connect("file:" + db_path.resolve().as_posix() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError('Database integrity check failed; nothing exported.')
        conversations = _read_rows(db, "conversation_messages")
        memories = _read_rows(db, "memories")
        audits = {name: _read_rows(db, name) for name in
                  ("conversation_audit", "memory_audit")}
    convo_path = root / "conversations" / ("conversation-" + stamp + ".jsonl")
    memory_path = root / "memories" / ("memories-" + stamp + ".json")
    audit_path = root / "exports" / ("audit-" + stamp + ".json")
    for path, content in (
            (convo_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in conversations)),
            (memory_path, json.dumps(memories, ensure_ascii=False, indent=2)),
            (audit_path, json.dumps(audits, ensure_ascii=False, indent=2))):
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()
    manifest = root / "exports" / ("profile-" + stamp + ".json")
    manifest.write_text(json.dumps({"format": "myn-personal-data", "version": 1,
                                    "exported_at_utc": datetime.now(timezone.utc).isoformat(),
                                    "conversations": str(convo_path), "memories": str(memory_path),
                                    "audit": str(audit_path),
                                    "conversation_count": len(conversations),
                                    "memory_count": len(memories)}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return manifest


def list_backups(db_path):
    return sorted(data_location.backups_for_db(db_path).glob("*.db"),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def restore_backup(db_path, selected):
    """Verify a selected local backup, preserve the current DB, then replace."""
    db_path = Path(db_path)
    selected = Path(selected).resolve(strict=True)
    permitted = data_location.backups_for_db(db_path).resolve()
    if selected.parent != permitted or selected.suffix.lower() != ".db":
        raise ValueError("Only SQLite files in MYN's backup folder can be restored.")
    candidate_state = data_location._state(selected)
    with closing(sqlite3.connect("file:" + selected.as_posix() + "?mode=ro", uri=True)) as candidate:
        if candidate.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or not candidate.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
            raise sqlite3.DatabaseError('Backup verification failed; the current database was not changed.')
    before = backup.backup_database(db_path, label="pre-restore")
    temporary = db_path.with_suffix(".restoring")
    if temporary.exists():
        temporary.unlink()
    try:
        with closing(sqlite3.connect("file:" + selected.as_posix() + "?mode=ro", uri=True)) as candidate:
            with closing(sqlite3.connect(str(temporary))) as copied:
                candidate.backup(copied)
        with closing(sqlite3.connect("file:" + temporary.resolve().as_posix() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError('Backup copy verification failed; the current database was not changed.')
        if data_location._state(temporary) != candidate_state:
            raise sqlite3.DatabaseError('Backup record counts differ; the current database was not changed.')
        os.replace(str(temporary), str(db_path))
        from conversation_log import ensure_schema
        ensure_schema(db_path)
        from semantic_search import sync_index
        sync_index(db_path)
        return before
    finally:
        if temporary.exists():
            temporary.unlink()
