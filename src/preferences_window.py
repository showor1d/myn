'Legacy standalone preference editor, not exposed by current navigation.\n\nAll displayed data lives in core/preferences.py (personal_data/preferences). This editor changes communication style, never code, permissions or security. It also supplies the Teach me dialog for rating a reply, explaining why and suggesting better wording.\n'

import os
import sys
import tkinter as tk
import theme
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import memory_query
import preferences

TITLE = 'Communication preferences'
RATING_LABELS = {1: 'Like', -1: 'Dislike', 0: "—"}
CHANNEL_LABELS = {"button": 'Button', "natural": 'Conversation', "button+natural": 'Button and conversation', "import": 'Import'}
ACTION_LABELS = {"create": 'Create', "reinforce": 'Reconfirmed', "edit": 'Modify', "delete": 'Delete', "undo": 'Undo',
                 "superseded": 'Replaced by a new rule', "withdraw": 'Supporting evidence withdrawn', "support": 'Reinforced', "oppose": 'Weakened',
                 "import": 'Import', "setting": 'Settings', "feedback_deleted": 'Delete feedback'}
ACTOR_LABELS = {"user": 'You', "myn": "MYN"}
ABOUT_LABELS = {"style": 'Communication style', "content": 'Content/facts', "both": 'Content and style', "unclear": 'Uncertain'}


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(memory_query.TOKYO).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value or "")


def clip(text, limit=60):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def open_folder(path):
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - the user's own data folder
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
    except OSError:
        pass


class ScenePicker(ttk.Frame):
    'Checkbuttons: all scenes or selected scenes.'

    def __init__(self, parent, scenes=None):
        super().__init__(parent)
        scenes = scenes or [preferences.ALL]
        self.everywhere = tk.BooleanVar(value=preferences.ALL in scenes)
        ttk.Checkbutton(self, text='All scenes', variable=self.everywhere,
                        command=self._sync).grid(row=0, column=0, sticky="w", columnspan=4)
        self.vars = {}
        for index, (scene, label) in enumerate(preferences.SCENES.items()):
            var = tk.BooleanVar(value=scene in scenes)
            box = ttk.Checkbutton(self, text=label, variable=var)
            box.grid(row=1 + index // 4, column=index % 4, sticky="w", padx=(0, 10))
            self.vars[scene] = (var, box)
        self._sync()

    def _sync(self):
        for var, box in self.vars.values():
            box.state(["disabled"] if self.everywhere.get() else ["!disabled"])

    def value(self):
        if self.everywhere.get():
            return [preferences.ALL]
        chosen = [scene for scene, (var, _box) in self.vars.items() if var.get()]
        return chosen or [preferences.ALL]


class RuleDialog:
    """Add or edit one rule by hand."""

    def __init__(self, parent, store, rule=None, on_done=None, font_family="TkDefaultFont"):
        self.store, self.rule, self.on_done = store, rule, on_done
        self.window = tk.Toplevel(parent)
        self.window.title('Edit rule' if rule else 'Add rule')
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text='How should MYN speak? (one sentence, 2-200 characters):').pack(anchor="w")
        self.text = tk.Text(frame, height=3, width=56, wrap=tk.WORD, font=(font_family, 10))
        self.text.pack(fill=tk.X, pady=(2, 8))
        if rule:
            self.text.insert("1.0", rule["text"])
        ttk.Label(frame, text='Applicable scenes:').pack(anchor="w")
        self.scenes = ScenePicker(frame, rule["scenes"] if rule else None)
        self.scenes.pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text='Priority (1-100; higher takes precedence):').pack(side=tk.LEFT)
        self.priority = tk.Spinbox(row, from_=1, to=100, width=5)
        self.priority.delete(0, tk.END)
        self.priority.insert(0, str(rule["priority"]) if rule else "100")
        self.priority.pack(side=tk.LEFT)
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text='Save', command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='Cancel', command=self.window.destroy).pack(side=tk.RIGHT, padx=6)
        self.text.focus_set()

    def save(self):
        text = self.text.get("1.0", tk.END).strip()
        try:
            priority = int(self.priority.get())
            if self.rule is None:
                dims = preferences.dimensions_in(text)
                dimension, direction = dims[0] if dims else ("other", "less")
                rule_id = self.store.add_rule(text, dimension, direction, self.scenes.value(), source="manual")
                if priority != 100:
                    self.store.update_rule(rule_id, priority=priority)
            else:
                self.store.update_rule(self.rule["id"], text=text, scenes=self.scenes.value(), priority=priority)
        except (ValueError, preferences.PreferenceError) as error:
            messagebox.showerror(TITLE, str(error), parent=self.window)
            return
        except Exception as error:  # sqlite / disk
            messagebox.showerror(TITLE, 'Could not save: %s' % error, parent=self.window)
            return
        self.window.destroy()
        if self.on_done:
            self.on_done()


