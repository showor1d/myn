"""Expenses/usage rendering plus retained preference editors and feedback dialogs.

Current navigation opens Expenses. Personal data and task management use chat tools;
legacy preference components remain for the backup adapter and compatibility tests.
"""

import json
from datetime import datetime
from pathlib import Path

import usage_stats
from sheets import Panel
from usage_window import receipt_data


def _config(win):
    return win._usage_config()


# Usage statistics.

class UsagePanel(Panel):
    key = "usage"
    title = 'Usage statistics'
    width = 960

    def __init__(self, win):
        super().__init__(win)
        self.period, self.offset = "day", 0

    def _report(self):
        try:
            return usage_stats.report(_config(self.win), self.period, self.offset), None
        except Exception as error:  # a broken log never breaks the screen
            return None, 'Could not read: %s' % (type(error).__name__,)

    def body(self):
        result, error = self._report()
        nav = [{"action": "period:" + period, "label": usage_stats.PERIOD_NAMES[period],
                "kind": "primary" if period == self.period else ""} for period in usage_stats.PERIODS]
        has_earlier = bool(result and result.get("has_earlier"))
        top = [{"t": "row", "children": [
            {"t": "buttons", "items": nav},
            {"t": "buttons", "items": [
                {"action": "earlier", "label": "◀", "disabled": not has_earlier, "title": 'Earlier'},
                {"action": "later", "label": "▶", "disabled": self.offset >= 0, "title": 'Later'},
                {"action": "now", "label": 'Back to now', "disabled": self.offset == 0}]},
            {"t": "p", "text": result["label"] if result else ""}]}]
        if result is None:
            return top + [{"t": "p", "tone": "bad", "text": error}]
        return top + [receipt_data(result, self.period)]

    def act(self, action, values):
        if action.startswith("period:"):
            self.period, self.offset = action.split(":", 1)[1], 0
        elif action == "earlier":
            result, _ = self._report()
            if result and result.get("has_earlier"):
                self.offset -= 1
        elif action == "later":
            self.offset = min(0, self.offset + 1)
        elif action == "now":
            self.offset = 0
        self.redraw()


class ExpensePanel(UsagePanel):
    title = "Expenses"

    def __init__(self, win):
        super().__init__(win)
        self.category = None

    def _report(self):
        import expenses
        try:
            return expenses.report(_config(self.win), self.period, self.offset, self.category), None
        except Exception as error:
            return None, 'Could not read expense records: ' + str(error)[:120]

    def body(self):
        import expenses
        result, error = self._report()
        nav = [{"action": "period:" + p, "label": expenses.PERIOD_LABELS[p],
                "kind": "primary" if p == self.period else ""} for p in usage_stats.PERIODS]
        top = [{"t": "expense_nav", "children": [
            {"t": "row", "children": [
            {"t": "buttons", "items": nav},
            {"t": "buttons", "items": [
                {"action": "earlier", "label": "◀", "disabled": not (result and result["has_earlier"])},
                {"action": "later", "label": "▶", "disabled": self.offset >= 0}]}]},
            {"t": "p", "text": result["date"] if result else ""}]}]
        if not result:
            return top + [{"t": "p", "text": error}]
        return top + [result]

    def act(self, action, values):
        import expenses
        if action.startswith("category:"):
            category = action.split(":", 1)[1]
            if category != "all" and category not in expenses.CATEGORIES:
                return
            self.category = None if category == "all" or category == self.category else category
        else:
            return super().act(action, values)
        self.redraw()

# Communication preferences.

def _pref_labels():
    import preferences_window as pw
    return pw


