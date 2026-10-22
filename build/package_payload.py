"""Build an allowlisted base payload from explicitly selected, working runtimes.

Run with the speech Python 3.12. No user libraries, venv launchers or credentials
are copied. Distribution RECORDs and dependency markers determine package files.
"""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

SKIP = {'__pycache__', '.git', '.cache'}


def copy_tree(source, destination, skip=()):
    source, destination = Path(source), Path(destination)
    for folder, dirs, files in os.walk(source):
        dirs[:] = [name for name in dirs if name not in SKIP and name not in skip]
        for name in dirs:
            path = Path(folder) / name
            if path.is_symlink() or path.stat().st_file_attributes & 0x400:
                raise ValueError('Linked build directory: ' + str(path))
        for name in files:
            if name in skip:
                continue
            path = Path(folder) / name
            if path.is_symlink() or path.stat().st_file_attributes & 0x400:
                raise ValueError('Linked build input: ' + str(path))
            if path.suffix in ('.pyc', '.pyo'):
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def interpreter(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('python.exe', 'pythonw.exe', 'python3.dll', 'LICENSE.txt',
                 'vcruntime140.dll', 'vcruntime140_1.dll'):
        shutil.copy2(source / name, destination / name)
    for path in source.glob('python3*.dll'):
        shutil.copy2(path, destination / path.name)
    for name in ('Lib', 'DLLs', 'tcl'):
        copy_tree(source / name, destination / name, {'site-packages', 'test', 'idlelib', 'ensurepip'})


def packages(source, destination, roots=None, python_version='3.12', defer=()):
    source, destination = Path(source), Path(destination)
    available = {canonicalize_name(d.metadata['Name']): d for d in metadata.distributions(path=[str(source)])}
    chosen, pending = {}, list(roots or available)
    while pending:
        name = canonicalize_name(pending.pop())
        if name in chosen:
            continue
        dist = available[name]
        chosen[name] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({'extra': '', 'python_version': python_version,
                                                       'python_full_version': python_version + '.0'}):
                continue
            dep = canonicalize_name(req.name)
            if dep not in available:
                raise ValueError('Missing dependency: ' + str(req))
            if req.specifier and available[dep].version not in req.specifier:
                raise ValueError('Dependency version mismatch: ' + str(req))
            pending.append(dep)
    for name, dist in sorted(chosen.items()):
        if name in defer:
            continue
        for entry in dist.files or []:
            relative = Path(str(entry))
            if '..' in relative.parts or relative.is_absolute() or '__pycache__' in relative.parts or relative.suffix == '.pyc':
                continue
            path = source / relative
            if path.is_symlink() or path.stat().st_file_attributes & 0x400:
                raise ValueError('Linked distribution file')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return {name: dist.version for name, dist in sorted(chosen.items())}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--live', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--git', type=Path, required=True)
    args = parser.parse_args()
    out, live = args.output, args.live
    if out.exists():
        raise ValueError('Choose a new payload directory; existing artifacts are never overwritten.')
    out.mkdir(parents=True)
    for name in ('src', 'tests', 'build'):
        copy_tree(args.source / name, out / name)
    (out / 'tools').mkdir()
    shutil.copy2(args.source / 'tools/dev_finish.py', out / 'tools/dev_finish.py')
    shutil.copy2(args.source / 'AGENTS.md', out / 'AGENTS.md')
    interpreter(Path(r'C:\Program Files\Python39'), out / 'runtime/main')
    locks = {'main': packages(Path(r'C:\Program Files\Python39\Lib\site-packages'),
             out / 'runtime/main/Lib/site-packages',
             ['pywebview', 'pygame', 'cryptography'], '3.9')}
    base = live / 'local-tts/python/cpython-3.12-windows-x86_64-none'
    for kind in ('speech', 'agent'):
        interpreter(base, out / 'runtime' / kind)
    locks['speech'] = packages(live / 'local-tts/.venv/Lib/site-packages',
        out / 'runtime/speech/Lib/site-packages',
        ['torch', 'transformers', 'sounddevice', 'soundfile', 'sherpa-onnx', 'pypinyin', 'librosa'], defer={'torch'})
    locks['agent'] = packages(live / 'local-agent/.venv/Lib/site-packages',
        out / 'runtime/agent/Lib/site-packages')
    copy_tree(args.git, out / 'runtime/git')
    for name in ('whisper-large-v3-turbo', 'kws'):
        copy_tree(live / 'local-stt' / name, out / 'local-stt' / name,
                  {'model.safetensors'} if name == 'whisper-large-v3-turbo' else ())
    (out / 'dependency-versions.json').write_text(json.dumps(locks, indent=2), encoding='utf-8')
    csc = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(csc), '/nologo', '/target:winexe', '/platform:x64',
                    '/reference:System.Windows.Forms.dll', '/out:' + str(out / 'MYN.exe'),
                    '/win32icon:' + str(out / 'src/assets/myn-symbol.ico'),
                    str(args.source / 'build/launcher.cs')], check=True)
    manifest = {p.relative_to(out).as_posix(): digest(p) for p in out.rglob('*') if p.is_file()}
    (out / 'package-manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    print(json.dumps({'files': len(manifest), 'bytes': sum(p.stat().st_size for p in out.rglob('*') if p.is_file()),
                      'distributions': {k: len(v) for k, v in locks.items()}}))


if __name__ == '__main__':
    main()
