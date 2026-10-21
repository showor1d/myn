"""Read-only model tools backed by MYN's existing journal, memories and grants."""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import message_log
import conversation_log
import memory_query
import storage
import data_location

GRANT_FILE = "file-access.json"
AUDIT_FILE = "tool-diagnostics.jsonl"
_LOCK = threading.Lock()
_SENSITIVE = {".env", "claude-key.dpapi", "elevenlabs-key.dpapi", "file-access.json"}
# Credential and per-user app-state folders are never readable through a broad grant.
_PROTECTED_DIRS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".config",
                   ".git", "appdata"}
_TEXT_SUFFIXES = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml",
                  ".toml", ".ini", ".cfg", ".html", ".css", ".js"}


def _protected_name(name):
    lowered = name.lower()
    return lowered in _SENSITIVE or lowered in _PROTECTED_DIRS or lowered.startswith(".env") or bool(re.search(
        r"(?:^|[-_.])(?:secret|credentials?|tokens?|api[-_]?keys?)(?:[-_.]|$)", lowered))

TOOLS = [
    {"name": "search_memories", "description": "Search active MYN memories. Dates in the query use Asia/Tokyo saved-time boundaries. Read only.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"},
                      "start_at": {"type": "string", "description": "Optional inclusive ISO timestamp with timezone"},
                      "end_at": {"type": "string", "description": "Optional exclusive ISO timestamp with timezone"},
                      "category": {"type": "string", "enum": list(storage.CATEGORIES)},
                      "memory_type": {"type": "string", "enum": ["event", "profile", "goal"]}},
                      "required": ["query"], "additionalProperties": False}},
    {"name": "search_history", "description": "Search saved user and assistant conversation messages, including Tokyo date windows. Read only.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False}},
    {"name": "history_by_time", "description": "List the saved conversation messages (user and MYN, with "
     "their Asia/Tokyo times) between two times, oldest first, at most 40. Use it for 'when did I last "
     "say…', 'what did we talk about last night', 'how long did I sleep' (the time of the last message "
     "before a long silence). Read only.",
     "input_schema": {"type": "object", "properties": {
         "start_at": {"type": "string", "description": "Inclusive ISO time with timezone, e.g. 2026-10-16T22:00:00+09:00"},
         "end_at": {"type": "string", "description": "Exclusive ISO time with timezone"}},
                      "required": ["start_at", "end_at"], "additionalProperties": False}},
    {"name": "list_files", "description": "List one directory only if the user confirmed MYN read access through computer_permissions.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"], "additionalProperties": False}},
    {"name": "read_file", "description": "Read a small UTF-8 text file only under a directory confirmed through computer_permissions.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"], "additionalProperties": False}},
]


BROAD_GRANT_ERROR = 'Do not authorize a whole drive, home directory or Users directory. Choose a specific working folder.'


def too_broad(directory):
    """A drive/filesystem root, the user's home, or the folder holding all homes."""
    path = Path(directory)
    if path.parent == path:
        return True
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    return path == home or path == home.parent


def _active_roots(data_dir):
    """Resolved read grants that are still usable; broad or missing ones are skipped."""
    roots = []
    for item in grants(data_dir)["read"]:
        try:
            root = Path(item).resolve(strict=True)
        except OSError:
            continue
        if root.is_dir() and not too_broad(root):
            roots.append(root)
    return roots


def _grant_path(data_dir):
    return Path(data_dir) / GRANT_FILE


def grants(data_dir):
    path = _grant_path(data_dir)
    if not path.exists():
        return {"read": [], "write": [], "delete": [], "execute": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(value.get(action), list)
                                          for action in ("read", "write", "delete", "execute")) or \
            any(not isinstance(item, str) for action in ("read", "write", "delete", "execute")
                for item in value[action]):
        raise ValueError('Invalid file access settings. Request authorization again with computer_permissions.')
    return value


def _save_grants(data_dir, data):
    """Write the grant file atomically: a crash mid-write must not leave a broken
    file, because a broken file switches off every file tool until it is fixed."""
    path = _grant_path(data_dir)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))


