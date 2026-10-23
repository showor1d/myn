"""Pinned, verified speech downloads for MYN Setup; no pip or system installation.

Only the public PyTorch wheel and Whisper weights are fetched. Private data and
credentials are never read. Receipts restrict uninstall to unchanged owned files.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
TORCH_ROOTS = {'torch', 'torchgen', 'functorch', 'torch-2.11.0+cu128.dist-info'}
MODEL = 'local-stt/whisper-large-v3-turbo/model.safetensors'
SITE = 'runtime/speech/Lib/site-packages/'


def inside(root, name):
    root = Path(root).resolve()
    parts = PurePosixPath(str(name).replace('\\', '/')).parts
    if not parts or any(p in ('..', '.') or ':' in p for p in parts) or PurePosixPath(str(name)).is_absolute():
        raise ValueError('Invalid component path')
    path = root.joinpath(*parts)
    path.resolve().relative_to(root)
    for parent in (path,) + tuple(path.parents):
        if parent == root:
            break
        if parent.exists() and (parent.is_symlink() or getattr(parent.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Linked component path')
    return path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(spec, target, report, opener=urllib.request.urlopen):
    target = Path(target)
    if target.exists() and digest(target) == spec['sha256']:
        return
    partial = target.with_suffix('.partial')
    target.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists() and digest(partial) == spec['sha256']:
        os.replace(partial, target)
        return
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(spec['url'], headers={'User-Agent': 'MYN-Setup/1.0',
                          **({'Range': 'bytes=%d-' % offset} if offset else {})})
        try:
            with opener(request, timeout=60) as response:
                resumed = offset and response.status == 206
                if resumed and not response.headers.get('Content-Range', '').startswith('bytes %d-' % offset):
                    raise ValueError('Invalid resumed download')
                if not resumed:
                    offset = 0
                length = int(response.headers.get('Content-Length', '0')) + offset
                with partial.open('ab' if resumed else 'wb') as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        stream.write(block)
                        offset += len(block)
                        report('Downloading speech components: %.0f / %.0f MB' % (offset / 1e6, length / 1e6),
                               offset / length if length else 0)
            report('Verifying the downloaded component...', 0)
            if digest(partial) != spec['sha256']:
                partial.unlink()
                raise ValueError('Downloaded component did not pass verification. Please retry setup.')
            os.replace(partial, target)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise
            report('Connection interrupted. Retrying the verified download...', 0)
            time.sleep(1)


def owned(name):
    return name == MODEL or (name.startswith(SITE) and name[len(SITE):].split('/')[0] in TORCH_ROOTS)


def install(root, report):
    root = Path(root).resolve()
    ready = inside(root, 'installation-ready.json')
    if ready.exists():
        ready.unlink()
    lock = json.loads((root / 'src/component-lock.json').read_text(encoding='utf-8'))
    receipt = inside(root, 'component-receipt.json')
    records = json.loads(receipt.read_text()).get('files', {}) if receipt.exists() else {}
    if any(not owned(name) for name in records):
        raise ValueError('Invalid component receipt')
    complete = records.get(MODEL) == lock['whisper']['sha256'] and all(inside(root, name).is_file() and digest(inside(root, name)) == value
                                   for name, value in records.items())
    if not complete:
        wheel = inside(root, 'runtime/.component-downloads/torch.whl')
        download(lock['torch'], wheel, report)
        report('Preparing speech recognition...', 0)
        records = {}
        backup = 'self-update/component-recovery-' + str(time.time_ns()) + '/'
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                relative = PurePosixPath(entry.filename)
                if '..' in relative.parts or relative.is_absolute() or '\\' in entry.filename:
                    raise ValueError('Unsafe component archive')
                if not relative.parts or relative.parts[0] not in TORCH_ROOTS:
                    continue
                name = SITE + entry.filename
                dest = inside(root, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                stage = inside(root, name + '.installing')
                with archive.open(entry) as source, stage.open('wb') as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                expected = digest(stage)
                if dest.exists() and digest(dest) != expected:
                    saved = inside(root, backup + name)
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, saved)
                os.replace(stage, dest)
                records[name] = expected
        if not (root / (SITE + 'torch/__init__.py')).is_file():
            raise ValueError('Speech runtime archive is incomplete')
        model = inside(root, MODEL)
        if model.exists() and digest(model) != lock['whisper']['sha256']:
            saved = inside(root, backup + MODEL)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model, saved)
        download(lock['whisper'], model, report)
        records[MODEL] = digest(model)
        temporary = inside(root, 'component-receipt.tmp')
        temporary.write_text(json.dumps({'files': records}, sort_keys=True), encoding='utf-8')
        os.replace(temporary, receipt)
        wheel.unlink()
        for folder in (wheel.parent,):
            try:
                folder.rmdir()
            except OSError:
                pass
    report('Checking installed components...', 0)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONHOME', 'PYTHONPATH', 'ANTHROPIC_API_KEY', 'ELEVENLABS_API_KEY')}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    check = subprocess.run([str(root / 'runtime/speech/python.exe'), '-I', '-B', '-c',
        'import torch,sounddevice,sherpa_onnx;from transformers import WhisperProcessor,WhisperForConditionalGeneration;assert torch.__version__=="2.11.0+cu128"'],
        env=env, capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if check.returncode:
        raise ValueError('Speech components could not load. Run setup again to repair the installation.')
    inside(root, 'installation-ready.json').write_text(json.dumps({'version': '1.0', 'ready': True}), encoding='utf-8')
    report('MYN is ready. Open it to import your full backup.', 1)


def uninstall(root):
    root = Path(root).resolve()
    receipt = inside(root, 'component-receipt.json')
    if not receipt.exists():
        return
    records = json.loads(receipt.read_text()).get('files', {})
    if any(not owned(name) for name in records):
        raise ValueError('Invalid component receipt')
    remaining = {}
    folders = set()
    for name, expected in records.items():
        path = inside(root, name)
        if path.is_file():
            if digest(path) != expected:
                remaining[name] = expected
                continue
            path.unlink()
            folders.update(p for p in path.parents if p != root and root in p.parents)
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass
    if remaining:
        receipt.write_text(json.dumps({'files': remaining}), encoding='utf-8')
    else:
        receipt.unlink()
    ready = inside(root, 'installation-ready.json')
    if ready.exists():
        ready.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--silent', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    if args.uninstall:
        uninstall(args.root)
        return 0
    if args.silent:
        try:
            install(args.root, lambda text, progress: print(text, flush=True))
            return 0
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title('Installing MYN')
    window.geometry('520x175')
    window.resizable(False, False)
    status = ttk.Label(window, text='Preparing MYN. Please keep your internet connection active.', wraplength=470)
    status.pack(padx=24, pady=24)
    bar = ttk.Progressbar(window, maximum=1)
    bar.pack(fill='x', padx=24)
    events = queue.Queue()
    result = {'code': 1}
    window.protocol('WM_DELETE_WINDOW', lambda: None)
    def work():
        try:
            install(args.root, lambda text, progress: events.put(('progress', text, progress)))
            events.put(('done', None, 0))
        except Exception as error:
            events.put(('done', str(error), 1))
    def poll():
        try:
            while True:
                kind, text, value = events.get_nowait()
                if kind == 'done':
                    result['code'] = value
                    if text:
                        messagebox.showerror('Installation incomplete', text + '\nReconnect and run MYN Setup again.', parent=window)
                    window.destroy()
                    return
                status.configure(text=text)
                bar['value'] = value
        except queue.Empty:
            window.after(100, poll)
    threading.Thread(target=work, daemon=True).start()
    window.after(100, poll)
    window.mainloop()
    return result['code']


if __name__ == '__main__':
    raise SystemExit(main())