class PreferencesPanel(Panel):
    key = "preferences"
    title = 'Communication preferences'
    width = 980

    def __init__(self, win, engine):
        super().__init__(win)
        self.engine = engine
        self.store = engine.store
        self.message = None

    def _guard(self, action, *args, **kwargs):
        import preferences
        try:
            return action(*args, **kwargs)
        except (ValueError, preferences.PreferenceError) as error:
            self.toast(str(error), "bad")
        except Exception as error:
            self.toast('Operation failed: %s' % error, "bad")
        return None

    def body(self):
        import preferences
        pw = _pref_labels()
        try:
            summary = self.engine.summary()
            status = '%s active rules | %s candidates | %s feedback items' % (
                summary.get("active_rules", "?"), summary.get("candidates", "?"), summary.get("feedback", "?"))
            paused, applied = self.store.learning_paused, self.store.apply_enabled
        except Exception as error:  # a damaged store: say so, keep the screen usable
            return [{"t": "p", "tone": "bad", "text": 'Preferences are temporarily unavailable: %s' % pw.clip(error, 80)}]
        return [
            {"t": "p", "tone": "muted", "text": 'The communication style MYN has learned from you. This affects wording only, not facts, safety, code or permissions.'},
            {"t": "row", "children": [
                {"t": "p", "tone": "muted", "text": status}]},
            {"t": "tabs", "id": "tab", "tabs": [
                {"key": "rules", "label": 'Communication rules', "children": self._rules(preferences, pw)},
                {"key": "feedback", "label": 'Feedback history', "children": self._feedback(preferences, pw)},
                {"key": "examples", "label": 'Examples', "children": self._examples(preferences, pw)},
                {"key": "history", "label": 'Learning history', "children": self._history(pw)},
                {"key": "effect", "label": 'Effectiveness', "children": self._effect(preferences, pw)},
                {"key": "data", "label": 'Data', "children": self._data()}]},
        ]

    # rules
    def _rules(self, preferences, pw):
        order = {"active": 0, "candidate": 1, "paused": 2, "retired": 3}
        rules = sorted(self.store.rules(), key=lambda rule: (order.get(rule["status"], 9), -rule["priority"], -rule["id"]))
        chosen = self.v("rule")
        rows = [{"key": rule["id"], "tone": "dim" if rule["status"] in ("paused", "retired") else "",
                 "cells": [rule["text"], preferences.scene_label(rule["scenes"]),
                           preferences.SOURCE_LABELS.get(rule["source"], rule["source"]),
                           preferences.STATUS_LABELS.get(rule["status"], rule["status"]),
                           "%.2f" % rule["confidence"], str(rule["support"])]} for rule in rules]
        nodes = []
        conflicts = self.store.conflicts()
        if conflicts:
            nodes.append({"t": "p", "tone": "warn", "text": 'Warning: %d groups of rules conflict (for example, brief and detailed replies in the same scene). Delete or pause one rule: %s' % (
                len(conflicts), "；".join("「%s」↔「%s」" % (pw.clip(a["text"], 20), pw.clip(b["text"], 20)) for a, b in conflicts[:3]))})
        nodes += [
            {"t": "table", "id": "rule", "columns": ['Rule', 'Scene', 'Source', 'Status', 'Confidence', 'Evidence'], "rows": rows,
             "selected": chosen, "on": "pick", "open": "edit_rule", "height": 260, "empty": 'No rules learned yet'},
            {"t": "buttons", "items": [{"action": "add_rule", "label": 'Add rule', "kind": "primary"},
                                       {"action": "edit_rule", "label": 'Edit'},
                                       {"action": "rule_active", "label": 'Activate'},
                                       {"action": "rule_paused", "label": 'Pause'},
                                       {"action": "delete_rule", "label": 'Delete', "kind": "danger"}]},
            {"t": "p", "tone": "muted", "text": 'Candidates are inferred rules still being tested. Repeated feedback is required before they become active.'},
        ]
        if chosen:
            nodes.append({"t": "p", "mono": False, "text": self._rule_detail(int(chosen), preferences, pw)})
        return nodes

    def _rule_detail(self, rule_id, preferences, pw):
        rule = self.store.rule(rule_id)
        if rule is None:
            return ""
        lines = ['Rule: ' + rule["text"],
                 'Scene: %s  Source: %s  Status: %s  Priority: %d  Confidence: %.2f  Reinforced %d times / weakened %d times' % (
                     preferences.scene_label(rule["scenes"]), preferences.SOURCE_LABELS.get(rule["source"]),
                     preferences.STATUS_LABELS.get(rule["status"]), rule["priority"], rule["confidence"],
                     rule["support"], rule["oppose"]),
                 'Created: %s  Last changed: %s' % (pw.local_time(rule["created_at"]), pw.local_time(rule["updated_at"]))]
        if rule["note"]:
            lines.append('Notes: ' + rule["note"])
        if rule["origin"]:
            lines.append('Based on this feedback: ')
            for feedback_id in rule["origin"][:8]:
                item = self.store.feedback(feedback_id)
                if item:
                    lines.append("· %s %s %s%s" % (pw.local_time(item["created_at"]), pw.RATING_LABELS.get(item["rating"], ""),
                                                    pw.clip(item["reason"] or item["ideal"] or item["reply"], 70),
                                                    '(withdrawn)' if item["status"] == "revoked" else ""))
        history = [event for event in self.store.history(limit=2000) if event["rule_id"] == rule_id]
        if history:
            lines.append('Change history: ')
            for event in history[:8]:
                lines.append("· %s %s（%s）%s" % (pw.local_time(event["at"]), pw.ACTION_LABELS.get(event["action"], event["action"]),
                                                  pw.ACTOR_LABELS.get(event["actor"], event["actor"]), event["note"]))
        return "\n".join(lines)

    def _rule_form(self, rule=None):
        import preferences
        scenes = rule["scenes"] if rule else [preferences.ALL]
        body = [{"t": "field", "id": "r_text", "kind": "textarea", "rows": 3, "label": 'How should MYN speak? (one sentence, 2-200 characters)',
                 "value": rule["text"] if rule else ""},
                {"t": "check", "id": "r_all", "label": 'All scenes', "value": preferences.ALL in scenes},
                {"t": "row", "children": [{"t": "check", "id": "r_scene_" + scene, "label": label, "value": scene in scenes}
                                          for scene, label in preferences.SCENES.items()]},
                {"t": "p", "tone": "muted", "text": 'Selecting All scenes overrides the individual scene selections below.'},
                {"t": "field", "id": "r_priority", "kind": "number", "label": 'Priority (1-100; higher takes precedence)',
                 "value": rule["priority"] if rule else 100, "width": 120},
                {"t": "buttons", "align": "right", "items": [{"action": "modal_cancel", "label": 'Cancel'},
                                                             {"action": "save_rule", "label": 'Save', "kind": "primary"}]}]
        self.editing_rule = rule["id"] if rule else None
        self.modal('Edit rule' if rule else 'Add rule', body, width=620)

    # feedback
    def _feedback(self, preferences, pw):
        chosen = self.v("feedback_item")
        rows = []
        for item in self.store.feedback_list(limit=1000):
            words = item["reason"] or (('Preferred wording: ' + item["ideal"]) if item["ideal"] else "")
            rows.append({"key": item["id"], "tone": "dim" if item["status"] == "revoked" else "",
                         "cells": [pw.local_time(item["created_at"]), pw.RATING_LABELS.get(item["rating"], ""),
                                   preferences.SCENES.get(item["scene"] or "", "—"), pw.clip(words, 80) or '(no reason provided)',
                                   pw.CHANNEL_LABELS.get(item["channel"], item["channel"]),
                                   'Withdrawn' if item["status"] == "revoked" else 'Active']})
        nodes = [{"t": "table", "id": "feedback_item", "columns": ['Time', 'Rating', 'Scene', 'Reason / preferred wording', 'Method', 'Status'],
                  "rows": rows, "selected": chosen, "on": "pick", "height": 260, "empty": 'No feedback yet'},
                 {"t": "buttons", "items": [{"action": "rate_up", "label": 'Change to Like'}, {"action": "rate_down", "label": 'Change to Dislike'},
                                            {"action": "teach_feedback", "label": 'Add reason / preferred wording'},
                                            {"action": "revoke_feedback", "label": 'Undo'},
                                            {"action": "delete_feedback", "label": 'Delete permanently', "kind": "danger"}]}]
        if chosen:
            nodes.append({"t": "p", "text": self._feedback_detail(int(chosen), preferences, pw)})
        return nodes

    def _feedback_detail(self, feedback_id, preferences, pw):
        item = self.store.feedback(feedback_id)
        if item is None:
            return ""
        lines = ['%s  %s  Scene: %s  Model: %s%s' % (
            pw.local_time(item["created_at"]), pw.RATING_LABELS.get(item["rating"], ""),
            preferences.SCENES.get(item["scene"] or "", "—"), item["model"] or 'Unknown',
            ("（%s）" % item["provider"]) if item["provider"] else ""),
            'Your question: ' + (item["question"] or '(none)'), "MYN's reply: " + (item["reply"] or '(none)')]
        if item["reason"]:
            lines.append('Your reason: ' + item["reason"])
        if item["ideal"]:
            lines.append('Your preferred wording: ' + item["ideal"])
        if item["about"]:
            lines.append('Applies to: ' + pw.ABOUT_LABELS.get(item["about"], item["about"]))
        if item["analysis"]:
            try:
                analysis = json.loads(item["analysis"])
                if analysis.get("style_issues"):
                    lines.append("MYN's reflection (what to improve): " + "；".join(analysis["style_issues"]))
                if analysis.get("keep"):
                    lines.append("MYN's reflection (what to keep): " + "；".join(analysis["keep"]))
                if analysis.get("better"):
                    lines.append('A better wording might be: ' + analysis["better"])
            except (ValueError, AttributeError):
                pass
        else:
            lines.append('Reflection: ' + {"pending": 'Waiting for MYN to reflect (requires an available API)', "failed": 'Reflection abandoned after repeated failures',
                                    "none": 'Not needed'}.get(item["analysis_status"], item["analysis_status"]))
        events = self.store.events(feedback_id)
        if events:
            lines.append('Original record: ')
            for event in events:
                lines.append("· %s %s %s %s" % (pw.local_time(event["at"]), event["kind"],
                                                 pw.RATING_LABELS.get(event["rating"], "") if event["rating"] is not None else "",
                                                 pw.clip(event["value"], 80)))
        return "\n".join(lines)

    # examples
    def _examples(self, preferences, pw):
        chosen = self.v("example")
        examples = self.store.examples()
        rows = [{"key": example["id"], "cells": [
            'Correction' if example["kind"] == "correction" else 'Liked reply', preferences.SCENES.get(example["scene"] or "", "—"),
            pw.clip(example["ideal"] if example["kind"] == "correction" else example["original"], 90),
            pw.local_time(example["created_at"])]} for example in examples]
        nodes = [{"t": "p", "tone": "muted", "text": 'Replies you liked and wording you corrected. MYN may refer to one or two in similar scenes without copying their content.'},
                 {"t": "table", "id": "example", "columns": ['Type', 'Scene', 'Content', 'Time'], "rows": rows,
                  "selected": chosen, "on": "pick", "height": 260, "empty": 'No examples yet'},
                 {"t": "buttons", "items": [{"action": "delete_example", "label": 'Delete example', "kind": "danger"}]}]
        found = [example for example in examples if chosen and str(example["id"]) == str(chosen)]
        if found:
            example = found[0]
            lines = ['Question: ' + (example["question"] or '(none)'), "MYN's original reply: " + (example["original"] or '(none)')]
            if example["kind"] == "correction":
                lines.append('Your preferred wording: ' + example["ideal"])
            nodes.append({"t": "p", "text": "\n\n".join(lines)})
        return nodes

    # learning history
    def _history(self, pw):
        rows = [{"key": event["id"], "cells": [
            pw.local_time(event["at"]), pw.ACTION_LABELS.get(event["action"], event["action"]),
            pw.ACTOR_LABELS.get(event["actor"], event["actor"]), pw.clip(event.get("rule_text") or "", 60),
            pw.clip(event["note"], 50)]} for event in self.store.history(limit=1000)]
        return [{"t": "table", "id": "history_item", "columns": ['Time', 'Change', 'Actor', 'Rule', 'Description'], "rows": rows,
                 "selected": self.v("history_item"), "height": 330, "empty": 'No learning history yet'},
                {"t": "row", "children": [{"t": "buttons", "items": [{"action": "undo", "label": 'Undo this step'}]},
                                          {"t": "p", "tone": "muted", "text": 'Learned the wrong thing? Undo that step to restore the preceding rules.'}]}]

    # effect
    def _effect(self, preferences, pw):
        report = preferences.evaluate(self.store)

        def rate(window):
            if window["satisfaction"] is None:
                return 'No ratings'
            return 'Satisfaction %d%% (Like %d / Dislike %d)' % (round(window["satisfaction"] * 100), window["likes"], window["dislikes"])

        nodes = [{"t": "kv", "items": [['Last %d days' % report["recent_days"], rate(report["recent"])],
                                       ['Previous %d days' % report["recent_days"], rate(report["before"])],
                                       ['Feedback awaiting reflection', '%d items (processed gradually when the API is available; your ratings are saved locally)' % self.store.pending_count()]]},
                 {"t": "h", "text": 'Weekly'},
                 {"t": "table", "id": "_weeks", "columns": ['Through', 'Satisfaction'], "height": 160, "empty": 'None yet',
                  "rows": [{"key": week["week_ending"], "cells": [week["week_ending"], rate(week)]} for week in report["weeks"]]},
                 {"t": "h", "text": 'Recurring issues'},
                 {"t": "p", "text": "\n".join('%s: %d times' % (item["label"], item["count"]) for item in report["recurring_issues"]) or 'None yet'},
                 {"t": "h", "text": 'Rule effectiveness'},
                 {"t": "table", "id": "_rules_effect", "columns": ['Rule', 'Usage', "▲", "▼", 'Conclusion'], "height": 200, "empty": 'No rules yet',
                  "rows": [{"key": index, "cells": [pw.clip(effect["text"], 40), str(effect["applied"]), str(effect["liked"]),
                                                    str(effect["disliked"]), effect["verdict"]]}
                           for index, effect in enumerate(report["rules"])]}]
        if report["ineffective"]:
            nodes += [{"t": "p", "tone": "warn", "text": 'Rules with no sustained positive effect (consider pausing or deleting):' +
                       "；".join("「%s」" % pw.clip(effect["text"], 60) for effect in report["ineffective"])}]
        return nodes

    # data
    def _data(self):
        folder = Path(self.store.folder)
        return [{"t": "p", "tone": "muted", "text": 'Saved at: %s (feedback.db contains original data; style_profile.json, preferred_examples.jsonl and '
                                                   'style_history.jsonl are readable copies)' % folder},
                {"t": "buttons", "items": [{"action": "export", "label": 'Export all preference data'},
                                           {"action": "import", "label": 'Import...'},
                                           {"action": "backup", "label": 'Back up now'},
                                           {"action": "restore", "label": 'Restore from backup...'},
                                           {"action": "open_folder", "label": 'Open folder'}]},
                {"t": "p", "tone": "muted", "text": 'Export: create a JSON file with rules, feedback, examples and learning history for import after reinstallation or on another computer.'
                                                   'Import: merge an export from the exports folder, backing up first and avoiding duplicate records.'
                                                   'Backup: save to preferences/backups, preserving old backups. Restore: replace current preferences with a selected backup, renaming and preserving the current file.'}]

    def _files(self, sub, pattern):
        folder = Path(self.store.folder) / sub
        try:
            return sorted(folder.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)[:60]
        except OSError:
            return []

    def _pick_file(self, title, sub, pattern, action, note):
        files = self._files(sub, pattern)
        rows = [{"key": str(path), "cells": [path.name, datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                                             "%.1f KB" % (path.stat().st_size / 1024)]} for path in files]
        self.modal(title, [{"t": "p", "tone": "muted", "text": note},
                           {"t": "table", "id": "file", "columns": ['File', 'Time', 'Size'], "rows": rows, "height": 240,
                            "empty": 'No files here yet (%s)' % (Path(self.store.folder) / sub)},
                           {"t": "buttons", "align": "right", "items": [{"action": "modal_cancel", "label": 'Cancel'},
                                                                        {"action": action, "label": 'Use selected', "kind": "primary"}]}],
                   width=640)

    # actions
    def act(self, action, values):
        import preferences
        pw = _pref_labels()
        store = self.store
        if action == "pick":
            self.redraw()
        elif action == "set_paused":
            self._guard(store.set_setting, "learning_paused", bool(values.get("paused")))
            self.redraw()
        elif action == "set_applied":
            self._guard(store.set_setting, "apply_enabled", bool(values.get("applied")))
            self.redraw()
        elif action == "add_rule":
            self._rule_form(None)
        elif action in ("edit_rule", "rule_active", "rule_paused", "delete_rule"):
            chosen = values.get("rule")
            rule = store.rule(int(chosen)) if chosen else None
            if rule is None:
                self.toast('Select a rule first.', "warn")
                return
            if action == "edit_rule":
                self._rule_form(rule)
            elif action == "delete_rule":
                def answered(yes):
                    if yes:
                        self._guard(store.delete_rule, rule["id"])
                        self.values.pop("rule", None)
                        self.redraw()
                self.confirm('Delete this rule? Future replies will stop using it immediately.\n\n%s\n\n(Deletion is recorded in learning history and can be undone.)' % rule["text"],
                             answered, yes='Delete', danger=True)
            else:
                self._guard(store.update_rule, rule["id"], status="active" if action == "rule_active" else "paused")
                self.redraw()
        elif action in ("rate_up", "rate_down", "teach_feedback", "revoke_feedback", "delete_feedback"):
            chosen = values.get("feedback_item")
            item = store.feedback(int(chosen)) if chosen else None
            if item is None:
                self.toast('Select a feedback item first.', "warn")
                return
            if action in ("rate_up", "rate_down"):
                rating = 1 if action == "rate_up" else -1
                if item["reply_id"] is not None and item["status"] == "active":
                    self._guard(self.engine.rate, item["reply_id"], rating)  # the learning follows the new rating
                else:
                    self._guard(store.set_rating, item["id"], rating)
                self.redraw()
            elif action == "teach_feedback":
                if item["reply_id"] is None:
                    self.toast('This feedback is not linked to a specific reply. It can only be withdrawn or deleted.', "warn")
                    return
                self._teach_form(item["reply_id"], item["reply"])
            elif action == "revoke_feedback":
                def answered(yes):
                    if yes:
                        self._guard(store.revoke_feedback, item["id"])
                        self.redraw()
                self.confirm('Withdraw this feedback? It will stop contributing to ratings and unsupported inferred rules will be removed. The original record is preserved.', answered, yes='Undo')
            else:
                def answered(yes):
                    if yes:
                        self._guard(store.delete_feedback, item["id"])
                        self.values.pop("feedback_item", None)
                        self.redraw()
                self.confirm('Permanently delete this feedback and its original record? This cannot be undone (older copies remain in previous automatic backups).', answered,
                             yes='Delete permanently', danger=True)
        elif action == "delete_example":
            chosen = values.get("example")
            if not chosen:
                self.toast('Select an example first.', "warn")
                return

            def answered(yes):
                if yes:
                    self._guard(store.delete_example, int(chosen))
                    self.values.pop("example", None)
                    self.redraw()
            self.confirm('Delete this example?', answered, yes='Delete', danger=True)
        elif action == "undo":
            chosen = values.get("history_item")
            if not chosen:
                self.toast('Select a learning-history entry first.', "warn")
                return
            if self._guard(store.undo_history, int(chosen)) is False:
                self.toast('This step cannot be undone (for example, a settings change or feedback deletion).', "warn")
            else:
                self.toast('Undone.', "good")
            self.redraw()
        elif action == "export":
            path = self._guard(store.export)
            if path:
                self.toast('Export completed: %s' % path, "good")
        elif action == "import":
            self._pick_file('Import communication preferences', "exports", "*.json", "do_import", 'Choose an export to merge. A backup is created first and duplicate records are avoided.')
        elif action == "backup":
            path = self._guard(store.backup, "manual")
            if path:
                self.toast('Backup completed: %s' % path, "good")
        elif action == "restore":
            self._pick_file('Restore from backup', "backups", "*.db", "do_restore", 'Replace current communication preferences with the selected backup, renaming and preserving the current file.')
        elif action == "open_folder":
            pw.open_folder(store.folder)

    def modal_act(self, action, values):
        import preferences
        store = self.store
        if action == "save_rule":
            text = str(values.get("r_text") or "").strip()
            scenes = [preferences.ALL] if values.get("r_all") else \
                [scene for scene in preferences.SCENES if values.get("r_scene_" + scene)] or [preferences.ALL]
            try:
                priority = int(float(values.get("r_priority") or 100))
                if self.editing_rule is None:
                    dims = preferences.dimensions_in(text)
                    dimension, direction = dims[0] if dims else ("other", "less")
                    rule_id = store.add_rule(text, dimension, direction, scenes, source="manual")
                    if priority != 100:
                        store.update_rule(rule_id, priority=priority)
                else:
                    store.update_rule(self.editing_rule, text=text, scenes=scenes, priority=priority)
            except (ValueError, preferences.PreferenceError) as error:
                self.toast(str(error), "bad")
                return
            except Exception as error:  # sqlite / disk
                self.toast('Could not save: %s' % error, "bad")
                return
            self.close_modal()
            self.redraw()
        elif action in ("teach_save", "teach_revoke"):
            if _teach_act(self, action, values):
                self.close_modal()
                self.redraw()
        elif action == "do_import":
            chosen = values.get("file")
            if not chosen:
                self.toast('Select a file first.', "warn")
                return
            added = self._guard(store.import_file, chosen)
            self.close_modal()
            if added is not None:
                self.toast('Import completed: %d rules, %d feedback items and %d examples added.' % (added["rules"], added["feedback"], added["examples"]), "good")
            self.redraw()
        elif action == "do_restore":
            chosen = values.get("file")
            if not chosen:
                self.toast('Select a backup first.', "warn")
                return

            def answered(yes):
                if not yes:
                    return
                kept = self._guard(store.restore, chosen)
                self.close_modal()
                if kept is not None or store.path.exists():
                    self.toast('Restored.' + (('Previous file preserved as: %s' % kept) if kept else ""), "good")
                self.redraw()
            self.confirm('Replace current preferences with this backup? The current file will be renamed and preserved.', answered, yes='Restore')

    def _teach_form(self, reply_id, reply_text):
        self.teach_reply_id = reply_id
        self.modal('Teach MYN this wording', _teach_body(self.engine, reply_id, reply_text), width=640)


