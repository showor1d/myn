"""Private SQLite storage, migration, and rule extraction for Myn."""

import json
import math
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import data_location

DATA_DIR = data_location.database_dir()
DEFAULT_DB = DATA_DIR / "jarvis.db"
EXPORT_DIR = data_location.exports_for_db(DEFAULT_DB)
FIELDS = "id, content, created_at, category, raw_text, memory_type, metadata, deleted_at, brain_data, supersedes_id, source_message_id"
CATEGORIES = ("study", "health", "fitness", "goal", "idea", "people", "life", "update", "other")
# Kept legacy inference rows are available by ID and in the analysis collection,
# but never enter factual lists/search/semantic indexes.
FACTUAL_MEMORY = ("COALESCE(json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END,"
                  "'$.kind'),'') != 'inference' AND COALESCE(raw_text,'') NOT LIKE '（推断）%' AND COALESCE(raw_text,'') NOT LIKE '(Inference)%'")

UPDATE_TERMS = (
    "系统更新", "windows更新", "ios更新", "软件更新", "版本升级", "补丁",
    "固件更新", "驱动更新", "myn更新", "system update", "windows update",
    "ios update", "software update", "version upgrade", "firmware update",
    "driver update", "myn update", "patch",
)

UPDATE_ACTIONS = re.compile(
    r"\b(?:added|add|updated|update|upgraded|upgrade|fixed|fix|changed|change|"
    r"renamed|rename|grouped|cleanup)\b|\b(?:new category|backup organization|"
    r"project name|update summary)\b"
)
SOFTWARE_CONTEXT = re.compile(
    r"\b(?:myn|windows|ios|system|software|app|application)\b|系统|软件|程序|固件|驱动"
)
VERSION_MARKER = re.compile(
    r"\bv\d+(?:\.\d+)*\b|\b(?:version|changelog|release)\b|版本"
)

CATEGORY_TERMS = {
    "study": ("学习", "学了", "复习", "考试", "作业", "数学", "英语", "课程", "读书",
              "study", "studied", "learn", "learned", "exam", "homework", "class"),
    "health": ("健康", "睡眠", "睡了", "睡觉", "失眠", "生病", "医院", "医生", "药", "体重", "肌酸", "饮食",
               "health", "sleep", "slept", "ill", "doctor", "medicine", "weight", "creatine"),
    "fitness": ("健身", "训练", "锻炼", "运动", "卧推", "深蹲", "硬拉", "跑步", "举重", "游泳", "瑜伽",
                "fitness", "workout", "exercise", "gym", "bench press", "squat", "deadlift", "run", "running"),
    "goal": ("目标", "计划", "希望", "打算", "争取", "要做到",
             "goal", "plan", "aim", "target", "want to", "intend"),
    "idea": ("想法", "创意", "点子", "灵感", "构思", "建议",
             "idea", "thought", "inspiration", "brainstorm", "concept"),
    "people": ("朋友", "家人", "同事", "同学", "妈妈", "爸爸", "老师", "伴侣", "客户",
               "friend", "family", "colleague", "coworker", "teacher", "partner", "client"),
    "life": ("生活", "旅行", "旅游", "搬家", "做饭", "购物", "家务", "生日", "假期",
             "life", "travel", "trip", "cooking", "shopping", "birthday", "holiday"),
}
NUMBER = r"(\d+(?:\.\d+)?)"
KG = r"\s*(?:kg(?![a-z])|公斤|千克)"
G = r"\s*(?:g(?![a-z])|克)"
EXERCISES = ("卧推", "深蹲", "硬拉", "跑步", "举重", "游泳", "瑜伽",
             "bench press", "squat", "deadlift", "running", "run")


def is_explicit_update_log(content):
    """Recognize an intentional update label or a MYN version log heading."""
    text = content.casefold()
    return bool(re.match(
        r"^\s*(?:\[update\](?:\s|$)|update\s*[:：]|"
        r"myn\s*v\d+(?:\.\d+)*\b(?:\s*[:：—-]|\s+(?:added|updated|fixed|released|"
        r"upgraded|changed|新增|更新|修复|发布|升级)))", text))


