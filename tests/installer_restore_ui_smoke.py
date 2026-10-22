"""Exercise the real Tk first-start restore with synthetic encrypted data."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing
import tkinter as tk
from tkinter import ttk
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import first_run
import portable_backup


def main():
    with tempfile.TemporaryDirectory(prefix='myn-first-start-ui-') as folder:
        base = Path(folder)
        original, restored = base / 'old', base / 'new'
        (original / 'database').mkdir(parents=True)
        import storage
        storage.init_db(original / 'database/jarvis.db')
        with closing(sqlite3.connect(original / 'database/jarvis.db')) as db:
            db.execute('CREATE TABLE example (value TEXT)')
            db.execute("INSERT INTO example VALUES ('UI restore proof')")
            db.commit()
        archive = base / 'example.mynbackup'
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'ELEVENLABS_API_KEY': ''}):
            portable_backup.create(original, archive, 'synthetic-password')
        original_tk = tk.Tk
        def create():
            window = original_tk()
            def start():
                def buttons(widget):
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Button) and child.cget('text') == 'Import full backup':
                            child.invoke()
                            return True
                        if buttons(child):
                            return True
                    return False
                assert buttons(window)
            window.after(300, start)
            window.after(20000, window.destroy)
            return window
        with mock.patch('data_location.legacy_candidates', return_value=[]), \
             mock.patch.object(first_run.tk, 'Tk', create), \
             mock.patch.object(first_run.filedialog, 'askopenfilename', return_value=str(archive)), \
             mock.patch.object(first_run.simpledialog, 'askstring', return_value='synthetic-password'), \
             mock.patch.object(first_run.messagebox, 'showerror') as errors:
            assert first_run.offer(restored)
            errors.assert_not_called()
        with closing(sqlite3.connect(restored / 'database/jarvis.db')) as db:
            assert db.execute('SELECT value FROM example').fetchone()[0] == 'UI restore proof'
        print('PASS: actual Tk welcome imports a verified encrypted backup without API credentials.')


if __name__ == '__main__':
    main()
