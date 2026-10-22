"""Retire registered development artifacts only after a confirmed healthy startup.

The durable queue is outside the disposable copy. No broad directory discovery,
backup pruning, user-data cleanup or recursive deletion is performed.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()
FORBIDDEN = {'.git', '.codex', '.aws', 'personal_data', 'data', 'backups',
             '.personal_data-transfers', '.venv', 'venv', 'node_modules',
             'local-agent', 'local-stt', 'local-tts', 'models', 'runtime'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(path, parent):
    path, parent = Path(path).absolute(), Path(parent).resolve()
    path.relative_to(parent)
    if path == parent:
        raise ValueError('The parent directory cannot be a cleanup target')
    for item in (path,) + tuple(path.parents):
        if item == parent:
            break
        if item.exists() and (item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Linked paths are not cleanup targets')
    path.resolve().relative_to(parent)
    return path


def _tree(work):
    files, directories = {}, []
    for parent, dirs, names in os.walk(work, followlinks=False):
        for name in dirs + names:
            path = _inside(Path(parent)/name, work)
            if name in FORBIDDEN or name.startswith('.env') or path.suffix.lower() in (
                    '.db', '.sqlite', '.sqlite3', '.dpapi', '.key', '.pem', '.pfx'):
                raise ValueError('Unexpected private or runtime data in working copy')
        directories.extend((Path(parent)/name).relative_to(work).as_posix() for name in dirs)
        for name in names:
            path = Path(parent)/name
            files[path.relative_to(work).as_posix()] = digest(path)
    return files, sorted(directories)


def _queue(root):
    return _inside(Path(root)/'self-update/cleanup', Path(root))


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_suffix('.tmp')
    stage.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(str(stage), str(path))


def schedule(work, backup, root=None, artifacts=()):
    """Register an installed copy created by self_update.prepare; never delete now."""
    import self_update
    root = Path(root or ROOT).resolve()
    work = _inside(work, root/'self-update/work')
    if work.parent != root/'self-update/work' or not work.is_dir():
        raise ValueError('Only a directly registered working copy can be retired')
    backup = _inside(backup, root/'self-update')
    if root/'self-update/work' in backup.parents or not backup.is_dir():
        raise ValueError('A separate recovery backup is required')
    base = self_update._base(work)
    if not base:
        raise ValueError('Missing trusted working-copy baseline')
    for name, expected in base.items():
        if digest(_inside(backup/name, backup)) != expected:
            raise ValueError('Recovery backup differs from the trusted baseline')
    installed = {name: digest(path) for name, path in self_update._program_files(work).items()}
    for name, expected in installed.items():
        if digest(_inside(root/name, root)) != expected:
            raise ValueError('Working copy has not been installed completely')
    files, directories = _tree(work)
    extra = {}
    for item in artifacts:
        path = _inside(item, root)
        if path.parent != root or not (path.name.startswith('.tmp-') or path.name.startswith('test-')) or path.suffix not in ('.png', '.log', '.json'):
            raise ValueError('Only explicitly listed root test outputs can be retired')
        extra[path.name] = digest(path)
    # One durable item per copy; repeated finish hooks must not duplicate cleanup.
    ident = hashlib.sha256(str(work).encode('utf-8')).hexdigest()[:24]
    path = _queue(root)/(ident+'.json')
    with _LOCK:
        if path.exists():
            existing = json.loads(path.read_text(encoding='utf-8'))
            if existing['files'] != files or existing['installed'] != installed:
                raise ValueError('A pending cleanup already owns this working copy')
            return ident
        _write(path, dict(id=ident, token=uuid.uuid4().hex, status='waiting_for_startup',
                         work=str(work), backup=str(backup), files=files, directories=directories,
                         baseline=digest(self_update._base_path(work)), original=base, installed=installed,
                         artifacts=extra, scheduled_at=time.time()))
    return ident


def cancel_reused_copy(work, root=None):
    """Preparing the same task again invalidates a previous startup's cleanup token."""
    root = Path(root or ROOT).resolve()
    work = _inside(work, root/'self-update/work')
    ident = hashlib.sha256(str(work).encode('utf-8')).hexdigest()[:24]
    with _LOCK:
        path = _queue(root)/(ident+'.json')
        if path.exists():
            path.unlink()