def is_one_off_daily_event(content):
    """Recognize a transient wake-up/meal concern without inventing a habit."""
    text = content.casefold()
    if re.search(r"每天|通常|经常|总是|一直|习惯|常常|每次|\b(?:usually|often|always|every day|habitually|habit)\b", text):
        return False
    if re.search(r"计划|目标|打算|希望|想要|争取|\b(?:plan to|want to|intend to|aim to|goal)\b", text):
        return False
    late_waking = bool(re.search(
        r"(?:今天|今早|早上|这次).{0,16}(?:起床|起晚).{0,8}(?:晚|迟|了)|"
        r"(?:起床晚了|起晚了|睡过头了)|"
        r"\b(?:woke|got) up late\b|\b(?:overslept|slept in)\b", text))
    possible_missed_meal = bool(re.search(
        r"(?:可能|也许|恐怕|快要|要).{0,18}(?:错过|吃不上|来不及吃|不吃).{0,12}(?:早餐|早饭|午餐|午饭|晚餐|晚饭|一餐)|"
        r"\b(?:might|may|could|probably|possibly)\b.{0,24}\b(?:miss|skip)\b.{0,24}\b(?:breakfast|lunch|dinner|meal)\b", text))
    return late_waking or possible_missed_meal


def is_explicit_daily_habit(content):
    text = content.casefold()
    recurring = re.search(r"每天|通常|经常|总是|一直|习惯|常常|每次|\b(?:usually|often|always|every day|habitually|habit)\b", text)
    daily_topic = re.search(r"起床|起晚|早饭|早餐|午饭|午餐|晚饭|晚餐|\b(?:wake up|get up|woke up|got up|breakfast|lunch|dinner)\b", text)
    goal = re.search(r"计划|目标|打算|希望|想要|争取|\b(?:plan to|want to|intend to|aim to|goal)\b", text)
    return bool(recurring and daily_topic and not goal)


def is_explicit_daily_goal(content):
    text = content.casefold()
    goal = re.search(r"计划|目标|打算|想要|争取|\b(?:plan to|want to|intend to|aim to|goal)\b", text)
    daily_topic = re.search(r"起床|起晚|早饭|早餐|午饭|午餐|晚饭|晚餐|\b(?:wake up|get up|woke up|got up|breakfast|lunch|dinner)\b", text)
    return bool(goal and daily_topic)


def classify_memory(content):
    text = content.casefold()
    if is_explicit_update_log(content):
        return "update"
    if re.search(r"\bmyn\s*v\d+(?:\.\d+)*\b", text):
        return "update"
    # An update can also mention a study, health, or other category.
    has_update_term = any(
        re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text)
        if re.search(r"[a-z]", term) else term in text
        for term in UPDATE_TERMS
    )
    if (has_update_term or re.search(r"(?:windows|ios|myn)\s*更新", text)
            or (SOFTWARE_CONTEXT.search(text) and
                (UPDATE_ACTIONS.search(text) or VERSION_MARKER.search(text)))):
        return "update"
    if is_one_off_daily_event(content):
        return "life"
    if is_explicit_daily_goal(content):
        return "goal"
    for category, terms in CATEGORY_TERMS.items():
        for term in terms:
            if re.search(r"[a-z]", term):
                if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text):
                    return category
            elif term in text:
                return category
    if re.search(r"起床|起晚|早饭|早餐|午饭|午餐|晚饭|晚餐|\b(?:wake up|get up|woke up|got up|breakfast|lunch|dinner)\b", text):
        return "life"
    return "other"


def classify_type(content):
    text = content.casefold()
    if is_one_off_daily_event(content):
        return "event"
    if is_explicit_daily_goal(content):
        return "event"
    if is_explicit_daily_habit(content):
        return "profile"
    if re.search(r"目标|计划|打算|争取|要做到|\b(?:goal|plan to|want to|intend to|aim to)\b", text):
        return "event"
    if re.search(r"今天|昨天|前天|刚才|刚刚|这次|今晚|今早|today|yesterday|last night", text):
        return "event"
    if re.search(r"我的|我叫|我是|我体重|我每天|我通常|我喜欢|每天|通常|一直|my\s+(?:name|weight)|i\s+(?:am|weigh|like)|every\s+day|usually", text):
        return "profile"
    return "event"


def _number(value):
    result = float(value)
    return int(result) if result.is_integer() else result


