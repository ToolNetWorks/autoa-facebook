#!/usr/bin/env python3
"""Read-only release gate. Does not import the application or apply migrations."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from migrations.v020_unique_active_membership import plan


def inspect(database: Path) -> dict:
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory=sqlite3.Row
    try:
        schema = {row[1] for row in connection.execute('SELECT * FROM pragma_table_info(?)', ('group_accounts',))}
        missing = sorted({'facebook_nickname','nickname_status','confirmed_at','last_verified_at'} - schema)
        conflicts = connection.execute("""SELECT pipeline_id,COUNT(*) FROM pipeline_memberships
            WHERE status='active' GROUP BY pipeline_id HAVING COUNT(*)>1""").fetchall()
        users = connection.execute("""SELECT user_id,COUNT(*) FROM pipeline_memberships
            WHERE status='active' GROUP BY user_id HAVING COUNT(*)>1""").fetchall()
        running = connection.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
        pending = connection.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
        uncertain = connection.execute("""SELECT COUNT(*) FROM jobs
            WHERE state IN ('submitted_unverified','pending_approval') AND attempts>0""").fetchone()[0]
        migration = plan(connection)
        constraints = not migration['missing_indexes']
        return {'nickname_columns_missing':missing,'pipeline_membership_conflicts':[tuple(row) for row in conflicts],
                'user_membership_conflicts':[tuple(row) for row in users],'running_jobs':running,'queued_jobs':pending,
                'unresolved_submissions':uncertain,'v20_indexes_present':constraints,
                'schema_compatible':not missing,'membership_ready':not conflicts and not users,
                'deploy_ready':not missing and not conflicts and not users and not running and not pending and constraints,
                'publish_authorized':False,'data_modified':False}
    finally:
        connection.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True,type=Path)
    report=inspect(parser.parse_args().database)
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report['deploy_ready'] else 2)