# Teach wording from a reply panel or feedback history form.

def _teach_body(engine, reply_id, reply_text):
    try:
        current = engine.store.feedback(reply_id=reply_id)
    except Exception:
        current = None
    rated = current["rating"] if current and current["status"] == "active" else 0
    body = [{"t": "p", "tone": "muted", "text": "MYN's reply: "},
            {"t": "p", "text": reply_text or (current or {}).get("reply", "") or '(Reply text not found)'},
            {"t": "radio", "id": "t_rating", "value": str(rated),
             "options": [["1", 'Like'], ["-1", 'Dislike'], ["0", 'No rating']]},
            {"t": "field", "id": "t_reason", "kind": "textarea", "rows": 2,
             "label": 'Why? (optional, e.g. "too verbose" or "skip the opening compliment")', "value": ""},
            {"t": "field", "id": "t_ideal", "kind": "textarea", "rows": 4, "label": 'How would you like it worded? (optional; write your ideal reply)',
             "value": (current or {}).get("ideal") or ""}]
    if current and current.get("reason"):
        body.append({"t": "p", "tone": "muted", "text": 'Previous reason: ' + _pref_labels().clip(current["reason"], 80)})
    items = [{"action": "modal_cancel", "label": 'Cancel'}, {"action": "teach_save", "label": 'Save', "kind": "primary"}]
    if current and current["status"] == "active":
        items.insert(0, {"action": "teach_revoke", "label": 'Withdraw this feedback', "kind": "danger"})
    body.append({"t": "buttons", "align": "right", "items": items})
    return body