def _find_number(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _number(match.group(1))
    return None


def extract_metadata(content):
    """Conservative extraction; raw_text remains the source of truth."""
    text = content.casefold()
    data = {}
    weight = _find_number(text, (
        r"(?:体重|weight|weigh)(?:是|约|大概|为|[:：=]|\s)*" + NUMBER + KG,
        NUMBER + KG + r"\s*(?:体重|bodyweight)",
    ))
    if weight is not None:
        data["weight_kg"] = weight
    creatine = _find_number(text, (
        r"(?:肌酸|creatine)(?:是|吃了|补了|约|[:：=]|\s)*" + NUMBER + G,
        NUMBER + G + r"\s*(?:肌酸|creatine)",
    ))
    if creatine is not None:
        data["creatine_g"] = creatine
    sleep = _find_number(text, (
        r"(?:睡了|睡眠|睡觉|sleep|slept)(?:约|了|for|[:：=]|\s)*" + NUMBER + r"\s*(?:小时|个小时|h\b|hours?\b)",
    ))
    if sleep is not None:
        data["sleep_hours"] = sleep
    clauses = re.split(r"[，,。；;\n]", text)
    for clause in clauses:
        if re.search(r"学习|学了|复习|数学|英语|读书|study|studied|learned|homework", clause):
            minutes = _find_number(clause, (NUMBER + r"\s*(?:分钟|分\b|min(?:ute)?s?\b)",))
            if minutes is None:
                hours = _find_number(clause, (NUMBER + r"\s*(?:小时|个小时|h\b|hours?\b)",))
                minutes = hours * 60 if hours is not None else None
            if minutes is not None:
                data["study_minutes"] = minutes
                break
    for clause in clauses:
        exercise = next((term for term in EXERCISES if re.search(
            r"(?<![a-z])" + re.escape(term) + r"(?![a-z])" if term[0].isascii() else re.escape(term),
            clause)), None)
        if exercise:
            data["exercise"] = exercise
            workout_weight = _find_number(clause, (
                re.escape(exercise) + r".{0,20}?" + NUMBER + KG,
            ))
            if workout_weight is not None:
                data["workout_weight_kg"] = workout_weight
            reps = _find_number(clause, (NUMBER + r"\s*(?:次|reps?\b)",))
            sets = _find_number(clause, (NUMBER + r"\s*(?:组|sets?\b)",))
            if reps is not None:
                data["reps"] = reps
            if sets is not None:
                data["sets"] = sets
            break
    return data


def _backup_before_migration(connection, db_path):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise sqlite3.DatabaseError("Source database integrity check failed")
    row_count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    has_messages = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='sent_messages'").fetchone() is not None
    message_count = (connection.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0]
                     if has_messages else None)
    base = db_path.with_name(db_path.name + ".pre-myn-v0.13-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup_path = Path(str(base) + ".bak")
    index = 1
    while backup_path.exists():
        backup_path = Path(str(base) + "-" + str(index) + ".bak")
        index += 1
    with closing(sqlite3.connect(str(backup_path))) as backup:
        connection.backup(backup)
    with closing(sqlite3.connect("file:" + backup_path.resolve().as_posix() + "?mode=ro", uri=True)) as verified:
        if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                verified.execute("SELECT COUNT(*) FROM memories").fetchone()[0] != row_count or \
                (message_count is not None and verified.execute(
                    "SELECT COUNT(*) FROM sent_messages").fetchone()[0] != message_count):
            raise sqlite3.DatabaseError("Migration backup verification failed")
    return backup_path


def init_db(db_path=DEFAULT_DB):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db_path))) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
        if not exists:
            with connection:
                connection.execute("""CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL, created_at TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    raw_text TEXT NOT NULL, memory_type TEXT NOT NULL DEFAULT 'event',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    deleted_at TEXT DEFAULT NULL,
                    trash_managed_at TEXT DEFAULT NULL,
                    brain_data TEXT NOT NULL DEFAULT '{}',
                    supersedes_id INTEGER DEFAULT NULL,
                    source_message_id INTEGER DEFAULT NULL
                )""")
        else:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
            missing = {"category", "raw_text", "memory_type", "metadata", "deleted_at",
                       "brain_data", "supersedes_id", "trash_managed_at", "source_message_id"} - columns
            if missing:
                _backup_before_migration(connection, db_path)
                with connection:
                    if "category" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")
                        old_rows = connection.execute("SELECT id, content FROM memories").fetchall()
                        connection.executemany("UPDATE memories SET category=? WHERE id=?",
                                               ((classify_memory(content), memory_id) for memory_id, content in old_rows))
                    if "raw_text" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN raw_text TEXT")
                    if "memory_type" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'event'")
                    if "metadata" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
                    if "deleted_at" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN deleted_at TEXT DEFAULT NULL")
                    if "trash_managed_at" in missing:
                        # Pre-existing deleted rows are personal data. They remain restorable
                        # until the owner explicitly deletes them under the new policy.
                        connection.execute("ALTER TABLE memories ADD COLUMN trash_managed_at TEXT DEFAULT NULL")
                    if "brain_data" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN brain_data TEXT NOT NULL DEFAULT '{}'")
                    if "supersedes_id" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN supersedes_id INTEGER DEFAULT NULL")
                    if "source_message_id" in missing:
                        connection.execute("ALTER TABLE memories ADD COLUMN source_message_id INTEGER DEFAULT NULL")
                    new_fields = [field for field in ("raw_text", "memory_type", "metadata") if field in missing]
                    if new_fields:
                        assignments = ", ".join(field + "=?" for field in new_fields)
                        old_rows = connection.execute("SELECT id, content FROM memories").fetchall()
                        values = []
                        for memory_id, content in old_rows:
                            derived = {
                                "raw_text": content,
                                "memory_type": classify_type(content),
                                "metadata": json.dumps(extract_metadata(content), ensure_ascii=False),
                            }
                            values.append(tuple(derived[field] for field in new_fields) + (memory_id,))
                        connection.executemany("UPDATE memories SET " + assignments + " WHERE id=?", values)
        with connection:
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_source_message_id "
                               "ON memories(source_message_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS memory_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                at_utc TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                actor TEXT NOT NULL
            )""")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("Database integrity check failed")
    return db_path


def _refresh_search_index(db_path):
    # Kept separate from the facts table; every search also reconciles the
    # index, so an interrupted write can be repaired from the database.
    from semantic_search import sync_index
    sync_index(db_path)


def _audit_memory(connection, memory_id, action, before=None, after=None, actor="user"):
    """Audit the same transaction as a memory change."""
    from datetime import timezone
    connection.execute("""INSERT INTO memory_audit
        (memory_id,action,at_utc,before_json,after_json,actor) VALUES(?,?,?,?,?,?)""",
        (memory_id, action, datetime.now(timezone.utc).isoformat(),
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None, actor))


def _audit_row(connection, memory_id):
    row = connection.execute("SELECT raw_text,category,memory_type,deleted_at,source_message_id "
                             "FROM memories WHERE id=?", (memory_id,)).fetchone()
    return dict(zip(("raw_text", "category", "memory_type", "deleted_at", "source_message_id"), row)) if row else None


def _analysis_json(parsed):
    analysis = {key: parsed[key] for key in
                       ("should_save", "category", "memory_type", "summary", "tags", "reason",
                        "source", "fallback_reason", "suggested_replaces_id")}
    for key in ("category_policy", "manual_category", "manual_type"):
        if key in parsed:
            analysis[key] = parsed[key]
    return json.dumps(analysis, ensure_ascii=False)


def _local_classification(content):
    """Safe rule fallback with no automatic profile or summary extraction."""
    return {"should_save": True, "category": classify_memory(content),
            "memory_type": "event", "metadata": {}, "summary": "", "tags": [],
            "reason": "", "source": "rule", "fallback_reason": "claude_unavailable_or_unconfigured",
            "suggested_replaces_id": None}


def save_memory(content, db_path=DEFAULT_DB, parsed=None):
    content = content.strip()
    if not content:
        raise ValueError("Memory text cannot be empty.")
    from message_log import is_sensitive
    if is_sensitive(content):
        raise ValueError('Possible key or password detected; not saved as a memory.')
    if parsed is None:
        parsed = _local_classification(content)
    if not parsed["should_save"]:
        return None
    init_db(db_path)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            cursor = connection.execute(
                """INSERT INTO memories (content, created_at, category, raw_text, memory_type,
                   metadata, brain_data) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (content, created_at, parsed["category"], content, parsed["memory_type"],
                 json.dumps(parsed["metadata"], ensure_ascii=False), _analysis_json(parsed)))
            memory_id = cursor.lastrowid
            _audit_memory(connection, memory_id, "create", after=_audit_row(connection, memory_id))
    _refresh_search_index(db_path)
    return memory_id


