"""Group worker boundary and read-only queue integrity diagnostics."""
from __future__ import annotations

import json
from pathlib import Path


def audit_queue(conn, pipeline_id: int) -> list[dict]:
    """Prepared jobs may lack post_id; dispatched jobs must have same-tenant links."""
    findings = []
    rows = conn.execute('''SELECT aj.id,aj.post_id,aj.account_id,aj.group_id,aj.status,
        p.pipeline_id post_pipeline FROM auto_post_jobs aj LEFT JOIN posts p ON p.id=aj.post_id
        WHERE aj.pipeline_id=? AND aj.status IN ('queued','running','posted','submitted_unverified','pending_approval')''',
        (pipeline_id,)).fetchall()
    for row in rows:
        code = ''
        if row['post_id'] is None:
            code = 'AUTO_JOB_MISSING_POST'
        elif row['post_pipeline'] != pipeline_id:
            code = 'AUTO_JOB_INVALID_POST_OWNER'
        elif not conn.execute('''SELECT 1 FROM jobs WHERE post_id=? AND pipeline_id=?
                AND account_id=? AND group_id=?''',
                (row['post_id'], pipeline_id, row['account_id'], row['group_id'])).fetchone():
            code = 'AUTO_JOB_MISSING_WORKER_JOB'
        if code:
            findings.append({'auto_job_id': row['id'], 'pipeline_id': pipeline_id, 'code': code})
    return findings


def mark_blocked(conn, job: dict, state: str, details: str, now: str) -> None:
    conn.execute('''UPDATE jobs SET state=?,posting_status=?,details=?,failure_reason=?,updated_at=?
        WHERE id=? AND pipeline_id=?''', (state, state, details, details, now, job['id'], job['pipeline_id']))
    conn.execute('''UPDATE auto_post_jobs SET status=?,error=?,defer_reason=?,updated_at=?
        WHERE post_id=? AND pipeline_id=? AND account_id=? AND group_id=?''',
        (state, details, details, now, job['post_id'], job['pipeline_id'], job['account_id'], job['group_id']))


def worker_payload(job: dict, data_root: Path) -> dict:
    media = json.loads(job['media_json'])
    if len(media) != 1:
        raise ValueError('One owned image is required')
    root = (data_root / 'uploads' / str(int(job['pipeline_id']))).resolve()
    path = (data_root / media[0]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Invalid owned media path')
    return {key: job[key] for key in ('pipeline_id', 'group_id', 'account_id', 'post_id')} | {
        'job_id': job['id'], 'group_url': job['group_url'], 'body': job['body'],
        'media': [str(path)], 'anonymous_only': True,
        'posting_mode': job.get('requested_posting_mode') or 'anonymous',
        'nickname': job.get('requested_nickname') or '',
    }


def validated_result_state(result: dict, job: dict) -> str:
    """Reject unsupported success claims at the subprocess/DB boundary."""
    status = result.get('state', 'needs_review')
    if status not in {'failed','needs_review','submitted_unverified','pending_approval','posted'}:
        return 'needs_review'
    if status != 'posted':
        return status
    if result.get('verification') != 'verified' or not verified_group_post_link(
            result.get('permalink'), job.get('group_url') or job.get('url')):
        return 'submitted_unverified'
    return 'posted'


def verified_group_post_link(permalink, expected_group) -> bool:
    """A Group landing URL or another Group's post can never be post evidence."""
    from urllib.parse import urlparse
    from services.posting_preflight import same_group
    import re
    try:
        link = urlparse(str(permalink or ''))
        return bool(link.scheme == 'https' and not link.username and not link.password
                    and link.port in (None,443)
                    and re.fullmatch(r'/groups/[^/]+/(?:posts|permalink)/[^/]+/?',link.path)
                    and same_group(str(permalink),str(expected_group or '')))
    except ValueError:
        return False