def startup_batch(root=None):
    """Capture only registrations present at this boot; later updates wait for theirs."""
    root = Path(root or ROOT)
    result = {}
    for path in _queue(root).glob('*.json'):
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(row, dict) or row.get('id') != path.stem or
                    not re.fullmatch(r'[0-9a-f]{24}', str(row.get('id', ''))) or
                    not re.fullmatch(r'[0-9a-f]{32}', str(row.get('token', '')))):
                continue
            result[row['id']] = row['token']
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def _retire(row, root):
    import self_update
    work = _inside(row['work'], root/'self-update/work')
    if work.parent != root/'self-update/work':
        raise ValueError('Invalid registered copy location')
    backup = _inside(row['backup'], root/'self-update')
    if root/'self-update/work' in backup.parents:
        raise ValueError('Invalid recovery location')
    for name, expected in row['installed'].items():
        if digest(_inside(root/name, root)) != expected:
            raise ValueError('Installed version changed; keep the development evidence')
    base_path = _inside(self_update._base_path(work), root/'self-update/work')
    if base_path.exists() and digest(base_path) != row['baseline']:
        raise ValueError('The working copy has been reused')
    current, directories = _tree(work) if work.exists() else ({}, [])
    partial = row['status'] == 'deleting'
    if (not partial and (current != row['files'] or directories != row['directories'])) or (
            partial and (any(row['files'].get(k) != v for k, v in current.items()) or
                         not set(directories) <= set(row['directories']))):
        raise ValueError('Working files changed after completion; retain them')
    for name, expected in row['original'].items():
        if digest(_inside(backup/name, backup)) != expected:
            raise ValueError('Recovery backup changed; retain working files')
    if not base_path.exists() and not partial:
        raise ValueError('Trusted baseline disappeared')
    evidence = backup/'verification/cleanup'
    evidence.mkdir(parents=True, exist_ok=True)
    if not partial:
        # Preserve complete, bounded root verification logs before retiring the copy.
        for name in current:
            source = work/name
            if '/' not in name and source.suffix in ('.log', '.json') and source.stat().st_size <= 8*1024*1024:
                target = evidence/name
                target.write_bytes(source.read_bytes())
                if digest(target) != current[name]:
                    raise OSError('Verification evidence copy failed')
        row['status'] = 'deleting'
        _write(_queue(root)/(row['id']+'.json'), row)
    for name, expected in current.items():
        path = _inside(work/name, work)
        if digest(path) != expected:
            raise ValueError('A working file changed during cleanup')
        path.unlink()
    for name in sorted(directories, key=lambda n: len(Path(n).parts), reverse=True):
        _inside(work/name, work).rmdir()  # only empty directories, never recursive deletion
    if work.exists():
        work.rmdir()
    if base_path.exists():
        base_path.unlink()
    retained = []
    for name, expected in row['artifacts'].items():
        path = _inside(root/name, root)
        if not path.exists():
            continue
        if digest(path) != expected:
            retained.append(name)
            continue
        if path.suffix in ('.log', '.json') and path.stat().st_size <= 8*1024*1024:
            target = evidence/('root-'+name)
            target.write_bytes(path.read_bytes())
            if digest(target) != expected:
                raise OSError('Root verification evidence copy failed')
        path.unlink()
    _write(evidence/'result.json', dict(status='completed', files=len(row['files']),
           retained_changed_artifacts=retained, completed_at=time.time()))


def finish_startup(batch, root=None):
    """Called in a background worker after the healthy-startup delay. Fail closed."""
    root = Path(root or ROOT).resolve()
    completed = []
    with _LOCK:
        if (root/'self-update/pending.json').exists():
            return completed
        for ident, token in batch.items():
            path = _inside(_queue(root)/(ident+'.json'), _queue(root))
            try:
                row = json.loads(path.read_text(encoding='utf-8'))
                if row['token'] != token or row['id'] != ident:
                    continue
                _retire(row, root)
                path.unlink()
                completed.append(ident)
            except (OSError, ValueError, KeyError, TypeError):
                # Do not mark a failed/partial cleanup complete; retry on a later boot.
                continue
    return completed
