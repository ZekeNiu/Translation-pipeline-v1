"""Append-only usage observations; no credentials, prompts or invented prices."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import threading

_lock = threading.Lock()


def append_usage(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'recorded_at': datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def read_usage(path):
    if not Path(path).is_file():
        return []
    result = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            result.append({'incomplete_record': True})
    return result
