"""Atomic request manifests with immutable page checksums and resumable cursors."""
import json
import os
from pathlib import Path

from common import digest, read


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


class BarCache:
    """One writer per request key; pages preceding a failure remain reusable."""
    def __init__(self, root):
        self.root = Path(root)

    def directory(self, key):
        return self.root / digest(key)

    def load(self, key):
        folder = self.directory(key)
        path = folder / "manifest.json"
        state = read(path) if path.exists() else dict(key=key, complete=False, pages=[], cursor=None, errors=[])
        if state["key"] != key:
            raise ValueError("cache key mismatch")
        if state["complete"] and (not state["pages"] or state["cursor"] is not None):
            raise ValueError("inconsistent complete cache manifest")
        bars = []
        for page in state["pages"]:
            payload = read(folder / page["file"])
            if digest(payload) != page["sha256"]:
                raise ValueError("cache page checksum mismatch")
            if not isinstance(payload, list) or len(payload) != page["bars"]:
                raise ValueError("cache page count mismatch")
            bars.extend(payload)
        return state, bars

    def page(self, key, state, rows, cursor):
        folder = self.directory(key)
        name = "page-%06d.json" % len(state["pages"])
        atomic_write(folder / name, rows)
        state["pages"].append(dict(file=name, sha256=digest(rows), bars=len(rows)))
        state["cursor"] = cursor
        self.save(key, state)

    def save(self, key, state):
        atomic_write(self.directory(key) / "manifest.json", state)