def edit_memory(memory_id, content, db_path=DEFAULT_DB, expected_raw_text=None, parsed=None):
    content = content.strip()
    if not content:
        raise ValueError("Memory text cannot be empty.")
    from message_log import is_sensitive
    if is_sensitive(content):
        raise ValueError('Possible key or password detected; not saved as a memory.')
    if parsed is None:
        parsed = _local_classification(content)
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            before = _audit_row(connection, memory_id)
            previous = connection.execute(
                "SELECT category, memory_type, brain_data FROM memories WHERE id=? AND deleted_at IS NULL",
                (memory_id,)).fetchone()
            if previous:
                try:
                    prior_analysis = json.loads(previous[2] or "{}")
                except (ValueError, TypeError):
                    prior_analysis = {}
                if not isinstance(prior_analysis, dict):
                    prior_analysis = {}
                manual_category = prior_analysis.get("manual_category")
                # Older category commands changed the stored category only.
                if manual_category not in CATEGORIES and prior_analysis.get("category") in CATEGORIES \
                        and prior_analysis["category"] != previous[0]:
                    manual_category = previous[0]
                if manual_category in CATEGORIES:
                    parsed = dict(parsed, manual_category=manual_category)
                manual_type = prior_analysis.get("manual_type")
                if manual_type not in ("event", "profile", "goal") and prior_analysis.get("memory_type") in ("event", "profile", "goal") \
                        and prior_analysis["memory_type"] != previous[1]:
                    manual_type = previous[1]
                if manual_type in ("event", "profile", "goal"):
                    parsed = dict(parsed, manual_type=manual_type)
            condition = " AND raw_text=?" if expected_raw_text is not None else ""
            params = (content, content, parsed.get("manual_category", parsed["category"]),
                      parsed.get("manual_type", parsed["memory_type"]),
                      json.dumps(parsed["metadata"], ensure_ascii=False), _analysis_json(parsed), memory_id)
            if expected_raw_text is not None:
                params += (expected_raw_text,)
            cursor = connection.execute(
                """UPDATE memories SET content=?, raw_text=?, category=?, memory_type=?, metadata=?, brain_data=?
                   WHERE id=? AND deleted_at IS NULL""" + condition, params)
            changed = cursor.rowcount == 1
            if changed:
                _audit_memory(connection, memory_id, "edit", before, _audit_row(connection, memory_id))
    if changed:
        _refresh_search_index(db_path)
    return changed


