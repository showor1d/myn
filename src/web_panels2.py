"""More settings screens drawn inside the main view (see sheets.py and web_panels.py):
Agents plus the database-backup component retained by the legacy backup adapter."""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import backup
import credentials
import memory_query
import message_log
import personal_profile
from data_location import root_for_db, user_data_dir
from sheets import Panel
from web_panels import VoicePanel


def _gui(win, name):
    """A helper from MYN's window module (it may run as __main__)."""
    return getattr(sys.modules[type(win).__module__], name)


def _when(win, value):
    try:
        return _gui(win, "tokyo_display")(value)
    except Exception:
        return str(value or "")


# ---- Agents: fixed model, API key and shared voice controls ---------------------------------------------


def _places():
    """Where browsing starts: the drives (Windows) and the usual folders of this user."""
    import os
    import string
    home = Path.home()
    places = []
    if os.name == "nt":
        places += ["%s:\\" % letter for letter in string.ascii_uppercase if os.path.exists("%s:\\" % letter)]
    else:
        places.append("/")
    for name in ("Desktop", "Documents", "Downloads", "Pictures"):
        if (home / name).is_dir():
            places.append(str(home / name))
    places.append(str(home))
    return places


def _subfolders(path, limit=400):
    """The folders inside path (no hidden / system ones), sorted by name."""
    found = []
    try:
        with __import__("os").scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith((".", "$")) or entry.name in ("System Volume Information",):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        found.append(entry.name)
                except OSError:
                    continue
                if len(found) >= limit:
                    break
    except OSError:
        return None
    return sorted(found, key=str.casefold)


class AgentsPanel(VoicePanel):
    key = 'agents'
    title = 'Agents'
    width = 760

    def body(self):
        source = credentials.claude_key_source(user_data_dir())
        claude = [{'t':'p','text':'Opus 5.5'},
            {'t':'group','title':'Claude API Key','children':[
                {'t':'p','tone':'muted','text':{'environment':'Using the environment variable key.','saved':'A key is stored encrypted on this computer.','none':'No key configured.'}[source]},
                {'t':'field','id':'claude_key','kind':'password','value':'','placeholder':'Paste new key'},
                {'t':'buttons','items':[{'action':'claude_save_key','label':'Save key'},
                                      {'action':'claude_clear_key','label':'Clear local key','kind':'danger'}]}]}]
        return claude + super().body()

    def act(self, action, values):
        win, data = self.win, user_data_dir()
        if action == "claude_save_key":
            try:
                credentials.save_key(data, values.get("claude_key") or "")
            except (ValueError, OSError, RuntimeError):
                self.toast('Could not save the key. Check the input and data-folder permissions.', "bad")
                return
            self.toast('Claude key saved with encryption.', "good")
            self.redraw()
            try:
                message_log.retry_pending_now(win.session.db_path)
                win.process_pending()
            except Exception:
                self.toast('Key saved, but pending-message processing could not start. It will retry later.')
        elif action == "claude_clear_key":
            def answered(yes):
                if not yes:
                    return
                try:
                    credentials.clear_key(data)
                except OSError:
                    self.toast('Could not clear the local key.', "bad")
                    return
                self.toast('Local key cleared.', "good")
                self.redraw()
            self.confirm('Clear the locally encrypted Claude key? Environment variables will remain unchanged.', answered,
                         title='Clear Claude API key', yes='Clear', danger=True)
        else:
            super().act(action, values)



# Backups.

class LibraryPanel(Panel):
    key = "personal"
    title = 'Backup'
    width = 720

    def body(self):
        db = self.win.session.db_path
        library_root = root_for_db(db)
        try:
            state = personal_profile.status(db)
            backups = personal_profile.list_backups(db)
        except (OSError, sqlite3.Error) as error:
            return [{"t": "p", "tone": "bad", "text": str(error)}]
        self.backups = {path.name: path for path in backups}
        counts = state["counts"]
        latest = _when(self.win, datetime.fromtimestamp(backups[0].stat().st_mtime, memory_query.TOKYO).isoformat()) \
            if backups else 'None yet'
        return [
            {"t": "p", "tone": "muted", "mono": True, "text": str(library_root)},
            {"t": "cards", "items": [
                {"name": 'Database', "value": "%.1f KiB" % (state["size_bytes"] / 1024)},
                {"name": 'Active memories', "value": "%d / %d" % (state["active_memories"], counts["memories"])},
                {"name": 'Original conversation', "value": str(counts["conversation_messages"])},
                {"name": 'Backup', "value": str(state["backup_count"]), "detail": 'Latest: ' + latest}]},
            {"t": "buttons", "items": [{"action": "folder", "label": 'Open folder'}]},
            {"t": "h", "text": 'Restorable database backups'},
            {"t": "table", "id": "backup", "rows": [{"key": path.name, "cells": [path.name]} for path in backups],
             "selected": self.v("backup"), "height": 220, "empty": 'No backups yet'},
            {"t": "buttons", "items": [{"action": "export", "label": 'Export readable conversations and memories'},
                                       {"action": "create", "label": 'Back up conversations and preferences'},
                                       {"action": "restore", "label": 'Restore selected database only', "kind": "danger"}]},
        ]

    def act(self, action, values):
        win = self.win
        db = win.session.db_path
        if action == "folder":
            try:
                os.startfile(str(root_for_db(db)))
            except (AttributeError, OSError) as error:
                self.toast('Could not open folder: %s' % error, "bad")
        elif action == "create":
            try:
                path = backup.backup_database(db, label="manual")
                win.session.preferences.store.backup("manual")
            except (OSError, sqlite3.Error) as error:
                self.toast(str(error), "bad")
                return
            self.toast('Backup created: %s' % path.name, "good")
            self.redraw()
        elif action == "export":
            try:
                path = personal_profile.export_profile(db)
            except (OSError, sqlite3.Error) as error:
                self.toast(str(error), "bad")
                return
            self.toast('Export completed: %s' % path, "good")
        elif action == "restore":
            chosen = getattr(self, "backups", {}).get(values.get("backup") or "")
            if chosen is None:
                self.toast('Choose a backup first.')
                return
            if win.busy or win.processing:
                self.toast('Wait for the current reply and memory processing to finish.')
                return

            def answered(yes):
                if not yes:
                    return
                try:
                    previous = personal_profile.restore_backup(win.session.db_path, chosen)
                    win._reconnect_session()
                except (OSError, ValueError, sqlite3.Error) as error:
                    self.toast(str(error), "bad")
                    return
                self.toast('Restore completed; previous database saved at: %s' % previous, "good")
                self.redraw()

            self.confirm('Back up the current database, then restore:\n%s\n\nContinue?' % chosen, answered, title='Restore backup',
                         yes='Restore', danger=True)


PANELS = {
    "agents": AgentsPanel,
}
