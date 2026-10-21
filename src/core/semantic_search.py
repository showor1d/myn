"""Local, rebuildable two-stage search for MYN memories.

BM25 retrieves literal matches. Small bilingual concept rules and character
ngrams rerank them and retrieve common paraphrases. This module is the only
ranking/index boundary; a local embedding backend can replace it later.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from contextlib import closing
from pathlib import Path

import data_location


INDEX_VERSION = 4
CONCEPTS = {
    "low_energy": r"状态不好|状态差|特别累|疲惫|疲劳|没精神|乏力|不想做|提不起劲|精疲力尽|exhausted|fatigue|tired|low energy|burned out|feeling down",
    "short_sleep": r"睡眠不足|没睡够|睡不够|睡得少|熬夜|失眠|睡不好|睡眠差|not enough sleep|sleep deprived|slept poorly|insomnia",
    "sleep": r"睡眠|睡觉|睡了|睡得|睡不好|失眠|熬夜|sleep|slept|insomnia",
    "creatine": r"肌酸|creatine|补剂|supplement",
    "workout": r"卧推|深蹲|硬拉|健身|训练|锻炼|运动|跑步|workout|exercise|gym|bench press|squat",
    "study": r"学习|学了|复习|数学|读书|考试|study|studied|learning|math|exam",
    "software_update": r"更新|升级|版本|补丁|release|software update|software release|updates?|upgrad|patch|version|changelog",
    "backup": r"备份|自动整理|整理备份|backup|cleanup|organization|organize",
    "review": r"回顾|总结|统计|复盘|review|summary|summaries|statistics|stats",
    "meals": r"早餐|早饭|午餐|午饭|吃饭|breakfast|lunch|meal",
    "missed_meal": r"可能错过|没吃|吃不上|错过|might miss|may miss|miss(?:ed|ing)? (?:breakfast|lunch|meal)",
    "wake_time": r"早起|起床|起得早|起得晚|起晚|睡懒觉|woke up|wake up|got up|get up",
}
QUESTION_WORDS = re.compile(r"我|上次|最近|什么时候|有没有|提过|记录|关于|请问|哪些|哪个|什么|时候|是否|的|了|吗|呢|有|过")
ENGLISH_STOP = {"i", "my", "me", "have", "has", "had", "did", "do", "does", "when",
                "what", "which", "was", "were", "about", "the", "a", "an", "and",
                "or", "to", "in", "on", "of", "for", "with", "any", "last", "recent",
                "recently", "ever", "said", "mention", "mentioned", "records"}
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")


def _index_path(db_path):
    return data_location.index_for_db(db_path)


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text):
    text = QUESTION_WORDS.sub(" ", _normalize(text))
    words = []
    for match in TOKEN_RE.finditer(text):
        part = match.group()
        if re.search(r"[\u3400-\u9fff]", part):
            words.extend(part[i:i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                words.append(part)
        elif part not in ENGLISH_STOP:
            words.append(part)
    return words


def _concepts(text, metadata=None):
    text = _normalize(text)
    found = {name for name, pattern in CONCEPTS.items() if re.search(pattern, text)}
    if metadata:
        try:
            hours = json.loads(metadata or "{}").get("sleep_hours")
            if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours < 6:
                found.update(("sleep", "short_sleep"))
        except (TypeError, ValueError):
            pass
    return sorted(found)


def _fingerprint(row):
    payload = [row["raw_text"], row["category"], row["metadata"]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def _document(row):
    concepts = set(_concepts(row["raw_text"], row["metadata"]))
    if row["category"] == "update":
        concepts.add("software_update")
    return {"fingerprint": _fingerprint(row), "terms": _terms(row["raw_text"]),
            "concepts": sorted(concepts)}


def _active_rows(db_path):
    import storage
    # The database, including soft deletion state, is always authoritative.
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT id, created_at, category, memory_type, raw_text, metadata FROM memories "
            "WHERE deleted_at IS NULL AND " + storage.FACTUAL_MEMORY + " ORDER BY id")]


def _load_index(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == INDEX_VERSION and isinstance(data.get("records"), dict):
            return data["records"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def _write_index(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".semantic-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump({"version": INDEX_VERSION, "method": "bm25-concepts-ngrams",
                       "records": records}, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_index(db_path, force=False):
    """Reconcile the disposable index with every currently active DB record."""
    rows = _active_rows(db_path)
    path = _index_path(db_path)
    old = {} if force else _load_index(path)
    records = {}
    for row in rows:
        key = str(row["id"])
        entry = old.get(key)
        if not isinstance(entry, dict) or entry.get("fingerprint") != _fingerprint(row) \
                or not isinstance(entry.get("terms"), list) or not isinstance(entry.get("concepts"), list):
            entry = _document(row)
        records[key] = entry
    if force or not path.exists() or records != old:
        _write_index(path, records)
    return rows, records


def reindex(db_path):
    rows, _ = sync_index(db_path, force=True)
    return len(rows), _index_path(db_path)


def _bm25(query_terms, records):
    lengths = [len(item["terms"]) for item in records.values()]
    count = len(lengths)
    if not count:
        return {}
    avg_len = sum(lengths) / count or 1
    frequencies = Counter(term for item in records.values() for term in set(item["terms"]))
    scores = {}
    for key, item in records.items():
        doc_terms = Counter(item["terms"])
        score = 0.0
        for term in set(query_terms):
            frequency = doc_terms.get(term, 0)
            if frequency:
                inverse = math.log(1 + (count - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * len(item["terms"]) / avg_len))
        scores[key] = score
    return scores


def _ngrams(terms):
    return set(terms)


def _rank(rows, records, query_text, limit=20, exclude_id=None, query_metadata=None):
    if limit < 1:
        raise ValueError("Limit must be at least 1.")
    query_terms = _terms(query_text)
    query_concepts = set(_concepts(query_text, query_metadata))
    if not query_terms and not query_concepts:
        return []
    bm25 = _bm25(query_terms, records)
    # Stage one: all literal hits plus concept matches. The latter catches
    # paraphrases that have no shared word or character pair.
    candidates = {key for key, score in bm25.items() if score > 0}
    candidates.update(key for key, item in records.items()
                      if query_concepts.intersection(item["concepts"]))
    ranked = []
    query_ng = _ngrams(query_terms)
    for row in rows:
        if row["id"] == exclude_id:
            continue
        key = str(row["id"])
        if key not in candidates:
            continue
        item = records[key]
        lexical = bm25[key] / (1 + bm25[key])
        overlap = len(query_concepts.intersection(item["concepts"]))
        semantic = overlap / len(query_concepts) if query_concepts else 0
        # A short focused memory should outrank a long release note that only
        # happens to mention the same topic in a list.
        specificity = .6 + .4 / (1 + max(0, len(item["terms"]) - 2) * .08)
        doc_ng = _ngrams(item["terms"])
        ngram = len(query_ng.intersection(doc_ng)) / len(query_ng.union(doc_ng)) if query_ng and doc_ng else 0
        score = .55 * lexical + .40 * semantic * specificity + .05 * ngram
        if score >= .08:
            ranked.append(dict(row, score=round(score, 3)))
    ranked.sort(key=lambda row: (-row["score"], -row["id"]))
    return ranked[:limit]


def ask(query, db_path, limit=20):
    if not query.strip():
        raise ValueError("Search text cannot be empty.")
    rows, records = sync_index(db_path)
    return _rank(rows, records, query, limit)


def similar(memory_id, db_path, limit=20):
    if memory_id < 1:
        raise ValueError("ID must be a positive number.")
    rows, records = sync_index(db_path)
    source = next((row for row in rows if row["id"] == memory_id), None)
    if source is None:
        raise ValueError("No active memory with ID {}.".format(memory_id))
    return _rank(rows, records, source["raw_text"], limit, memory_id, source["metadata"])