def supersede_memory(new_id, old_id, db_path=DEFAULT_DB):
    """Record an explicit, traceable profile relationship; retain both records."""
    if new_id <= old_id or old_id < 1:
        raise ValueError("Use a newer profile ID followed by an older profile ID.")
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            rows = connection.execute(
                "SELECT id, memory_type, deleted_at FROM memories WHERE id IN (?, ?)",
                (new_id, old_id)).fetchall()
            if len(rows) != 2 or any(row[1] != "profile" or row[2] is not None for row in rows):
                raise ValueError("Both IDs must be active profile memories.")
            cursor = connection.execute(
                "UPDATE memories SET supersedes_id=? WHERE id=? AND supersedes_id IS NULL",
                (old_id, new_id))
            if cursor.rowcount != 1:
                raise ValueError("This memory already supersedes another record.")
            _audit_memory(connection, new_id, "supersede", {"supersedes_id": None},
                          {"supersedes_id": old_id})
    return True


def delete_memory(memory_id, db_path=DEFAULT_DB, expected_raw_text=None):
    init_db(db_path)
    deleted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            before = _audit_row(connection, memory_id)
            condition = " AND raw_text=?" if expected_raw_text is not None else ""
            params = (deleted_at, memory_id)
            if expected_raw_text is not None:
                params += (expected_raw_text,)
            cursor = connection.execute(
                "UPDATE memories SET deleted_at=?, trash_managed_at=? WHERE id=? AND deleted_at IS NULL" + condition,
                (deleted_at,) + params)
            changed = cursor.rowcount == 1
            if changed:
                _audit_memory(connection, memory_id, "delete", before, _audit_row(connection, memory_id))
    if changed:
        _refresh_search_index(db_path)
    return changed


