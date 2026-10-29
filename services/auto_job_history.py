"""Tenant-scoped history with evidence-aware status and matching DB counters."""
CATEGORIES = {
    'waiting': ('pending', 'approved'), 'processing': ('queued', 'running'),
    'needs_review': ('needs_review', 'pending_approval', 'submitted_unverified'),
    'posted': ('posted', 'published'), 'failed': ('failed', 'blocked', 'cancelled'),
}

CTE = """WITH history AS (
    SELECT j.*, CASE WHEN j.status IN ('posted','published') AND NOT EXISTS (
        SELECT 1 FROM jobs w JOIN groups g ON g.id=w.group_id AND g.pipeline_id=w.pipeline_id
        WHERE w.post_id=j.post_id AND w.pipeline_id=j.pipeline_id
          AND w.account_id=j.account_id AND w.group_id=j.group_id
          AND w.state='posted' AND w.verification_status='verified'
          AND w.permalink=j.permalink AND w.permalink<>''
          AND fpm_valid_group_post_link(w.permalink,g.url)=1
    ) THEN 'submitted_unverified' ELSE j.status END display_status
    FROM auto_post_jobs j WHERE j.pipeline_id=?
) """


def read_history(conn, pipeline_id: int, status: str | None, limit: int) -> dict:
    from services.group_job_lifecycle import verified_group_post_link
    conn.create_function('fpm_valid_group_post_link',2,verified_group_post_link,deterministic=True)
    counts={'all':0, **{name:0 for name in CATEGORIES}}
    for state, count in conn.execute(CTE+'SELECT display_status,COUNT(*) FROM history GROUP BY display_status', (pipeline_id,)):
        counts['all']+=count
        for category,states in CATEGORIES.items():
            if state in states:
                counts[category]+=count
    query=CTE+'''SELECT j.*,g.label group_label,g.url group_url,m.original_name media_name,a.label account_label
        FROM history j LEFT JOIN groups g ON g.id=j.group_id AND g.pipeline_id=j.pipeline_id
        LEFT JOIN accounts a ON a.id=j.account_id AND a.pipeline_id=j.pipeline_id
        LEFT JOIN media_assets m ON m.id=j.media_id AND m.pipeline_id=j.pipeline_id'''
    params=[pipeline_id]
    if status and status!='all':
        states=CATEGORIES.get(status,(status,))
        query+=' WHERE j.display_status IN ('+','.join('?' for _ in states)+')'
        params.extend(states)
    query+=' ORDER BY j.id DESC LIMIT ?';params.append(min(500,max(1,limit)))
    jobs=[]
    for row in conn.execute(query,params):
        job=dict(row);job['status']=job.pop('display_status')
        if job.get('error') or job.get('defer_reason'):
            code,message=public_failure(job.get('error') or job.get('defer_reason'),job['status'])
            job['error_code']=code
            job['error']=message
            job['defer_reason']=message if job.get('defer_reason') else ''
        for hidden in ('pipeline_id','created_by_ctv_id','ctv_config_id'):
            job.pop(hidden,None)
        jobs.append(job)
    return {'ok':True,'jobs':jobs,'counts':counts}


def public_failure(details: str, state: str) -> tuple[str,str]:
    """Map legacy diagnostics on the server; never expose worker/DOM/SQL text."""
    text=str(details or '').casefold()
    if state in ('submitted_unverified','pending_approval'):
        return 'SUBMISSION_REVIEW_REQUIRED','Cần đối chiếu kết quả trên Facebook; không gửi lại bài.'
    if any(word in text for word in ('checkpoint','captcha','xác minh bảo mật')):
        return 'FACEBOOK_SECURITY_CHECK','Chủ tài khoản cần xử lý xác minh bảo mật trên Facebook.'
    if any(word in text for word in ('expired','login','đăng nhập')):
        return 'FACEBOOK_LOGIN_REQUIRED','Cần đăng nhập lại Facebook rồi kiểm tra phiên tài khoản.'
    if any(word in text for word in ('nickname','biệt danh','anonymous','ẩn danh')):
        return 'POSTING_IDENTITY_UNVERIFIED','Chưa xác minh được danh tính đăng theo yêu cầu. Vui lòng kiểm tra lại.'
    if any(word in text for word in ('restricted','bị hạn chế','temporarily blocked')):
        return 'FACEBOOK_RESTRICTED','Facebook đang hạn chế thao tác. Vui lòng kiểm tra trực tiếp trên Facebook.'
    return 'PUBLISHING_REVIEW_REQUIRED','Chưa thể hoàn tất bài đăng. Vui lòng kiểm tra tài khoản, nhóm và nội dung.'


def confirmed_recent_count(conn, pipeline_id: int) -> int:
    """Use the same verified execution evidence as history, including legacy labels."""
    from services.group_job_lifecycle import verified_group_post_link
    conn.create_function('fpm_valid_group_post_link',2,verified_group_post_link,deterministic=True)
    return conn.execute(CTE + """SELECT COUNT(*) FROM history
        WHERE display_status IN ('posted','published')
        AND datetime(updated_at)>=datetime('now','-24 hours')""",(pipeline_id,)).fetchone()[0]
