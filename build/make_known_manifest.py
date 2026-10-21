"""Record exact known old program trees; unknown files prevent cleanup."""

import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PRIOR = Path(r"D:\chatgpt\2026-10-16\referenced-chatgpt-conversation-this-is-an")
SOURCE = PRIOR / "work" / "myn-v0.13" / "source-backup"
TREES = {
    "0.13.0": PRIOR / "outputs" / "MYN-0.13.0",
    "0.13.1": Path(r"D:\MYN\versions\0.13.1"),
}
ROOT_NAMES = ("brain.py", "jarvis.py", "local_tools.py", "rag_chat.py",
              "README.md", "semantic_search.py", "storage.py")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest().upper()


def entries(root):
    if not root.is_dir():
        raise FileNotFoundError(root)
    found = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Symlink in known program tree")
        relative = path.relative_to(root).as_posix()
        found[relative] = digest(path) if path.is_file() else None
    return found


def main():
    manifest = {
        "root_files": {name: digest(SOURCE / name) for name in ROOT_NAMES},
        "versions": {version: entries(path) for version, path in TREES.items()},
    }
    output = Path(__file__).with_name("known-program-files.json")
    output.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")), encoding="utf-8")
    print("Known program manifest:", {key: len(value) for key, value in manifest["versions"].items()},
          "root files:", len(manifest["root_files"]))


if __name__ == "__main__":
    main()