def restore_memory(memory_id, db_path=DEFAULT_DB, expected_deleted_at=None):
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            before = _audit_row(connection, memory_id)
            row = connection.execute("SELECT trash_managed_at FROM memories WHERE id=? AND deleted_at IS NOT NULL",
                                     (memory_id,)).fetchone()
            if row is None:
                return False
            if row[0] is not None and datetime.fromisoformat(row[0]).astimezone() <= \
                    datetime.now().astimezone() - timedelta(days=7):
                return False
            condition = " AND deleted_at=?" if expected_deleted_at is not None else ""
            params = (memory_id,)
            if expected_deleted_at is not None:
                params += (expected_deleted_at,)
            cursor = connection.execute(
                "UPDATE memories SET deleted_at=NULL, trash_managed_at=NULL WHERE id=? AND deleted_at IS NOT NULL" + condition,
                params)
            changed = cursor.rowcount == 1
            if changed:
                _audit_memory(connection, memory_id, "restore", before, _audit_row(connection, memory_id))
    if changed:
        _refresh_search_index(db_path)
    return changed


def trash_memories(db_path=DEFAULT_DB, limit=100):
    return _rows(db_path, "SELECT " + FIELDS + ", trash_managed_at FROM memories "
                 "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?", (limit,))


def category_memories(category, db_path=DEFAULT_DB, limit=100):
    if category not in CATEGORIES:
        raise ValueError("Unknown category")
    return _rows(db_path, "SELECT " + FIELDS + " FROM memories "
                 "WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY + " AND category=? ORDER BY id DESC LIMIT ?",
                 (category, limit))


def purge_expired_trash(db_path=DEFAULT_DB):
    """Only v0.13 managed deletions expire; historical deleted data is preserved."""
    init_db(db_path)
    cutoff = datetime.now().astimezone() - timedelta(days=7)
    with closing(sqlite3.connect(str(db_path))) as connection:
        rows = connection.execute("SELECT id, trash_managed_at FROM memories "
                                  "WHERE deleted_at IS NOT NULL AND trash_managed_at IS NOT NULL").fetchall()
        expired = []
        for memory_id, stamp in rows:
            try:
                if datetime.fromisoformat(stamp).astimezone() <= cutoff:
                    expired.append((memory_id, stamp))
            except ValueError:
                # An invalid timestamp never authorizes destruction.
                continue
        if expired:
            from backup import backup_database
            backup_database(db_path, label="pre-purge")
            with connection:
                connection.executemany("DELETE FROM memories WHERE id=? AND trash_managed_at=? "
                                       "AND deleted_at IS NOT NULL", expired)
    if expired:
        _refresh_search_index(db_path)
    return len(expired)


def move_memory(memory_id, new_category, db_path=DEFAULT_DB, expected_category=None,
                expected_raw_text=None):
    if memory_id < 1:
        raise ValueError("ID must be a positive number.")
    if new_category not in CATEGORIES:
        raise ValueError("Category must be one of: " + ", ".join(CATEGORIES))
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            before = _audit_row(connection, memory_id)
            row = connection.execute(
                "SELECT brain_data FROM memories WHERE id=? AND deleted_at IS NULL", (memory_id,)).fetchone()
            if row is None:
                return False
            try:
                analysis = json.loads(row[0] or "{}")
            except (ValueError, TypeError):
                analysis = {}
            if not isinstance(analysis, dict):
                analysis = {}
            analysis["manual_category"] = new_category
            condition = ""
            params = [new_category, json.dumps(analysis, ensure_ascii=False), memory_id]
            if expected_category is not None:
                condition += " AND category=?"
                params.append(expected_category)
            if expected_raw_text is not None:
                condition += " AND raw_text=?"
                params.append(expected_raw_text)
            cursor = connection.execute(
                "UPDATE memories SET category=?, brain_data=? WHERE id=? AND deleted_at IS NULL" + condition,
                params)
            changed = cursor.rowcount == 1
            if changed:
                _audit_memory(connection, memory_id, "category", before, _audit_row(connection, memory_id))
    if changed:
        _refresh_search_index(db_path)
    return changed


