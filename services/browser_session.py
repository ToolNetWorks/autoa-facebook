"""Persist a private Playwright state snapshot without damaging the last good one."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


async def atomic_storage_state(context, destination: str | Path) -> None:
    target = Path(destination).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Obtain and validate the whole snapshot before touching the existing file.
    state = await context.storage_state()
    if not isinstance(state, dict) or not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
        raise ValueError('Incomplete browser storage state')
    encoded = json.dumps(state, ensure_ascii=False).encode('utf-8')
    descriptor, name = tempfile.mkstemp(prefix='.session-', dir=target.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


class BrowserSessionManager:
    def __init__(self, context, lock, destination=None):
        self.context, self.lock = context, lock
        self.destination = destination
        self.closed = False

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.context and self.destination:
                await atomic_storage_state(self.context, self.destination)
        finally:
            try:
                if self.context:
                    await self.context.close()
            finally:
                if self.lock:
                    self.lock.release()
