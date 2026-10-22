"""Local first-start restore, available before credentials or models are configured."""
import os
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


def needed(root):
    from data_location import legacy_candidates
    root = Path(root)
    return not (root / 'database' / 'jarvis.db').exists() and not any(
        (folder / 'jarvis.db').exists() for folder in legacy_candidates(root / 'database'))


def restore_empty(root, archive, password):
    """Never replace an existing library through the first-start entry point."""
    import portable_backup
    root = Path(root)
    if not needed(root):
        raise ValueError('Existing data found. Open MYN and import from there.')
    stage = portable_backup.prepare(root, archive, password)
    if not needed(root):
        raise ValueError('Data appeared during verification. Nothing was replaced.')
    return portable_backup.install(root, stage)


def offer(root):
    if not needed(root):
        return True
    import portable_backup
    # A second first-start window cannot restore while this one is preparing data.
    import msvcrt
    lock_path = portable_backup.transfer_dir(root) / 'first-start.lock'
    lock = lock_path.open('a+b')
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return False
    try:
        if not needed(root):
            return True
        window = tk.Tk()
        window.title('Welcome to MYN')
        window.geometry('520x270')
        window.resizable(False, False)
        import app_icon
        app_icon.apply_tk(window)
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Welcome back', font=('Segoe UI', 20)).pack(anchor='w')
        ttk.Label(frame, text='Restore your conversations, memories and settings from a full backup.\n'
                  'Your password stays on this computer.', wraplength=465).pack(anchor='w', pady=16)
        status = ttk.Label(frame, text='Choose a .mynbackup file to continue.', wraplength=465)
        status.pack(anchor='w', pady=8)
        progress = ttk.Progressbar(frame, mode='indeterminate')
        events = queue.Queue()
        result = {'continue': False, 'busy': False}

        def close():
            if not result['busy']:
                window.destroy()

        def new():
            result['continue'] = True
            close()

        def import_backup():
            archive = filedialog.askopenfilename(parent=window, title='Choose full backup',
                       filetypes=[('MYN full backup', '*.mynbackup')])
            if not archive:
                return
            password = simpledialog.askstring('Backup password', 'Enter the password for this backup.',
                                              parent=window, show='*')
            if password is None:
                return
            result['busy'] = True
            restore_button.configure(state='disabled')
            new_button.configure(state='disabled')
            status.configure(text='Checking and restoring your backup...')
            progress.pack(fill='x', pady=8)
            progress.start()
            def work():
                try:
                    restore_empty(root, archive, password)
                    events.put(None)
                except Exception as error:
                    events.put(str(error))
            threading.Thread(target=work, daemon=True).start()
            window.after(100, poll)

        def poll():
            try:
                error = events.get_nowait()
            except queue.Empty:
                window.after(100, poll)
                return
            result['busy'] = False
            progress.stop()
            if error:
                progress.pack_forget()
                status.configure(text='Restore did not finish. Your original files are unchanged.')
                restore_button.configure(state='normal')
                new_button.configure(state='normal')
                messagebox.showerror('Restore backup', error, parent=window)
            else:
                result['continue'] = True
                window.destroy()

        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        restore_button = ttk.Button(row, text='Import full backup', command=import_backup)
        restore_button.pack(side='left')
        new_button = ttk.Button(row, text='Start fresh', command=new)
        new_button.pack(side='left', padx=12)
        window.protocol('WM_DELETE_WINDOW', close)
        window.mainloop()
        return result['continue']
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()
