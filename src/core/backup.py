"""Durable SQLite backups. Existing backup files are never deleted."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import data_location


def backup_database(db_path, label="auto"):
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    backup_dir = data_location.backups_for_db(db_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    final = backup_dir / ("jarvis-" + label + "-" + stamp + ".db")
    index = 1
    while final.exists():
        final = backup_dir / ("jarvis-" + label + "-" + stamp + "-" + str(index) + ".db")
        index += 1
    temporary = final.with_suffix(".tmp")
    try:
        with closing(sqlite3.connect("file:" + db_path.resolve().as_posix() + "?mode=ro", uri=True)) as original:
            if original.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Source database integrity failed")
            count = original.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            counts = {}
            for table in ("sent_messages", "conversation_messages", "conversation_audit",
                          "memory_audit"):
                if original.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (table,)).fetchone():
                    counts[table] = original.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            with closing(sqlite3.connect(str(temporary))) as copied:
                original.backup(copied)
        with closing(sqlite3.connect("file:" + temporary.resolve().as_posix() + "?mode=ro", uri=True)) as copied:
            if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok" or \
                    copied.execute("SELECT COUNT(*) FROM memories").fetchone()[0] != count or \
                    any(copied.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] != expected
                        for table, expected in counts.items()):
                raise sqlite3.DatabaseError("Backup verification failed")
        os.replace(str(temporary), str(final))
        return final
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_daily(db_path):
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    backup_dir = data_location.backups_for_db(db_path)
    today = datetime.now().astimezone().strftime("%Y%m%d")
    if backup_dir.is_dir() and any(backup_dir.glob("jarvis-auto-" + today + "-*.db")):
        return None
    return backup_database(db_path)