class FeedbackDialog:
    'Teach me: rate one reply, explain why and/or provide preferred wording.'

    def __init__(self, parent, engine, reply_id, reply_text="", on_done=None, font_family="TkDefaultFont"):
        self.engine, self.reply_id, self.on_done = engine, reply_id, on_done
        try:
            current = engine.store.feedback(reply_id=reply_id)
        except Exception:
            current = None
        self.window = tk.Toplevel(parent)
        self.window.title('Teach MYN this wording')
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="MYN's reply: ").pack(anchor="w")
        shown = scrolledtext.ScrolledText(frame, height=5, width=64, wrap=tk.WORD, font=(font_family, 10))
        shown.insert("1.0", reply_text or (current or {}).get("reply", ""))
        shown.configure(state=tk.DISABLED)
        shown.pack(fill=tk.BOTH, expand=True, pady=(2, 8))
        rated = current["rating"] if current and current["status"] == "active" else 0
        self.rating = tk.IntVar(value=rated)
        row = ttk.Frame(frame)
        row.pack(anchor="w")
        for value, label in ((1, 'Like'), (-1, 'Dislike'), (0, 'No rating')):
            ttk.Radiobutton(row, text=label, value=value, variable=self.rating).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(frame, text='Why? (optional, e.g. "too verbose" or "skip the opening compliment"):').pack(anchor="w", pady=(8, 0))
        self.reason = tk.Text(frame, height=2, width=64, wrap=tk.WORD, font=(font_family, 10))
        self.reason.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(frame, text='How would you like it worded? (optional; write your ideal reply):').pack(anchor="w")
        self.ideal = tk.Text(frame, height=4, width=64, wrap=tk.WORD, font=(font_family, 10))
        self.ideal.pack(fill=tk.X, pady=(2, 6))
        if current and current.get("ideal"):
            self.ideal.insert("1.0", current["ideal"])
        if current and current.get("reason"):
            ttk.Label(frame, text='Previous reason: ' + clip(current["reason"], 80),
                      foreground=theme.MUTED).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text='Save', command=self.save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='Cancel', command=self.window.destroy).pack(side=tk.RIGHT, padx=6)
        if current and current["status"] == "active":
            ttk.Button(buttons, text='Withdraw this feedback', command=self.revoke).pack(side=tk.LEFT)
        self.reason.focus_set()

    def save(self):
        reason = self.reason.get("1.0", tk.END).strip()
        ideal = self.ideal.get("1.0", tk.END).strip()
        rating = self.rating.get()
        if rating == 0 and not reason and not ideal:
            self.window.destroy()
            return
        try:
            _feedback, made = self.engine.rate(self.reply_id, rating, reason, ideal)
        except Exception as error:
            messagebox.showerror(TITLE, 'Could not save: %s' % error, parent=self.window)
            return
        self.window.destroy()
        if self.on_done:
            self.on_done(rating, made)

    def revoke(self):
        try:
            current = self.engine.store.feedback(reply_id=self.reply_id)
            if current:
                self.engine.store.revoke_feedback(current["id"])
        except Exception as error:
            messagebox.showerror(TITLE, 'Could not undo: %s' % error, parent=self.window)
            return
        self.window.destroy()
        if self.on_done:
            self.on_done(0, [])


