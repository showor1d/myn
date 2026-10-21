"""Password encrypted, verified personal-library transfers; no program code is restored.

SQLite files use online backup. Restore runs only after the GUI process exits,
uses a directory transaction, and keeps the entire former library for recovery.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAGIC = b'MYNBACKUP\x01'
FORMAT = 'myn-complete-backup'
CHUNK = 1024 * 1024
SKIP_DIRS = {'indexes', 'portable-backups'}
SECRET_NAMES = {'claude-key.dpapi': 'ANTHROPIC_API_KEY',
                'elevenlabs-key.dpapi': 'ELEVENLABS_API_KEY'}


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return Cipher, algorithms, modes, AESGCM
    except ImportError:
        raise ValueError('Backup encryption component missing. Install requirements-backup.txt first.') from None


def _key(password, salt):
    if not isinstance(password, str) or len(password) < 8 or len(password) > 1024:
        raise ValueError('Backup password must contain 8-1024 characters. Keep it safe; a lost password cannot be recovered.')
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000, 32)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def _json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temporary), str(path))


def transfer_dir(root):
    import data_location
    root = Path(root).resolve()
    directory = root.parent / ('.' + root.name + '-transfers')
    # Same per-account ACL as the main library. This also protects staging/rollback files.
    data_location.ensure_layout(directory)
    return directory


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _files(root, filtered=True):
    for directory, folders, files in os.walk(str(root), followlinks=False):
        base = Path(directory)
        for folder in list(folders):
            path = base / folder
            if _linked(path):
                raise ValueError('Linked directory in personal data was not packaged: ' + str(path.relative_to(root)))
            if filtered and base == root and folder in SKIP_DIRS:
                folders.remove(folder)
        for name in sorted(files):
            path = base / name
            if _linked(path):
                raise ValueError('Linked file in personal data was not packaged: ' + str(path.relative_to(root)))
            if filtered and name.endswith(('-wal', '-shm', '-journal', '.lock', '.tmp', '.mynbackup')):
                continue
            yield path


def _sqlite(path):
    with path.open('rb') as handle:
        return handle.read(16) == b'SQLite format 3\x00'


def _check_db(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup database verification failed: ' + path.name)


def _crypt_file(source, target, password, encrypt):
    Cipher, algorithms, modes, _ = _crypto()
    from cryptography.exceptions import InvalidTag
    try:
        with Path(source).open('rb') as src, Path(target).open('xb') as dst:
            if encrypt:
                salt, nonce = os.urandom(16), os.urandom(12)
                header = MAGIC + salt + nonce
                ctx = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce)).encryptor()
                ctx.authenticate_additional_data(header)
                dst.write(header)
                for block in iter(lambda: src.read(CHUNK), b''):
                    dst.write(ctx.update(block))
                dst.write(ctx.finalize())
                dst.write(ctx.tag)
            else:
                header = src.read(len(MAGIC) + 28)
                if len(header) != len(MAGIC) + 28 or not header.startswith(MAGIC):
                    raise ValueError('Unsupported MYN full backup.')
                salt, nonce = header[len(MAGIC):len(MAGIC)+16], header[-12:]
                src.seek(-16, 2)
                tag, left = src.read(16), src.tell() - 16 - len(header)
                if left < 0:
                    raise ValueError('Backup file is incomplete.')
                ctx = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce, tag)).decryptor()
                ctx.authenticate_additional_data(header)
                src.seek(len(header))
                while left:
                    block = src.read(min(CHUNK, left))
                    if not block:
                        raise ValueError('Backup file is incomplete.')
                    left -= len(block)
                    dst.write(ctx.update(block))
                dst.write(ctx.finalize())
    except InvalidTag:
        raise ValueError('Incorrect password or damaged backup. Original data was not changed.') from None


def create(root, target, password):
    """A portable archive includes saved/effective API keys, never in plaintext on disk."""
    import credentials
    root, target = Path(root).resolve(), Path(target).resolve()
    if target.exists():
        raise ValueError('Destination already exists. Choose another filename; existing backups will not be overwritten.')
    _crypto()
    _key(password, os.urandom(16))
    if not (root / 'database/jarvis.db').is_file():
        raise ValueError('Personal database not found.')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    if temporary.exists():
        raise ValueError('An unfinished backup uses this destination filename. Choose another name.')
    with tempfile.TemporaryDirectory(prefix='export-', dir=str(transfer_dir(root))) as work:
        work = Path(work)
        raw = work / 'data.zip'
        records, secrets = {}, {}
        for name, env in SECRET_NAMES.items():
            value = os.environ.get(env, '').strip()
            path = root / 'config' / name
            if not value and path.exists():
                value = credentials._crypt(path.read_bytes(), False).decode('utf-8')
            if value:
                secrets[name] = value
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path in _files(root):
                relative = path.relative_to(root).as_posix()
                if path in (target, temporary) or relative in {'config/' + n for n in SECRET_NAMES}:
                    continue
                copied = work / 'snapshot'
                is_db = _sqlite(path)
                if is_db:
                    if copied.exists():
                        copied.unlink()
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
                        with closing(sqlite3.connect(str(copied))) as dest:
                            source.backup(dest)
                    _check_db(copied)
                else:
                    # Copy once, then hash and archive that exact snapshot, even if the source changes.
                    shutil.copyfile(path, copied)
                records[relative] = {'sha256': digest(copied), 'size': copied.stat().st_size,
                                     'sqlite': is_db}
                archive.write(copied, 'data/' + relative)
            # The intermediate ZIP contains only separately encrypted credentials.
            salt, nonce = os.urandom(16), os.urandom(12)
            encrypted = _crypto()[3](_key(password, salt)).encrypt(
                nonce, json.dumps(secrets).encode('utf-8'), MAGIC)
            manifest = {'format': FORMAT, 'version': 1, 'created': datetime.now(timezone.utc).isoformat(),
                        'source_root': str(root), 'files': records,
                        'credentials': (salt + nonce + encrypted).hex(),
                        'excluded': ['rebuildable indexes', 'previous portable archives', 'temporary locks']}
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        try:
            _crypt_file(raw, temporary, password, True)
            # Authentication + file hashes verified before the final file becomes visible.
            check = work / 'verified.zip'
            _crypt_file(temporary, check, password, False)
            with zipfile.ZipFile(check) as archive:
                if archive.testzip() is not None:
                    raise ValueError('Backup verification failed.')
            # On Windows rename refuses a file created concurrently; never replace an old backup.
            temporary.rename(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return target


def _safe_name(name):
    parts = PurePosixPath(name).parts
    if (not parts or '\\' in name or ':' in name or name.startswith('/') or
            any(p in ('', '..', '.') or p.endswith((' ', '.')) for p in name.split('/')) or
            any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM%d' % n for n in range(10)),
                                           *('LPT%d' % n for n in range(10))} for p in parts)):
        raise ValueError('Backup contains unsafe file paths.')
    return parts


def _rebase_settings(stage, old_root, new_root):
    """Rebase known file locations, never rewrite historical conversation text."""
    def remap(value):
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, str):
            normalized, old = value.replace('\\', '/'), old_root.replace('\\', '/').rstrip('/')
            if normalized.casefold().startswith(old.casefold() + '/'):
                return str(new_root.joinpath(*normalized[len(old)+1:].split('/')))
        return value
    for name in ('file-access.json', 'agent-settings.json'):
        path = stage / 'config' / name
        if path.exists():
            _json(path, remap(json.loads(path.read_text(encoding='utf-8'))))
    # Program copies must be rebuilt against the installed program, never applied from another PC.
    path = stage / 'config/evolution-points.json'
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        rows = data.get('points', []) if isinstance(data, dict) else data
        for point in rows:
            if point.get('work') or point.get('status') in ('building', 'updating', 'built', 'planning'):
                point['work'] = None
                point['status'] = 'failed'
                point['reason'] = 'Restored from backup; plan retained for verification after you confirm a retry.'
        _json(path, data)


def prepare(root, archive_path, password):
    """Validate and stage without touching the current library; return a private stage directory."""
    import credentials
    _crypto()
    from cryptography.exceptions import InvalidTag
    root = Path(root).resolve()
    directory = transfer_dir(root)
    stage = Path(tempfile.mkdtemp(prefix='restore-', dir=str(directory)))
    raw = directory / (stage.name + '.zip')
    try:
        _crypt_file(archive_path, raw, password, False)
        with zipfile.ZipFile(raw) as archive:
            info = archive.getinfo('manifest.json')
            if info.file_size > 64 * CHUNK:
                raise ValueError('Backup manifest is too large.')
            manifest = json.loads(archive.read(info))
            if manifest.get('format') != FORMAT or manifest.get('version') != 1:
                raise ValueError('Unsupported backup format or version. Use the same or a newer MYN version.')
            records = manifest['files']
            if not isinstance(records, dict) or len(records) > 100000:
                raise ValueError('Invalid backup file manifest.')
            names = archive.namelist()
            if len(names) != len(set(n.casefold() for n in names)) or set(names) != {'manifest.json', *('data/' + n for n in records)}:
                raise ValueError('Backup contains duplicate or unregistered files.')
            if sum(archive.getinfo('data/' + n).file_size for n in records) > shutil.disk_usage(directory).free:
                raise ValueError('Insufficient disk space for a safe restore.')
            for name, record in records.items():
                parts = _safe_name(name)
                if parts[0] in SKIP_DIRS or name.endswith(('-wal', '-shm', '.lock')) or name == 'config/claude-key.dpapi' or name == 'config/elevenlabs-key.dpapi':
                    raise ValueError('Backup contains files that cannot be restored directly.')
                info = archive.getinfo('data/' + name)
                if stat.S_ISLNK(info.external_attr >> 16) or info.file_size != record['size']:
                    raise ValueError('Invalid backup file type or size.')
                dest = stage.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, dest.open('xb') as out:
                    shutil.copyfileobj(src, out, CHUNK)
                if digest(dest) != record['sha256']:
                    raise ValueError('Backup file verification failed: ' + name)
                if record.get('sqlite'):
                    _check_db(dest)
            database = stage / 'database/jarvis.db'
            _check_db(database)
            with closing(sqlite3.connect(str(database))) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
                    raise ValueError('Not a MYN personal database.')
            pref = stage / 'preferences/feedback.db'
            if pref.exists():
                import preferences
                with closing(sqlite3.connect(str(pref))) as db:
                    if preferences.Store._version(db) > preferences.SCHEMA_VERSION:
                        raise ValueError('Communication preferences come from a newer version. Update MYN first.')
            secret = bytes.fromhex(manifest['credentials'])
            try:
                keys = json.loads(_crypto()[3](_key(password, secret[:16])).decrypt(secret[16:28], secret[28:], MAGIC))
            except InvalidTag:
                raise ValueError('Backup credential verification failed.') from None
            for name, value in keys.items():
                if name not in SECRET_NAMES or not isinstance(value, str) or len(value) > 1024:
                    raise ValueError('Invalid backup credential format.')
                path = stage / 'config' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(credentials._crypt(value.encode('utf-8'), True))
            _rebase_settings(stage, manifest['source_root'], root)
            migration = stage / 'config/migration.json'
            previous = json.loads(migration.read_text(encoding='utf-8')) if migration.exists() else None
            _json(migration, {'kind': 'complete-backup', 'imported_at': datetime.now(timezone.utc).isoformat(),
                              'previous': previous})
        _json(directory / (stage.name + '.verified.json'), {
            path.relative_to(stage).as_posix(): digest(path) for path in _files(stage, filtered=False)})
        return stage
    except Exception:
        shutil.rmtree(stage)
        raise
    finally:
        if raw.exists():
            raw.unlink()


def recover(root):
    """Repair an interrupted directory swap before the GUI opens any databases."""
    root = Path(root).resolve()
    directory = transfer_dir(root)
    journal = directory / 'transaction.json'
    if not journal.exists():
        return
    job = json.loads(journal.read_text(encoding='utf-8'))
    old, stage = directory / job['old'], directory / job['stage']
    if old.parent != directory or stage.parent != directory or not old.name.startswith('before-') or not stage.name.startswith('restore-'):
        raise ValueError('Invalid restore transaction path; no data changed.')
    if old.exists():
        if root.exists():
            # Keep the interrupted candidate too; never delete personal data during recovery.
            root.rename(directory / ('interrupted-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
        old.rename(root)
    journal.unlink()


def install(root, stage):
    """OFFLINE ONLY. Atomic directory swap with a durable rollback journal."""
    root, stage = Path(root).resolve(), Path(stage).resolve()
    directory = transfer_dir(root)
    if stage.parent != directory or not stage.name.startswith('restore-') or _linked(stage):
        raise ValueError('Invalid restore directory.')
    if (directory / 'transaction.json').exists():
        raise ValueError('The previous restore is incomplete. Restart MYN first.')
    expected = json.loads((directory / (stage.name + '.verified.json')).read_text(encoding='utf-8'))
    actual = {path.relative_to(stage).as_posix(): digest(path) for path in _files(stage, filtered=False)}
    if expected != actual:
        raise ValueError('Restore files changed after verification; original data was preserved.')
    _check_db(stage / 'database/jarvis.db')
    old = directory / ('before-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    _json(directory / 'transaction.json', {'old': old.name, 'stage': stage.name})
    try:
        if root.exists():
            root.rename(old)
        stage.rename(root)
        (directory / 'transaction.json').unlink()
    except Exception:
        recover(root)
        raise
    return old