def set_memory_type(memory_id, new_type, db_path=DEFAULT_DB, expected_type=None,
                    expected_raw_text=None):
    if memory_id < 1:
        raise ValueError("ID must be a positive number.")
    if new_type not in ("event", "profile", "goal"):
        raise ValueError("Type must be event, profile or goal.")
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        with connection:
            before = _audit_row(connection, memory_id)
            row = connection.execute(
                "SELECT brain_data FROM memories WHERE id=? AND deleted_at IS NULL", (memory_id,)).fetchone()
            if row is None:
                return False
            try:
                analysis = json.loads(row[0] or "{}")
            except (ValueError, TypeError):
                analysis = {}
            if not isinstance(analysis, dict):
                analysis = {}
            analysis["manual_type"] = new_type
            analysis["suggested_replaces_id"] = None
            condition = ""
            params = [new_type, json.dumps(analysis, ensure_ascii=False), memory_id]
            if expected_type is not None:
                condition += " AND memory_type=?"
                params.append(expected_type)
            if expected_raw_text is not None:
                condition += " AND raw_text=?"
                params.append(expected_raw_text)
            cursor = connection.execute(
                "UPDATE memories SET memory_type=?, brain_data=? WHERE id=? AND deleted_at IS NULL" + condition,
                params)
            changed = cursor.rowcount == 1
            if changed:
                _audit_memory(connection, memory_id, "type", before, _audit_row(connection, memory_id))
    if changed:
        _refresh_search_index(db_path)
    return changed


def _rows(db_path, query, params=()):
    init_db(db_path)
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def search_memories(query, db_path=DEFAULT_DB, limit=20):
    query = query.strip()
    if not query:
        raise ValueError("Search text cannot be empty.")
    if limit < 1:
        raise ValueError("Limit must be at least 1.")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _rows(db_path, "SELECT " + FIELDS + " FROM memories WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY + " AND raw_text LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                 ("%" + escaped + "%", limit))


def memories_in_saved_range(start_at, end_at, db_path=DEFAULT_DB, category=None,
                            memory_type=None, limit=100):
    """Query active memories by absolute instants, not date-shaped text."""
    if start_at.tzinfo is None or end_at.tzinfo is None or start_at >= end_at:
        raise ValueError("Saved time range must have ordered timezone-aware endpoints")
    if category is not None and category not in CATEGORIES:
        raise ValueError("Invalid memory category")
    if memory_type is not None and memory_type not in ("event", "profile", "goal"):
        raise ValueError("Invalid memory type")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid result limit")
    query = ("SELECT " + FIELDS + " FROM memories WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY + " "
             "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)")
    params = [start_at.isoformat(), end_at.isoformat()]
    if category is not None:
        query += " AND category=?"
        params.append(category)
    if memory_type is not None:
        query += " AND memory_type=?"
        params.append(memory_type)
    query += " ORDER BY julianday(created_at), id LIMIT ?"
    params.append(limit)
    return _rows(db_path, query, tuple(params))