def grant_read(data_dir, directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('The authorization target is not a directory.')
    if too_broad(root):
        raise ValueError(BROAD_GRANT_ERROR)
    private = Path(data_dir).resolve()
    if root == private or private in root.parents:
        raise ValueError('MYN private data cannot be authorized as an ordinary file directory.')
    data = grants(data_dir)
    if str(root) not in data["read"]:
        data["read"].append(str(root))
    _save_grants(data_dir, data)


def revoke_read(data_dir, directory):
    data = grants(data_dir)
    data["read"] = [item for item in data["read"] if item != directory]
    _save_grants(data_dir, data)


def _authorized_path(data_dir, requested):
    if not isinstance(requested, str) or not requested or len(requested) > 1024:
        raise ValueError('Invalid path.')
    path = Path(requested)
    if not path.is_absolute():
        raise PermissionError('Provide an absolute path.')
    resolved = path.resolve()
    roots = _active_roots(data_dir)
    if not any(resolved == root or root in resolved.parents for root in roots):
        if any(too_broad(item) for item in grants(data_dir)["read"]):
            raise PermissionError('Whole-drive and home-directory access is disabled. Request access to a specific folder with computer_permissions.')
        raise PermissionError('This path is outside authorized read-only folders. Request authorization for its specific folder with computer_permissions.')
    resolved = path.resolve(strict=True)
    matching = [root for root in roots if resolved == root or root in resolved.parents]
    if not matching:
        raise PermissionError('The resolved path is outside authorized folders.')
    # Only names below the granted folder are screened, so an explicitly granted
    # folder that itself sits under AppData (e.g. Temp) remains usable.
    below = resolved.relative_to(max(matching, key=lambda root: len(root.parts))).parts
    # A broad directory grant must not expose MYN's credentials or private DB files.
    if Path(data_dir).resolve() == resolved or Path(data_dir).resolve() in resolved.parents:
        raise PermissionError('Read MYN data through memory or history tools, not ordinary file access.')
    if Path(data_dir).name == "config" and (Path(data_dir).parent.resolve() == resolved or
            Path(data_dir).parent.resolve() in resolved.parents):
        raise PermissionError('Read personal data through controlled storage tools, not ordinary file access.')
    if any(_protected_name(part) for part in below):
        raise PermissionError('The model cannot read credentials or authorization settings.')
    return resolved


def _audit(data_dir, name, ok, detail, count=0):
    record = {"at_utc": datetime.now(timezone.utc).isoformat(), "tool": name,
              "ok": ok, "detail": detail, "count": count}
    try:
        with _LOCK:
            with (Path(data_dir) / AUDIT_FILE).open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def execute(name, arguments, db_path, exclude_message_id=None):
    """Return (JSON result, is_error); never perform model-initiated mutations."""
    data_dir = data_location.config_for_db(db_path)
    try:
        if not isinstance(arguments, dict):
            raise ValueError('Invalid tool arguments.')
        if name in ("search_memories", "search_history"):
            allowed = ({"query", "start_at", "end_at", "category", "memory_type"}
                       if name == "search_memories" else {"query"})
            if set(arguments) - allowed:
                raise ValueError('Tool arguments contain unsupported fields.')
            query = arguments.get("query")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
                raise ValueError('Query must contain 1-120 characters.')
            if name == "search_memories":
                start, end = arguments.get("start_at"), arguments.get("end_at")
                if (start is None) != (end is None):
                    raise ValueError('Provide both start and end times.')
                if start is not None:
                    if not isinstance(start, str) or not isinstance(end, str):
                        raise ValueError('Invalid time range format.')
                    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
                rows = memory_query.retrieve(query, db_path, limit=6, start_at=start,
                                             end_at=end, category=arguments.get("category"),
                                             memory_type=arguments.get("memory_type"))
                result = [{"memory_id": row["id"], "raw_text": row["raw_text"][:700],
                           "saved_at": row["saved_at"],
                           "saved_local": conversation_log.local_time(row["saved_at"]),
                           "event_at": row["event_at"],
                           "confidence": row["confidence"],
                           "confidence_kind": row["confidence_kind"],
                           "category": row["category"],
                           "memory_type": row["memory_type"]} for row in rows
                          if not message_log.is_sensitive(row["raw_text"])]
            else:
                rows = conversation_log.search(db_path, query, limit=6,
                                               exclude_source_id=exclude_message_id)
                result = [{"id": row["id"], "time_local": conversation_log.local_time(row["created_at"], seconds=True),
                           "session_id": row["session_id"], "role": row["role"],
                           "text": row["display_text"][:700],
                           "confidence": row["confidence"],
                           "confidence_kind": row["confidence_kind"]} for row in rows]
            detail = "db_read"
        elif name == "history_by_time":
            if set(arguments) != {"start_at", "end_at"}:
                raise ValueError('start_at and end_at are required.')
            try:
                start = datetime.fromisoformat(str(arguments["start_at"]))
                end = datetime.fromisoformat(str(arguments["end_at"]))
            except ValueError:
                raise ValueError('Use an ISO timestamp with timezone, such as 2026-10-16T22:00:00+09:00.') from None
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError('Times must include timezones, and end must follow start.')
            result, more = conversation_log.messages_between(db_path, start, end)
            result = {"timezone": "Asia/Tokyo", "messages": result, "more_in_range": more}
            detail = "db_read"
        elif name in ("list_files", "read_file"):
            if set(arguments) != {"path"}:
                raise ValueError('Invalid tool arguments.')
            path = _authorized_path(data_dir, arguments.get("path"))
            if name == "list_files":
                if not path.is_dir():
                    raise ValueError('Target is not a directory.')
                result = [{"name": child.name, "directory": child.is_dir()}
                          for child in sorted(path.iterdir(), key=lambda p: p.name.casefold())[:100]
                          if not _protected_name(child.name)]
            else:
                if not path.is_file():
                    raise ValueError('Target is not a file.')
                if path.suffix.lower() not in _TEXT_SUFFIXES:
                    raise ValueError('Only common UTF-8 text files are supported.')
                if path.stat().st_size > 128 * 1024:
                    raise ValueError('File exceeds the 128 KiB read limit.')
                with path.open("r", encoding="utf-8", errors="strict") as source:
                    result = source.read(16001)
                if len(result) > 16000:
                    raise ValueError('File text exceeds the 16000-character read limit.')
            detail = str(path)
        else:
            raise PermissionError('This tool is unavailable. Writing, deletion and execution are separately prohibited.')
        count = len(result)
        _audit(data_dir, name, True, detail, count)
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False), False
    except (OSError, ValueError, PermissionError, UnicodeError, sqlite3.Error) as error:
        detail = "%s: %s" % (type(error).__name__, error)
        _audit(data_dir, name, False, detail)
        return json.dumps({"ok": False, "error": detail}, ensure_ascii=False), True
