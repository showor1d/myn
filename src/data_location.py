"""One path policy for MYN's durable personal data and legacy migration."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PARTS = ("database", "conversations", "memories", "attachments", "indexes",
         "backups", "exports", "config")
CONFIG_FILES = ("brain-settings.json", "tts-settings.json", "file-access.json",
                "claude-key.dpapi", "elevenlabs-key.dpapi", "claude-usage.jsonl",
                "claude-protocol.jsonl", "tool-diagnostics.jsonl")
try:
    TOKYO = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")


def personal_root():
    return Path(os.environ.get("MYN_PERSONAL_DATA_DIR") or
                Path(__file__).resolve().parent.parent / "personal_data").resolve()


def database_dir():
    override = os.environ.get("MYN_DATA_DIR")
    return Path(override).resolve() if override else personal_root() / "database"


def user_data_dir():
    """Private settings and DPAPI credentials; kept outside manual exports."""
    override = os.environ.get("MYN_CONFIG_DIR")
    if override:
        return Path(override).resolve()
    if os.environ.get("MYN_DATA_DIR"):
        return database_dir()  # isolated legacy/test callers
    return personal_root() / "config"


def root_for_db(db_path):
    return Path(db_path).resolve().parent.parent


def config_for_db(db_path):
    path = Path(db_path).resolve()
    return path.parent.parent / "config" if path.parent.name == "database" else path.parent


def index_for_db(db_path):
    path = Path(db_path).resolve()
    directory = path.parent.parent / "indexes" if path.parent.name == "database" else path.parent
    return directory / (path.stem + ".semantic-index.json")


def backups_for_db(db_path):
    return root_for_db(db_path) / "backups"


def exports_for_db(db_path):
    return root_for_db(db_path) / "exports"


def ensure_layout(root=None):
    root = Path(root or personal_root())
    new_root = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if new_root and os.name == "nt":
        # NTFS cannot distinguish one Python module from another process.
        # Restrict the archive to this Windows account, SYSTEM and admins.
        identity = subprocess.check_output(
            ["whoami", "/user", "/fo", "csv", "/nh"], text=True).strip()
        sid = identity.rsplit(",", 1)[-1].strip('" ')
        if not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
            raise RuntimeError('Cannot verify the Windows user identity; data permissions were not configured.')
        subprocess.run(["icacls.exe", str(root), "/inheritance:r", "/grant:r",
                        "*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                        "*S-1-5-32-544:(OI)(CI)F"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for part in PARTS:
        (root / part).mkdir(parents=True, exist_ok=True)
    return root


def legacy_data_dir():
    root = Path(__file__).resolve().parent.parent
    default = root / 'data' if (root / 'runtime/main/python.exe').is_file() else Path(r'D:\MYN\data')
    return Path(os.environ.get("MYN_LEGACY_DATA_DIR") or default)


def legacy_candidates(target_dir):
    candidates = [legacy_data_dir()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "MYN" / "data")
    candidates.append(Path(r"D:\Jarvis\data"))
    return [path for path in candidates if path.resolve() != target_dir.resolve()]


def _state(path):
    with closing(sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("MYN database integrity check failed: " + str(path))
        result = {}
        for table in ("memories", "sent_messages", "conversation_messages"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                result[table] = db.execute("SELECT COUNT(*),MIN(id),MAX(id) FROM " + table).fetchone()
        return result


def _digest(path):
    """Detect later writes by an old MYN process without copying secret text."""
    digest = hashlib.sha256()
    with closing(sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro", uri=True)) as db:
        for table in ("memories", "sent_messages"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                for row in db.execute("SELECT * FROM " + table + " ORDER BY id"):
                    digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()


def _covers_legacy(source, target):
    """An existing target without a manifest must prove it includes old rows."""
    with closing(sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True)) as old, \
            closing(sqlite3.connect("file:" + target.resolve().as_posix() + "?mode=ro", uri=True)) as current:
        for identifier, content, created in old.execute("SELECT id,content,created_at FROM memories"):
            if current.execute("SELECT content,created_at FROM memories WHERE id=?",
                               (identifier,)).fetchone() != (content, created):
                return False
        if old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_messages'").fetchone():
            if not current.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_messages'").fetchone():
                return False
            for identifier, text, stamp in old.execute("SELECT id,raw_text,sent_at FROM sent_messages"):
                if current.execute("SELECT raw_text,sent_at FROM sent_messages WHERE id=?",
                                   (identifier,)).fetchone() != (text, stamp):
                    return False
    return True


def copy_legacy_backups(old_dir, backup_dir):
    """Copy known MYN SQLite backups without deleting or overwriting originals."""
    old_dir, backup_dir = Path(old_dir), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    candidates = list(old_dir.glob("jarvis.db.pre-*.bak"))
    prior_backups = old_dir.parent / "backups"
    if prior_backups.is_dir():
        candidates.extend(prior_backups.glob("*.db"))
    copied = []
    for source in candidates:
        before = _state(source)
        target = backup_dir / ("legacy-" + source.name + ".db")
        if target.exists():
            if _state(target) != before:
                raise RuntimeError('A legacy backup conflicts with an existing backup. Not overwritten: ' + target.name)
            continue
        temporary = target.with_suffix(".tmp")
        try:
            shutil.copy2(source, temporary)
            if _state(temporary) != before or source.stat().st_size != temporary.stat().st_size:
                raise RuntimeError('Legacy backup copy verification failed: ' + source.name)
            temporary.replace(target)
            copied.append(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return copied


def prepare_data():
    """Copy the existing SQLite database once; retain all old files for recovery."""
    target_dir = database_dir()
    if target_dir == personal_root() / "database":
        ensure_layout()
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "jarvis.db"
    manifest = target_dir.parent / "config" / "migration.json"
    if target.exists():
        _state(target)
        if manifest.is_file():
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record.get("kind") != "complete-backup":
                source = Path(record["source"])
                if source.is_file() and _digest(source) != record["source_digest"]:
                    raise RuntimeError('The legacy MYN database changed after migration. Startup stopped; close the old version and compare both databases.')
        elif target_dir == personal_root() / "database":
            source = next((path / "jarvis.db" for path in legacy_candidates(target_dir)
                           if (path / "jarvis.db").is_file()), None)
            if source is not None and not _covers_legacy(source, target):
                raise RuntimeError('The new database is missing legacy data and has no migration record. Startup stopped; the legacy database is unchanged.')
        return target, "existing"
    candidates = [path for path in legacy_candidates(target_dir) if (path / "jarvis.db").is_file()]
    if len(candidates) > 1:
        primary = candidates[0] / "jarvis.db"
        primary_ids = _state(primary).get("memories", (0,))[0]
        for other_dir in candidates[1:]:
            other = other_dir / "jarvis.db"
            with closing(sqlite3.connect("file:" + other.resolve().as_posix() + "?mode=ro", uri=True)) as old, \
                    closing(sqlite3.connect("file:" + primary.resolve().as_posix() + "?mode=ro", uri=True)) as current:
                if any(current.execute("SELECT content,created_at FROM memories WHERE id=?", (identifier,)).fetchone()
                       != (content, created) for identifier, content, created in
                       old.execute("SELECT id,content,created_at FROM memories")):
                    raise RuntimeError('Conflicting legacy databases found. Automatic merging stopped; review them manually.')
            if _state(other).get("memories", (0,))[0] > primary_ids:
                raise RuntimeError('Another legacy database contains more memories. No data source was selected automatically.')
    old_dir = candidates[0] if candidates else None
    if old_dir is not None:
        source = old_dir / "jarvis.db"
        before = _state(source)
        source_digest = _digest(source)
        temporary = target_dir / "jarvis.db.migrating"
        if temporary.exists():
            temporary.unlink()
        try:
            with closing(sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(str(temporary))) as copied:
                    original.backup(copied)
            if _state(temporary) != before or _state(source) != before or _digest(source) != source_digest:
                raise RuntimeError('The legacy database changed during migration. Original files and backups were preserved.')
            backup_dir = backups_for_db(target)
            backup_dir.mkdir(parents=True, exist_ok=True)
            snapshot = backup_dir / ("pre-migration-" +
                                     datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + ".db")
            shutil.copy2(temporary, snapshot)
            if _state(snapshot) != before:
                raise RuntimeError('Pre-migration backup verification failed. The legacy database is unchanged.')
            if target_dir == personal_root() / "database":
                settings = user_data_dir()
                settings.mkdir(parents=True, exist_ok=True)
                for name in CONFIG_FILES:
                    prior = old_dir / name
                    current = settings / name
                    if prior.is_file() and not current.exists():
                        shutil.copy2(prior, current)
                prior_index = old_dir / "jarvis.semantic-index.json"
                new_index = index_for_db(target)
                if prior_index.is_file() and not new_index.exists():
                    shutil.copy2(prior_index, new_index)
                copy_legacy_backups(old_dir, backup_dir)
            temporary.replace(target)
            if target_dir == personal_root() / "database":
                manifest.write_text(json.dumps({"source": str(source), "source_digest": source_digest,
                                                "original_counts": before}, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            return target, "migrated"
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
    from storage import init_db
    init_db(target)
    return target, "new"