def recent_memories(db_path=DEFAULT_DB, limit=20):
    if limit < 1:
        raise ValueError("Limit must be at least 1.")
    return _rows(db_path, "SELECT " + FIELDS + " FROM memories WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY + " ORDER BY id DESC LIMIT ?", (limit,))


def today_memories(db_path=DEFAULT_DB):
    today = datetime.now(data_location.TOKYO).date()
    return [row for row in _dated_memories(db_path)
            if datetime.fromisoformat(row["created_at"]).astimezone(data_location.TOKYO).date() == today]


def week_memories(db_path=DEFAULT_DB):
    today = datetime.now(data_location.TOKYO).date()
    monday = today.toordinal() - today.weekday()
    return [row for row in _dated_memories(db_path)
            if monday <= datetime.fromisoformat(row["created_at"]).astimezone(data_location.TOKYO).date().toordinal() < monday + 7]


def month_memories(db_path=DEFAULT_DB):
    today = datetime.now(data_location.TOKYO).date()
    return [row for row in _dated_memories(db_path)
            if datetime.fromisoformat(row["created_at"]).astimezone(data_location.TOKYO).date().year == today.year
            and datetime.fromisoformat(row["created_at"]).astimezone(data_location.TOKYO).date().month == today.month]


def year_memories(db_path=DEFAULT_DB):
    year = datetime.now(data_location.TOKYO).date().year
    return [row for row in _dated_memories(db_path)
            if datetime.fromisoformat(row["created_at"]).astimezone(data_location.TOKYO).date().year == year]


def _metadata(row):
    try:
        value = json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _metric_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def review_stats(rows, week_rows=None):
    """Summarize active rows using stored metadata, without changing the database."""
    weights = []
    creatine_records = 0
    study_minutes = 0
    study_records = 0
    workout_records = 0
    exercises = Counter()
    categories = Counter()
    for row in rows:
        categories[row["category"]] += 1
        data = _metadata(row)
        weight = _metric_number(data.get("weight_kg"))
        if weight is not None:
            weights.append(weight)
        if _metric_number(data.get("creatine_g")) is not None:
            creatine_records += 1
        minutes = _metric_number(data.get("study_minutes"))
        if minutes is not None:
            study_minutes += minutes
            study_records += 1
        exercise = data.get("exercise")
        if isinstance(exercise, str) and exercise.strip():
            workout_records += 1
            exercises[exercise.strip()] += 1
    weekly_weights = [_metric_number(_metadata(row).get("weight_kg")) for row in (week_rows or [])]
    weekly_weights = [weight for weight in weekly_weights if weight is not None]
    return {
        "records": len(rows), "categories": categories,
        "latest_weight_kg": weights[-1] if weights else None,
        "week_weight_change_kg": (weekly_weights[-1] - weekly_weights[0]
                                  if len(weekly_weights) >= 2 else None),
        "creatine_records": creatine_records, "study_minutes": study_minutes,
        "study_records": study_records, "workout_records": workout_records,
        "exercises": exercises, "update_records": categories["update"],
    }


def _dated_memories(db_path):
    rows = _rows(db_path, "SELECT " + FIELDS + " FROM memories WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY)
    return sorted(rows, key=lambda row: (datetime.fromisoformat(row["created_at"]).astimezone(), row["id"]))


def profile_memories(db_path=DEFAULT_DB):
    return _rows(db_path, "SELECT " + FIELDS + " FROM memories WHERE deleted_at IS NULL AND " + FACTUAL_MEMORY + " AND memory_type='profile' ORDER BY id DESC")


def get_memory(memory_id, db_path=DEFAULT_DB):
    if memory_id < 1:
        raise ValueError("ID must be a positive number.")
    rows = _rows(db_path, "SELECT " + FIELDS + " FROM memories WHERE id=?", (memory_id,))
    return rows[0] if rows else None


def export_memories(db_path=DEFAULT_DB, export_dir=EXPORT_DIR):
    rows = _rows(db_path, "SELECT " + FIELDS + " FROM memories ORDER BY id")
    memories = [{
        "id": row["id"], "created_at": row["created_at"], "category": row["category"],
        "memory_type": row["memory_type"], "raw_text": row["raw_text"],
        "deleted_at": row["deleted_at"],
        "metadata": json.loads(row["metadata"] or "{}"),
        "brain_data": json.loads(row["brain_data"] or "{}"),
        "supersedes_id": row["supersedes_id"]
    } for row in rows]
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {"format": "myn-memories", "version": 1, "exported_at": stamp, "memories": memories}
    markdown = ["# Myn memories", "", "Exported: " + stamp, ""]
    for memory in memories:
        markdown.extend([
            "## #{} — {}".format(memory["id"], memory["created_at"]), "",
            "- Type: " + memory["memory_type"],
            "- Category: " + memory["category"],
            "- Deleted: " + (memory["deleted_at"] or "no"),
            "- Fields: " + json.dumps(memory["metadata"], ensure_ascii=False), "",
            "- Brain: " + json.dumps(memory["brain_data"], ensure_ascii=False),
            "- Supersedes: " + str(memory["supersedes_id"] or "no"), "",
            memory["raw_text"], "",
        ])
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = export_dir / "memories.json", export_dir / "memories.md"
    json_tmp, md_tmp = export_dir / "memories.json.tmp", export_dir / "memories.md.tmp"
    try:
        json_tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_tmp.write_text("\n".join(markdown), encoding="utf-8")
        json_tmp.replace(json_path)
        md_tmp.replace(md_path)
    finally:
        json_tmp.unlink(missing_ok=True)
        md_tmp.unlink(missing_ok=True)
    return json_path, md_path, len(memories)
