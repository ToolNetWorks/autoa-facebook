"""Private append-only JSONL events with recursive credential redaction.

Appending preserves earlier events; serializing writers avoids interleaved records.
This module does not collect browser DOM, cookies or storage state.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

HIDDEN = '[REDACTED]'
PRIVATE_NAME = re.compile(r'^c_user$|^xs$|^datr$|passw(?:or)?d|passwd|token|secret|cookie|authorization|session|profile|storage.?state', re.I)
CREDENTIAL = re.compile(r'\b(c_user|xs|datr|token|password|passwd|cookie|secret|session)\s*[:=]\s*[^\s,;]+', re.I)
BEARER = re.compile(r'\bBearer\s+[^\s,;]+', re.I)


def new_run_id():
    return uuid.uuid4().hex


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def redact_sensitive(value, *, key=None):
    if key is not None and PRIVATE_NAME.search(str(key)):
        return HIDDEN
    if isinstance(value, Mapping):
        clean = {}
        for name in value:
            clean[str(name)] = redact_sensitive(value[name], key=name)
        return clean
    if isinstance(value, (list, tuple)):
        return list(map(redact_sensitive, value))
    if isinstance(value, (str, Path)):
        return CREDENTIAL.sub(lambda match: match[1] + '=' + HIDDEN,
                              BEARER.sub('Bearer ' + HIDDEN, str(value)))
    return value


class JsonlEventLogger:
    def __init__(self, path, *, run_id=None):
        self.path = Path(path).expanduser().absolute()
        self.run_id = run_id or new_run_id()
        self._closed = False

    def emit(self, event, **fields):
        if self._closed:
            return False
        record = {**fields, 'ts': now_iso(), 'run_id': self.run_id, 'event': event}
        line = json.dumps(redact_sensitive(record), ensure_ascii=False, separators=(',', ':')) + '\n'
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(line)
                stream.flush()
            return True
        except OSError:
            return False

    def close(self):
        self._closed = True


class RunLogger:
    def __init__(self, log_dir, run_id=None, name='run'):
        self.log_dir = Path(log_dir).expanduser().absolute()
        self.run_id = run_id or new_run_id()
        self.logger = JsonlEventLogger(self.log_dir / (name + '.jsonl'), run_id=self.run_id)

    def emit(self, event, **fields):
        return self.logger.emit(event, **fields)

    def close(self):
        self.logger.close()


def default_log_dir(data_dir):
    return Path(data_dir).expanduser().absolute() / 'logs'
