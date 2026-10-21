"""Offline restore helper: wait for the originating GUI to exit, then swap and restart."""
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'core'))
import portable_backup


def wait_for_exit(pid):
    if os.name != 'nt':
        raise RuntimeError('This restore entry point requires Windows.')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # process already exited
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 120000) != 0:
            raise RuntimeError('MYN is still running. No data was restored.')
    finally:
        kernel.CloseHandle(handle)


def main(request):
    request = Path(request).resolve()
    job = json.loads(request.read_text(encoding='utf-8'))
    root = Path(job['root']).resolve()
    if request != portable_backup.transfer_dir(root) / 'request.json':
        raise ValueError('Invalid restore request path.')
    exited = False
    try:
        wait_for_exit(int(job['pid']))
        exited = True
        old = portable_backup.install(root, job['stage'])
        text = 'Full backup restored. The previous data is preserved at:\n' + str(old)
    except Exception as error:
        text = 'Restore incomplete. Original data preserved: ' + str(error)
    portable_backup._json(request.parent / 'result.json', {'text': text})
    request.unlink()
    if exited:
        # The just-imported, user-selected saved credentials take precedence over inherited keys.
        env = dict(os.environ)
        for name, variable in portable_backup.SECRET_NAMES.items():
            if (root / 'config' / name).exists():
                env.pop(variable, None)
        subprocess.Popen([sys.executable, str(Path(__file__).with_name('myn_gui.py'))], env=env,
                         cwd=str(Path(__file__).resolve().parent.parent),
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)


if __name__ == '__main__':
    main(sys.argv[1])
