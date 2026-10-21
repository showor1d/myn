"""Shared complete-backup actions for the page and classic window."""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import portable_backup
from data_location import root_for_db


def busy(win):
    return (any(getattr(win, name, False) for name in
                ('busy', 'processing', 'draft_active', 'self_fix', 'evolution_run',
                 'evolution_mining', 'evolution_applying', 'pending_confirmation', 'backup_working')) or
            bool(getattr(win, 'agent_runner', None) and win.agent_runner.busy))


def transfer(win, importing=False):
    if busy(win):
        win._show_error('Backup', 'Wait for the current reply, background tasks and data processing to finish before backing up or importing.')
        return
    root = root_for_db(win.session.db_path)
    folder = root / 'portable-backups'
    folder.mkdir(exist_ok=True)
    options = dict(parent=win.root, initialdir=str(folder), filetypes=[('MYN full backup', '*.mynbackup')])
    if importing:
        chosen = filedialog.askopenfilename(title='Choose full backup', **options)
    else:
        chosen = filedialog.asksaveasfilename(title='Save full backup', defaultextension='.mynbackup',
                    initialfile='MYN-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.mynbackup', **options)
    if not chosen:
        return
    password = simpledialog.askstring('Backup password',
        'Enter the password for this backup.' if importing else 'Set a password of at least 8 characters. You will need it to import on another computer; a lost password cannot be recovered.',
        show='●', parent=win.root)
    if password is None:
        return
    if len(password) < 8:
        win._show_error('Backup', 'The backup password must contain at least 8 characters.')
        return
    if not importing and password != simpledialog.askstring('Confirm backup password', 'Enter the backup password again.', show='●', parent=win.root):
        win._show_error('Backup', 'Passwords do not match. No backup was created.')
        return

    def begin(yes=True):
        if not yes:
            return
        if busy(win):
            win._show_error('Backup', 'A task is running. Please try again later.')
            return
        win.backup_working = True
        def show(text, bad=False):
            if getattr(win, 'web_active', False) and win.sheets.is_open():
                win.sheets.toast(text, 'bad' if bad else 'good')
            else:
                (messagebox.showerror if bad else messagebox.showinfo)('Backup', text, parent=win.root)
        show('Checking and preparing restore...' if importing else 'Creating a full backup. The saved location will appear when it is ready.')

        def worker():
            try:
                result = portable_backup.prepare(root, chosen, password) if importing else portable_backup.create(root, chosen, password)
                error = None
            except Exception as exc:
                result, error = None, str(exc)
            def finished():
                win.backup_working = False
                if error:
                    show('Backup operation incomplete: ' + error, True)
                elif not importing:
                    show('Full backup saved:\n' + str(result))
                else:
                    # Recheck work that may have begun during archive verification.
                    def when_idle():
                        if busy(win):
                            win.root.after(1000, when_idle)
                            return
                        win.backup_working = True
                        try:
                            launch_restore(root, result)
                        except Exception as exc:
                            win.backup_working = False
                            show('Restore has not started: ' + str(exc), True)
                            return
                        win.close()
                    when_idle()
            win.events.put(('call', finished))
        threading.Thread(target=worker, daemon=True).start()

    if importing:
        win._ask_user('Import full backup', 'The backup will be checked and MYN restarted to restore all included data, preferences, settings and media.'
                      'Your current data will be preserved locally for recovery. Microphones, speakers and external folders may need to be selected again on a new computer.'
                      '\n\nImport this backup?', begin, yes='Import and restart', no='Cancel')
    else:
        begin()


def launch_restore(root, stage):
    directory = portable_backup.transfer_dir(root)
    request = directory / 'request.json'
    if request.exists():
        raise ValueError('A restore request is already pending. Restart MYN first.')
    portable_backup._json(request, {'root': str(root), 'stage': str(stage), 'pid': os.getpid()})
    command = [sys.executable, str(Path(__file__).with_name('backup_restore_worker.py')), str(request)]
    try:
        subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent.parent),
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
    except Exception:
        request.unlink()
        raise