class PreferencesWindow:
    """Rules, their sources, the reward/penalty history, examples, learning history,
    the effect check and the data itself (export / import / backups)."""

    def __init__(self, parent, engine, font_family="TkDefaultFont", owner=None):
        self.engine = engine
        self.store = engine.store
        self.font_family = font_family
        self.window = tk.Toplevel(parent)
        self.window.title('Backup' if owner is not None else TITLE)
        self.window.geometry("980x700")
        outer = theme.scroll_body(self.window, padding=12)  # scrolls when the panel is shorter
        ttk.Label(outer, text='The communication style MYN has learned from you. This affects wording only, not facts, safety, code or permissions.',
                  foreground=theme.MUTED).pack(anchor="w")
        top = ttk.Frame(outer)
        top.pack(fill=tk.X, pady=(6, 6))
        self.paused = tk.BooleanVar()
        self.applied = tk.BooleanVar()
        self.status = ttk.Label(top, foreground=theme.MUTED)
        self.status.pack(side=tk.RIGHT)
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=tk.BOTH, expand=True)
        if owner is not None:
            import backup_ui
            tab = ttk.Frame(self.tabs, padding=8)
            self.tabs.add(tab, text='Backup and restore')
            ttk.Label(tab, text='Full backup: conversations, memories, preferences, settings, expenses, alarms, calendar, media and encrypted API keys.',
                      wraplength=850).pack(anchor="w")
            row = ttk.Frame(tab)
            row.pack(fill=tk.X, pady=8)
            ttk.Button(row, text='Export full backup', command=lambda: backup_ui.transfer(owner)).pack(side=tk.LEFT)
            ttk.Button(row, text='Import full backup', command=lambda: backup_ui.transfer(owner, True)).pack(side=tk.LEFT, padx=8)
            from data_location import root_for_db
            import portable_backup
            ttk.Button(row, text='Data preserved before restore', command=lambda: open_folder(
                portable_backup.transfer_dir(root_for_db(owner.session.db_path)))).pack(side=tk.LEFT)
            ttk.Label(tab, text='On another computer, install MYN and speech components first, then import with the backup password. External files and hardware are not included.',
                      wraplength=850).pack(anchor="w")
            owner._build_library_frame(tab)
        self._build_rules()
        self._build_feedback()
        self._build_examples()
        self._build_history()
        self._build_effect()
        self._build_data()
        self.refresh()

    # -- helpers ---------------------------------------------------------------------------

    def _guard(self, action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except (ValueError, preferences.PreferenceError) as error:
            messagebox.showerror(TITLE, str(error), parent=self.window)
        except Exception as error:
            messagebox.showerror(TITLE, 'Operation failed: %s' % error, parent=self.window)
        return None

    def _setting(self, name, value):
        self._guard(self.store.set_setting, name, value)
        self.refresh()

    @staticmethod
    def _tree(parent, columns, widths, height=12):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for column, width in zip(columns, widths):
            tree.heading(column, text=column)
            tree.column(column, width=width, anchor="w", stretch=column == columns[0] or width > 200)
        bar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        return frame, tree

    def _detail_box(self, parent, height=8):
        box = scrolledtext.ScrolledText(parent, height=height, wrap=tk.WORD, font=(self.font_family, 10))
        box.configure(state=tk.DISABLED)
        return box

    @staticmethod
    def _show(box, text):
        box.configure(state=tk.NORMAL)
        box.delete("1.0", tk.END)
        box.insert("1.0", text)
        box.configure(state=tk.DISABLED)

    @staticmethod
    def _selected(tree):
        chosen = tree.selection()
        return int(chosen[0]) if chosen else None

    # -- rules -----------------------------------------------------------------------------

    def _build_rules(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Communication rules')
        self.conflict_label = ttk.Label(tab, foreground=theme.WARN)
        self.conflict_label.pack(anchor="w")
        frame, self.rule_tree = self._tree(tab, ('Rule', 'Scene', 'Source', 'Status', 'Confidence', 'Evidence'),
                                           (420, 160, 100, 70, 60, 50), height=13)
        frame.pack(fill=tk.BOTH, expand=True)
        self.rule_tree.bind("<<TreeviewSelect>>", lambda _event: self._rule_detail())
        self.rule_tree.bind("<Double-1>", lambda _event: self._edit_rule())
        buttons = ttk.Frame(tab)
        buttons.pack(fill=tk.X, pady=6)
        for label, action in (('Add rule', self._add_rule), ('Edit', self._edit_rule),
                              ('Activate', lambda: self._rule_status("active")),
                              ('Pause', lambda: self._rule_status("paused")),
                              ('Delete', self._delete_rule)):
            ttk.Button(buttons, text=label, command=action).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(buttons, text='Candidates are inferred rules still being tested. Repeated feedback is required before they become active.',
                  foreground=theme.MUTED).pack(side=tk.LEFT, padx=8)
        self.rule_box = self._detail_box(tab, 7)
        self.rule_box.pack(fill=tk.BOTH, expand=False)

    def _fill_rules(self):
        tree = self.rule_tree
        tree.delete(*tree.get_children())
        order = {"active": 0, "candidate": 1, "paused": 2, "retired": 3}
        rules = sorted(self.store.rules(), key=lambda rule: (order.get(rule["status"], 9), -rule["priority"],
                                                              -rule["id"]))
        for rule in rules:
            tree.insert("", tk.END, iid=str(rule["id"]), values=(
                rule["text"], preferences.scene_label(rule["scenes"]),
                preferences.SOURCE_LABELS.get(rule["source"], rule["source"]),
                preferences.STATUS_LABELS.get(rule["status"], rule["status"]),
                "%.2f" % rule["confidence"], rule["support"]))
        conflicts = self.store.conflicts()
        self.conflict_label.configure(text=('Warning: %d groups of rules conflict (for example, both brief and detailed answers in the same scene). '
                                            'Delete or pause one rule from each group: %s' % (
                                                len(conflicts), "；".join("「%s」↔「%s」" % (clip(a["text"], 20),
                                                                                         clip(b["text"], 20))
                                                                          for a, b in conflicts[:3])))
                                      if conflicts else "")

    def _rule_detail(self):
        rule_id = self._selected(self.rule_tree)
        if rule_id is None:
            return
        rule = self.store.rule(rule_id)
        if rule is None:
            return
        lines = ['Rule: ' + rule["text"],
                 'Scene: %s  Source: %s  Status: %s  Priority: %d  Confidence: %.2f  Reinforced %d times / weakened %d times' % (
                     preferences.scene_label(rule["scenes"]), preferences.SOURCE_LABELS.get(rule["source"]),
                     preferences.STATUS_LABELS.get(rule["status"]), rule["priority"], rule["confidence"],
                     rule["support"], rule["oppose"]),
                 'Created: %s  Last changed: %s' % (local_time(rule["created_at"]), local_time(rule["updated_at"]))]
        if rule["note"]:
            lines.append('Notes: ' + rule["note"])
        if rule["origin"]:
            lines.append('\nBased on this feedback: ')
            for feedback_id in rule["origin"][:8]:
                item = self.store.feedback(feedback_id)
                if item:
                    lines.append("· %s %s %s%s" % (local_time(item["created_at"]),
                                                    RATING_LABELS.get(item["rating"], ""),
                                                    clip(item["reason"] or item["ideal"] or item["reply"], 70),
                                                    '(withdrawn)' if item["status"] == "revoked" else ""))
        history = [event for event in self.store.history(limit=2000) if event["rule_id"] == rule_id]
        if history:
            lines.append('\nChange history: ')
            for event in history[:8]:
                lines.append("· %s %s（%s）%s" % (local_time(event["at"]), ACTION_LABELS.get(event["action"],
                                                                                         event["action"]),
                                                  ACTOR_LABELS.get(event["actor"], event["actor"]), event["note"]))
        self._show(self.rule_box, "\n".join(lines))

    def _add_rule(self):
        RuleDialog(self.window, self.store, None, self.refresh, self.font_family)

    def _edit_rule(self):
        rule_id = self._selected(self.rule_tree)
        if rule_id is None:
            messagebox.showinfo(TITLE, 'Select a rule first.', parent=self.window)
            return
        rule = self.store.rule(rule_id)
        if rule:
            RuleDialog(self.window, self.store, rule, self.refresh, self.font_family)

    def _rule_status(self, status):
        rule_id = self._selected(self.rule_tree)
        if rule_id is None:
            messagebox.showinfo(TITLE, 'Select a rule first.', parent=self.window)
            return
        self._guard(self.store.update_rule, rule_id, status=status)
        self.refresh()

    def _delete_rule(self):
        rule_id = self._selected(self.rule_tree)
        if rule_id is None:
            messagebox.showinfo(TITLE, 'Select a rule first.', parent=self.window)
            return
        rule = self.store.rule(rule_id)
        if rule and messagebox.askyesno(TITLE, 'Delete this rule? Future replies will stop using it immediately.\n\n' + rule["text"] +
                                        '\n\n(Deletion is recorded in learning history and can be undone.)', parent=self.window):
            self._guard(self.store.delete_rule, rule_id)
            self.refresh()

    # -- feedback ---------------------------------------------------------------------------

    def _build_feedback(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Feedback history')
        frame, self.feedback_tree = self._tree(tab, ('Time', 'Rating', 'Scene', 'Reason / preferred wording', 'Method', 'Status'),
                                               (120, 90, 90, 420, 90, 70), height=13)
        frame.pack(fill=tk.BOTH, expand=True)
        self.feedback_tree.bind("<<TreeviewSelect>>", lambda _event: self._feedback_detail())
        buttons = ttk.Frame(tab)
        buttons.pack(fill=tk.X, pady=6)
        for label, action in (('Change to Like', lambda: self._set_rating(1)), ('Change to Dislike', lambda: self._set_rating(-1)),
                              ('Add reason / preferred wording', self._teach_feedback),
                              ('Undo', self._revoke_feedback), ('Delete permanently', self._delete_feedback)):
            ttk.Button(buttons, text=label, command=action).pack(side=tk.LEFT, padx=(0, 6))
        self.feedback_box = self._detail_box(tab, 9)
        self.feedback_box.pack(fill=tk.BOTH, expand=False)

    def _fill_feedback(self):
        tree = self.feedback_tree
        tree.delete(*tree.get_children())
        for item in self.store.feedback_list(limit=1000):
            words = item["reason"] or (('Preferred wording: ' + item["ideal"]) if item["ideal"] else "")
            tree.insert("", tk.END, iid=str(item["id"]), values=(
                local_time(item["created_at"]), RATING_LABELS.get(item["rating"], ""),
                preferences.SCENES.get(item["scene"] or "", "—"), clip(words, 80) or '(no reason provided)',
                CHANNEL_LABELS.get(item["channel"], item["channel"]),
                'Withdrawn' if item["status"] == "revoked" else 'Active'))

    def _feedback_detail(self):
        feedback_id = self._selected(self.feedback_tree)
        if feedback_id is None:
            return
        item = self.store.feedback(feedback_id)
        if item is None:
            return
        lines = ['%s  %s  Scene: %s  Model: %s%s' % (
            local_time(item["created_at"]), RATING_LABELS.get(item["rating"], ""),
            preferences.SCENES.get(item["scene"] or "", "—"), item["model"] or 'Unknown',
            ("（%s）" % item["provider"]) if item["provider"] else ""),
            'Your question: ' + (item["question"] or '(none)'), "MYN's reply: " + (item["reply"] or '(none)')]
        if item["reason"]:
            lines.append('Your reason: ' + item["reason"])
        if item["ideal"]:
            lines.append('Your preferred wording: ' + item["ideal"])
        if item["about"]:
            lines.append('Applies to: ' + ABOUT_LABELS.get(item["about"], item["about"]))
        if item["analysis"]:
            try:
                import json
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
            lines.append('\nOriginal record: ')
            for event in events:
                lines.append("· %s %s %s %s" % (local_time(event["at"]), event["kind"],
                                                 RATING_LABELS.get(event["rating"], "") if event["rating"] is not None else "",
                                                 clip(event["value"], 80)))
        self._show(self.feedback_box, "\n".join(lines))

    def _set_rating(self, rating):
        feedback_id = self._selected(self.feedback_tree)
        if feedback_id is None:
            messagebox.showinfo(TITLE, 'Select a feedback item first.', parent=self.window)
            return
        item = self.store.feedback(feedback_id)
        if item and item["reply_id"] is not None and item["status"] == "active":
            # Through the engine so the learning follows the new rating.
            self._guard(self.engine.rate, item["reply_id"], rating)
        else:
            self._guard(self.store.set_rating, feedback_id, rating)
        self.refresh()

    def _teach_feedback(self):
        feedback_id = self._selected(self.feedback_tree)
        item = self.store.feedback(feedback_id) if feedback_id is not None else None
        if item is None:
            messagebox.showinfo(TITLE, 'Select a feedback item first.', parent=self.window)
            return
        if item["reply_id"] is None:
            messagebox.showinfo(TITLE, 'This feedback is not linked to a specific reply. It can only be withdrawn or deleted.', parent=self.window)
            return
        FeedbackDialog(self.window, self.engine, item["reply_id"], item["reply"],
                       lambda *_args: self.refresh(), self.font_family)

    def _revoke_feedback(self):
        feedback_id = self._selected(self.feedback_tree)
        if feedback_id is None:
            messagebox.showinfo(TITLE, 'Select a feedback item first.', parent=self.window)
            return
        if messagebox.askyesno(TITLE, 'Withdraw this feedback? It will stop contributing to ratings and unsupported inferred rules will be removed. The original record is preserved.',
                               parent=self.window):
            self._guard(self.store.revoke_feedback, feedback_id)
            self.refresh()

    def _delete_feedback(self):
        feedback_id = self._selected(self.feedback_tree)
        if feedback_id is None:
            messagebox.showinfo(TITLE, 'Select a feedback item first.', parent=self.window)
            return
        if messagebox.askyesno(TITLE, 'Permanently delete this feedback and its original record? This cannot be undone '
                                      '(older copies remain in previous automatic backups).', parent=self.window):
            self._guard(self.store.delete_feedback, feedback_id)
            self.refresh()

    # -- examples -----------------------------------------------------------------------------

    def _build_examples(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Examples')
        ttk.Label(tab, text='Replies you liked and wording you corrected. MYN may refer to one or two in similar scenes without copying their content.',
                  foreground=theme.MUTED).pack(anchor="w")
        frame, self.example_tree = self._tree(tab, ('Type', 'Scene', 'Content', 'Time'), (90, 100, 560, 120), height=14)
        frame.pack(fill=tk.BOTH, expand=True)
        self.example_tree.bind("<<TreeviewSelect>>", lambda _event: self._example_detail())
        buttons = ttk.Frame(tab)
        buttons.pack(fill=tk.X, pady=6)
        ttk.Button(buttons, text='Delete example', command=self._delete_example).pack(side=tk.LEFT)
        self.example_box = self._detail_box(tab, 7)
        self.example_box.pack(fill=tk.BOTH, expand=False)

    def _fill_examples(self):
        tree = self.example_tree
        tree.delete(*tree.get_children())
        for example in self.store.examples():
            content = example["ideal"] if example["kind"] == "correction" else example["original"]
            tree.insert("", tk.END, iid=str(example["id"]), values=(
                'Correction' if example["kind"] == "correction" else 'Liked reply',
                preferences.SCENES.get(example["scene"] or "", "—"), clip(content, 90),
                local_time(example["created_at"])))

    def _example_detail(self):
        example_id = self._selected(self.example_tree)
        found = [example for example in self.store.examples() if example["id"] == example_id]
        if not found:
            return
        example = found[0]
        lines = ['Question: ' + (example["question"] or '(none)'), "MYN's original reply: " + (example["original"] or '(none)')]
        if example["kind"] == "correction":
            lines.append('Your preferred wording: ' + example["ideal"])
        self._show(self.example_box, "\n\n".join(lines))

    def _delete_example(self):
        example_id = self._selected(self.example_tree)
        if example_id is None:
            messagebox.showinfo(TITLE, 'Select an example first.', parent=self.window)
            return
        if messagebox.askyesno(TITLE, 'Delete this example?', parent=self.window):
            self._guard(self.store.delete_example, example_id)
            self.refresh()

    # -- learning history -------------------------------------------------------------------------

    def _build_history(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Learning history')
        frame, self.history_tree = self._tree(tab, ('Time', 'Change', 'Actor', 'Rule', 'Description'),
                                              (120, 100, 60, 380, 260), height=18)
        frame.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(tab)
        buttons.pack(fill=tk.X, pady=6)
        ttk.Button(buttons, text='Undo this step', command=self._undo).pack(side=tk.LEFT)
        ttk.Label(buttons, text='Learned the wrong thing? Undo that step to restore the preceding rules.',
                  foreground=theme.MUTED).pack(side=tk.LEFT, padx=8)

    def _fill_history(self):
        tree = self.history_tree
        tree.delete(*tree.get_children())
        for event in self.store.history(limit=1000):
            tree.insert("", tk.END, iid=str(event["id"]), values=(
                local_time(event["at"]), ACTION_LABELS.get(event["action"], event["action"]),
                ACTOR_LABELS.get(event["actor"], event["actor"]), clip(event.get("rule_text") or "", 60),
                clip(event["note"], 50)))

    def _undo(self):
        event_id = self._selected(self.history_tree)
        if event_id is None:
            messagebox.showinfo(TITLE, 'Select a learning-history entry first.', parent=self.window)
            return
        if self._guard(self.store.undo_history, event_id) is False:
            messagebox.showinfo(TITLE, 'This step cannot be undone (for example, a settings change or feedback deletion).', parent=self.window)
        self.refresh()

    # -- effect --------------------------------------------------------------------------------

    def _build_effect(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text='Effectiveness')
        self.effect_box = self._detail_box(tab, 30)
        self.effect_box.pack(fill=tk.BOTH, expand=True)

    def _fill_effect(self):
        report = preferences.evaluate(self.store)

        def rate(window):
            if window["satisfaction"] is None:
                return 'No ratings'
            return 'Satisfaction %d%% (Like %d / Dislike %d)' % (round(window["satisfaction"] * 100), window["likes"],
                                                  window["dislikes"])

        lines = ['Last %d days: %s' % (report["recent_days"], rate(report["recent"])),
                 'Previous %d days: %s' % (report["recent_days"], rate(report["before"])), "", 'Weekly:']
        for week in report["weeks"]:
            lines.append('  Through %s  %s' % (week["week_ending"], rate(week)))
        lines += ["", 'Recurring issues:']
        lines += ['  %s: %d times' % (item["label"], item["count"]) for item in report["recurring_issues"]] or ['  None yet']
        lines += ["", 'Rule effectiveness (usage and subsequent ratings):']
        for effect in report["rules"]:
            lines.append('  "%s"  Used %d times, Like %d / Dislike %d, %s' % (
                clip(effect["text"], 40), effect["applied"], effect["liked"], effect["disliked"], effect["verdict"]))
        if not report["rules"]:
            lines.append('  No rules yet')
        if report["ineffective"]:
            lines += ["", 'Rules with no sustained positive effect (consider pausing or deleting):']
            lines += ["  「%s」" % clip(effect["text"], 60) for effect in report["ineffective"]]
        lines += ["", '%d feedback items await reflection (processed gradually when the API is available; your ratings are already saved locally)'
                  % self.store.pending_count()]
        self._show(self.effect_box, "\n".join(lines))

    # -- data ------------------------------------------------------------------------------------

    def _build_data(self):
        tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(tab, text='Data')
        self.path_label = ttk.Label(tab, foreground=theme.MUTED, wraplength=900, justify=tk.LEFT)
        self.path_label.pack(anchor="w", pady=(0, 10))
        for label, action, note in (
                ('Export all preference data', self._export, 'Create a JSON file containing rules, feedback, examples and learning history for import after reinstallation or on another computer.'),
                ('Import...', self._import, 'Merge an exported file. A backup is created first; duplicate records are not inserted.'),
                ('Back up now', self._backup, 'Save to preferences/backups. Existing backups are preserved.'),
                ('Restore from backup...', self._restore, 'Replace the preference database with the selected backup. The current file is renamed and preserved.'),
                ('Open folder', lambda: open_folder(self.store.folder), "")):
            row = ttk.Frame(tab)
            row.pack(fill=tk.X, pady=3)
            ttk.Button(row, text=label, command=action, width=18).pack(side=tk.LEFT)
            ttk.Label(row, text=note, foreground=theme.MUTED).pack(side=tk.LEFT, padx=10)

    def _export(self):
        path = self._guard(self.store.export)
        if path:
            messagebox.showinfo(TITLE, 'Export completed:\n' + str(path), parent=self.window)

    def _import(self):
        selected = filedialog.askopenfilename(parent=self.window, title='Choose a preference export file',
                                              initialdir=str(self.store.folder / "exports"),
                                              filetypes=(("JSON", "*.json"), ('All', "*.*")))
        if not selected:
            return
        added = self._guard(self.store.import_file, selected)
        if added is not None:
            messagebox.showinfo(TITLE, 'Import completed: %d rules, %d feedback items and %d examples added.' % (
                added["rules"], added["feedback"], added["examples"]), parent=self.window)
        self.refresh()

    def _backup(self):
        path = self._guard(self.store.backup, "manual")
        if path:
            messagebox.showinfo(TITLE, 'Backup completed:\n' + str(path), parent=self.window)

    def _restore(self):
        selected = filedialog.askopenfilename(parent=self.window, title='Choose a backup to restore',
                                              initialdir=str(self.store.folder / "backups"),
                                              filetypes=(("SQLite", "*.db"),))
        if not selected:
            return
        if not messagebox.askyesno(TITLE, 'Replace current preferences with this backup? The current file will be renamed and preserved.', parent=self.window):
            return
        kept = self._guard(self.store.restore, selected)
        if kept is not None or self.store.path.exists():
            messagebox.showinfo(TITLE, 'Restored.' + (('\nPrevious file preserved as:\n' + str(kept)) if kept else ""),
                                parent=self.window)
        self.refresh()

    # -- all ---------------------------------------------------------------------------------------

    def refresh(self):
        if not self.window.winfo_exists():
            return
        trees = (self.rule_tree, self.feedback_tree, self.example_tree, self.history_tree)
        kept = [tree.selection() for tree in trees]  # a refresh keeps what you had selected
        try:
            self.paused.set(self.store.learning_paused)
            self.applied.set(self.store.apply_enabled)
            self._fill_rules()
            self._fill_feedback()
            self._fill_examples()
            self._fill_history()
            self._fill_effect()
            summary = self.engine.summary()
            self.status.configure(text='%s active rules | %s candidates | %s feedback items' % (
                summary.get("active_rules", "?"), summary.get("candidates", "?"), summary.get("feedback", "?")))
            self.path_label.configure(text='Saved at: %s\n(feedback.db contains the original data; style_profile.json, '
                                           'preferred_examples.jsonl and style_history.jsonl are readable copies)'
                                           % Path(self.store.folder))
            for tree, chosen in zip(trees, kept):
                still = [iid for iid in chosen if tree.exists(iid)]
                if still:
                    tree.selection_set(still)
        except Exception as error:  # a damaged store: say so, keep the window usable
            self.status.configure(text='Preferences are temporarily unavailable: %s' % clip(error, 80))
