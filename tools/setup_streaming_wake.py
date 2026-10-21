"""Install model assets for the offline keyword detector (run with worker Python).

Dependencies: uv pip install --python local-tts/.venv/Scripts/python.exe
    sherpa-onnx==1.12.40 pypinyin==0.55.0
No personal files, microphone recordings or cloud API calls are involved.
"""
from pathlib import Path
import hashlib
import json
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
NAME = 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
URL = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/' + NAME + '.tar.bz2'


def main():
    folder = ROOT / 'local-stt' / 'kws'
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / 'model.tar.bz2'
    if not archive.exists():
        urllib.request.urlretrieve(URL, archive)
    with tarfile.open(archive) as data:
        data.extractall(folder, filter='data')
    manifest = dict(source=URL, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (folder / NAME).glob('*') if p.is_file()})
    (folder / 'model-source.json').write_text(json.dumps(manifest, indent=2)+'\n',encoding='utf-8')
    print('Local streaming wake model ready.')


if __name__ == '__main__':
    main()
