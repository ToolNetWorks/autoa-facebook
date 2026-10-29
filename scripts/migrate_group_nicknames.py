"""Explicit V19 migration; defaults to a read-only plan. Never run on import."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrations.v019_group_nicknames import plan, apply


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as source:
        print(json.dumps(plan(source)))
        if not args.apply:
            return
        if args.backup_dir is None:
            parser.error('--apply requires --backup-dir; stop workers and obtain deployment approval first')
        folder = args.backup_dir.resolve()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = folder / f'nickname-v19-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}.sqlite3'
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as target:
            source.backup(target)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        baseline_fk = list(conn.execute('PRAGMA foreign_key_check'))
        conn.execute('BEGIN IMMEDIATE')
        apply(conn)
        if list(conn.execute('PRAGMA foreign_key_check')) != baseline_fk:
            raise ValueError('Migration introduced a foreign key violation')
    print(json.dumps({'applied': 19, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