def _teach_act(panel, action, values):
    'Save or withdraw feedback from the Teach me form; return True on completion.'
    engine, reply_id = panel.engine, panel.teach_reply_id
    if action == "teach_revoke":
        try:
            current = engine.store.feedback(reply_id=reply_id)
            if current:
                engine.store.revoke_feedback(current["id"])
        except Exception as error:
            panel.toast('Could not undo: %s' % error, "bad")
            return False
        panel.done = (0, [])
        return True
    reason = str(values.get("t_reason") or "").strip()
    ideal = str(values.get("t_ideal") or "").strip()
    try:
        rating = int(values.get("t_rating") or 0)
    except ValueError:
        rating = 0
    if rating == 0 and not reason and not ideal:
        panel.done = None
        return True
    try:
        _feedback, made = engine.rate(reply_id, rating, reason, ideal)
    except Exception as error:
        panel.toast('Could not save: %s' % error, "bad")
        return False
    panel.done = (rating, made)
    return True


class TeachPanel(Panel):
    'Teach me form for a reply under the core, displayed in its own small panel.'
    key = "teach"
    title = 'Teach MYN this wording'
    width = 640

    def __init__(self, win, engine, reply_id, reply_text, on_done=None):
        super().__init__(win)
        self.engine, self.teach_reply_id, self.reply_text, self.on_done = engine, reply_id, reply_text, on_done
        self.done = None

    def body(self):
        return _teach_body(self.engine, self.teach_reply_id, self.reply_text)

    def act(self, action, values):
        if action == "modal_cancel":
            self.host.close()
        elif action in ("teach_save", "teach_revoke"):
            if _teach_act(self, action, values):
                done = self.done
                self.host.close()
                if done is not None and self.on_done:
                    self.on_done(*done)


