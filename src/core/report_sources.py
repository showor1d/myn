"""Read-only, dated evidence for reports. Stored text is data, never instructions."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKYO = timezone(timedelta(hours=9))
TOPICS = {'updates': 'Feature updates', 'preferences': 'Communication preferences', 'tasks': 'Evolution tasks',
          'life': 'Personal records', 'study': 'Study', 'agenda': 'Calendar', 'expenses': 'Expenses'}


def moment(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=TOKYO) if result.tzinfo is None else result.astimezone(TOKYO)


def rows(path, sql, args=()):
    if not Path(path).exists():
        return []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def collect(config, db_path, topics, start, end):
    import message_log
    import data_location
    items, coverage = [], []

    def add(topic, source, ident, at, title, detail, status='recorded'):
        try:
            when = moment(at)
        except (ValueError, TypeError):
            coverage.append(TOPICS[topic] + ': Some records lack valid timestamps and were excluded from this period.')
            return
        if start <= when < end and not message_log.is_sensitive(str(title) + json.dumps(detail, ensure_ascii=False)):
            items.append(dict(topic=topic, source=source, source_id=str(ident), at=when.isoformat(),
                              title=str(title), status=status, detail=detail))

    for topic in topics:
        before = len(items)
        try:
            if topic == 'updates':
                for row in rows(Path(config) / 'reports.sqlite3', 'SELECT * FROM changes ORDER BY at'):
                    add(topic, 'development_change', row['id'], row['at'], row['text'],
                        {'stage': 'prepared', 'note': 'Developer-submitted change description; this does not prove the new version has started.'}, 'prepared')
                    if row['applied_at']:
                        add(topic, 'development_change', row['id'] + ':applied', row['applied_at'], row['text'],
                            {'stage': 'applied', 'prepared_at': row['at'],
                             'note': 'The new process has started; this does not prove all features were tested with live services.'}, 'applied')
                coverage.append('Feature updates: development history begins when tracking was enabled. Older manual edits lack complete history; absence does not prove no changes occurred.')
            if topic in ('updates', 'tasks'):
                import evolution
                for point in evolution.points(config):
                    for index, event in enumerate(point.get('history', [])):
                        add(topic, 'evolution_history', '%s:%d' % (point['id'], index), event['at'],
                            point['title'] + '：' + event['what'],
                            {'point_id': point['id'], 'current_status': point['status'],
                             'status_at_event': event.get('status'),
                             'note': 'current_status is the status at query time; status_at_event is historical and may be absent in older records. completed may have been set manually.'}, event.get('status','history'))
                coverage.append(TOPICS[topic] + ': Each evolution point retains at most 40 history entries; omitted history cannot be reconstructed.')
            elif topic == 'preferences':
                import preferences
                path = preferences.folder_for_db(db_path) / 'feedback.db'
                for row in rows(path, 'SELECT * FROM rule_history ORDER BY id'):
                    old, new = (json.loads(row[k] or '{}') for k in ('before', 'after'))
                    old, new = old or {}, new or {}
                    status = new.get('status') or 'deleted'
                    kind = ('New' if not old else 'Revoked or disabled' if status in ('deleted','retired','paused')
                            else 'Modify' if any(old.get(k) != new.get(k) for k in ('text','status','scenes','direction'))
                            else 'Learning reinforced')
                    detail = dict(before=old, after=new, action=row['action'], actor=row['actor'], note=row['note'],
                                  feedback_id=row['feedback_id'], note_status='candidate is tentative, not a confirmed active preference')
                    if row['feedback_id']:
                        detail['feedback'] = rows(path, 'SELECT * FROM feedback WHERE id=?', (row['feedback_id'],))
                    add(topic, 'preference_history', row['id'], row['at'],
                        kind + '：' + (new.get('text') or old.get('text') or ''), detail, status)
            elif topic in ('life', 'study'):
                # Raw user speech is evidence of what was said, not proof a planned event happened.
                sql = "SELECT id,raw_text,created_at,source_message_id FROM conversation_messages WHERE role='user' AND deleted_at IS NULL"
                if topic == 'study':
                    sql += " AND source_message_id IN (SELECT message_id FROM memory_evidence_links WHERE category='study')"
                sql += ' AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) ORDER BY julianday(created_at) DESC LIMIT 5001'
                originals = rows(db_path,sql,(start.isoformat(),end.isoformat()))
                annotations = {}
                for link in rows(db_path, '''SELECT e.* FROM memory_evidence_links e
                    WHERE e.message_id IN (SELECT source_message_id FROM conversation_messages
                    WHERE deleted_at IS NULL AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?))''',
                    (start.isoformat(),end.isoformat())):
                    annotations.setdefault(link['message_id'],[]).append(link)
                for row in originals:
                    add(topic, 'conversation', row['id'], row['created_at'], row['raw_text'],
                        {'original_text': row['raw_text'], 'time_basis': 'Message time, not event completion time',
                         'annotations':annotations.get(row['source_message_id'],[])}, 'user_statement')
                coverage.append(TOPICS[topic] + ': Includes saved original messages only. Study classification uses analyzed scenes and may omit unclassified records. Plans do not prove completion.')
            elif topic == 'agenda':
                import agenda
                for event in agenda.occurrences(config, start.replace(tzinfo=None), end.replace(tzinfo=None)):
                    add(topic, 'calendar', event['id'], event['start'].isoformat(), agenda.describe(event),
                        {'note': event.get('note', ''), 'repeat': event.get('repeat'),
                         'time_basis': 'Planned event time from the current calendar; not proof of execution'}, 'planned')
            elif topic == 'expenses':
                import expenses
                import usage_stats
                for row in expenses.records(config):
                    title = row['description'] + '：' + expenses.money(row['amount_minor'],row['currency']) + '（' + expenses.money(row['usd'],'USD') + '）'
                    add(topic, 'expense', row['id'], row['occurred_at'], title, row, 'recorded_spending')
                calls = [c for c in usage_stats.load_calls(config) if start <= moment(c.when.isoformat()) < end]
                groups = {}
                for call in calls:
                    key = (call.when.astimezone(TOKYO).date().isoformat(), call.group)
                    group = groups.setdefault(key, dict(usd=0., tokens=0, calls=0, unpriced=0, at=call.when.isoformat()))
                    group['usd'] += call.cost
                    group['tokens'] += call.tokens
                    group['calls'] += 1
                    group['unpriced'] += not call.priced
                for (day, group), value in groups.items():
                    add(topic, 'usage_ledger', day + ':' + group, value['at'],
                        '%s：$%.4f USD，%s Token' % (group,value['usd'],value['tokens']), value, 'token_usage')
                coverage.append('Expenses: living expense usd values use cents; token ledger usd values use dollars. Summarize saved amounts without reconverting currency.')
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            coverage.append(TOPICS[topic] + ': Some sources could not be read (' + type(error).__name__ + '); do not treat this as zero or no change.')
        if len(items) == before:
            coverage.append(TOPICS[topic] + ': No usable records found for this range; this does not prove nothing happened.')
    items.sort(key=lambda item: (item['at'], item['topic'], item['source_id']))
    if len(items) > 5000:
        coverage.append('This report keeps only the latest 5000 evidence records. Narrow the range to view older content.')
        items = items[-5000:]
    for index, item in enumerate(items, 1):
        item['number'] = index
    return items, coverage
