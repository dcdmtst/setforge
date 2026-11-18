"""Atomic files, portable snapshots and a single-writer run lock."""

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    for attempt in range(8):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(.01 * (attempt + 1))


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows readers can briefly hold a handle without delete sharing. Keep
    # atomic replacement, retry only this transient class, and remain bounded.
    for attempt in range(8):
        try:
            os.replace(temp, path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(.01 * (attempt + 1))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(root, name):
    root = Path(root).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Path escapes its root: {name}")
    return target


def event(run, event_type, **data):
    with (Path(run) / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(redact(json.dumps({"time": now(), "event": event_type, **data}, ensure_ascii=False)) + "\n")


def redact(text):
    for name, value in os.environ.items():
        if len(value) > 8 and re.search(r"key|token|secret|password", name, re.I):
            text = text.replace(value, "[REDACTED]")
    return text


@contextmanager
def locked(run):
    lock = Path(run) / ".lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("Run is locked. If its process has stopped, use `setforge unlock --run ...`.")
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)