# Background tasks.

class AgentLogPanel(Panel):
    key = "agent_log"
    title = 'Background tasks'
    width = 640
    MARKS = {"task": 'Task: ', "text": "", "tool": "· ", "ask": '? Approval needed: ', "answer": "  → ", "done": "■ "}

    def body(self):
        win = self.win
        log = win.agent_log or [("text", 'No background tasks yet. For example, ask MYN to organize images in Downloads by month.')]
        running = win.agent_runner is not None and win.agent_runner.busy
        text = "".join(self.MARKS.get(kind, "") + line + "\n" + ("\n" if kind in ("task", "text", "done") else "")
                       for kind, line in log)
        return [{"t": "p", "text": text.strip()},
                {"t": "row", "children": [
                    {"t": "buttons", "items": [{"action": "stop", "label": 'Stop task', "kind": "danger", "disabled": not running}]},
                    {"t": "p", "tone": "muted", "text": 'In progress...' if running else ""}]}]

    def act(self, action, values):
        if action == "stop":
            self.win.stop_agent_task()
            self.redraw()


# Choose an image for MYN from the input bar, inside the page.

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class ImagePickPanel(Panel):
    key = "pick_image"
    title = 'Choose images to show MYN'
    width = 680

    def __init__(self, win, start=""):
        super().__init__(win)
        self.path = start

    def body(self):
        from web_panels2 import _places
        import os
        rows, note = [], None
        if not self.path:
            rows = [{"key": "d:" + place, "cells": ["📁 " + place, ""]} for place in _places()]
        else:
            try:
                with os.scandir(self.path) as entries:
                    items = []
                    for entry in entries:
                        if entry.name.startswith((".", "$")):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                items.append((0, entry.name.casefold(), "d:" + entry.path, "📁 " + entry.name, ""))
                            elif entry.name.lower().endswith(IMAGE_TYPES):
                                size = entry.stat().st_size
                                items.append((1, entry.name.casefold(), "f:" + entry.path, "🖼 " + entry.name,
                                              "%.0f KB" % (size / 1024)))
                        except OSError:
                            continue
                        if len(items) >= 600:
                            break
                rows = [{"key": key, "cells": [label, size]} for _k, _n, key, label, size in sorted(items)]
            except OSError:
                note = 'Cannot open this folder (missing or access denied).'
        body = [{"t": "field", "id": "path", "label": 'Current folder (edit the path and press Enter to open)', "value": self.path, "enter": "go",
                 "placeholder": 'Choose a drive or common folder below'},
                {"t": "table", "id": "item", "columns": ['Name (double-click to open a folder or select an image)', 'Size'], "rows": rows, "height": 320,
                 "open": "open", "empty": 'No folders or images here'}]
        if note:
            body.append({"t": "p", "tone": "warn", "text": note})
        body.append({"t": "buttons", "align": "right", "items": [
            {"action": "up", "label": 'Up one level', "disabled": not self.path},
            {"action": "cancel", "label": 'Cancel'},
            {"action": "open", "label": 'Open / use this image', "kind": "primary"}]})
        return body

    def act(self, action, values):
        if action == "cancel":
            self.host.close()
            return
        if action == "go":
            self.path = str(values.get("path") or "").strip()
        elif action == "up":
            parent = str(Path(self.path).parent) if self.path else ""
            self.path = "" if parent == self.path else parent
        elif action == "open":
            chosen = str(values.get("item") or "")
            if chosen.startswith("d:"):
                self.path = chosen[2:]
            elif chosen.startswith("f:"):
                path = chosen[2:]
                self.win.last_image_folder = str(Path(path).parent)
                self.host.close()
                import vision
                self.win._attach(lambda camera: vision.from_file(camera, path), Path(path).name)
                return
            else:
                self.toast('Select a folder or image first.', "warn")
                return
        self.values.pop("item", None)
        self.redraw()


# Evolution points.

def parse_tokens(text):
    'Parse decimal or abbreviated token counts (including Chinese units); raise ValueError for invalid input.'
    raw = str(text or "").strip().lower().replace(",", "").replace("token", "").strip()
    for suffix, factor in (("亿", 10 ** 8), ("万", 10 ** 4), ("b", 10 ** 9), ("m", 10 ** 6), ("k", 10 ** 3)):
        if raw.endswith(suffix):
            from decimal import Decimal, InvalidOperation
            try:
                value = Decimal(raw[:-len(suffix)]) * factor
                if not value.is_finite() or value < 0 or value > 10 ** 10:
                    raise ValueError('Monthly allowance must be between 0 and 10 billion tokens.')
                return int(value)
            except InvalidOperation:
                raise ValueError('Enter a numeric allowance, for example 20M.') from None
    return int(raw)


def conversation_time(stamp):
    try:
        from datetime import timedelta, timezone
        value = datetime.fromisoformat(str(stamp))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(hours=9)))
        return value.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


PANELS = {
    "usage": ExpensePanel,
    "agent_log": AgentLogPanel,
}
