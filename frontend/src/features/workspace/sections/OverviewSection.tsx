
import { Link } from 'react-router-dom';
import { Card, Stat } from '@/components/ui/Surfaces';
import { Badge } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function OverviewSection({ workspace }: { workspace: WorkspaceData }) {
  const { pipeline, pipelineId, groups, accounts, jobs } = workspace;
  const screening = groups.reduce<Record<string, number>>((acc, group) => {
    acc[group.screening_status] = (acc[group.screening_status] ?? 0) + 1;
    return acc;
  }, {});
  const pending = jobs.filter((job) => ['queued', 'running', 'pending_approval'].includes(job.state)).length;
  const failed = jobs.filter((job) => ['failed', 'needs_review'].includes(job.state)).length;

  return (
    <div className="stack">
      <Card
        title={pipeline?.name ?? 'Workspace'}
        description={pipeline?.description || 'Chưa có mô tả cho Workspace này.'}
      >
        <div className="stat-row">
          <Stat label="Tài khoản" value={accounts.length} />
          <Stat label="Nhóm" value={groups.length} />
          <Stat label="Đăng trực tiếp" value={screening.priority ?? 0} />
          <Stat label="Cần duyệt" value={screening.review ?? 0} />
          <Stat label="Chờ đăng" value={pending} />
          <Stat label="Lỗi" value={failed} />
        </div>
      </Card>

      <div className="card-grid wide">
        <Card title="Nhóm sẵn sàng đăng tự động" description="Group đã được xác minh quyền đăng.">
          <div className="stack-sm">
            {groups.filter((group) => ['priority', 'review'].includes(group.screening_status)).length === 0 ? (
              <span className="hint">Chưa có group nào được xác minh.</span>
            ) : (
              groups
                .filter((group) => ['priority', 'review'].includes(group.screening_status))
                .map((group) => (
                  <div className="row-between" key={group.id}>
                    <span className="truncate">{group.label}</span>
                    <Badge tone={group.screening_status === 'priority' ? 'ok' : 'warn'}>
                      {group.screening_status === 'priority' ? 'Đăng trực tiếp' : 'Cần duyệt'}
                    </Badge>
                  </div>
                ))
            )}
          </div>
        </Card>

        <Card title="Bước tiếp theo">
          <div className="stack-sm">
            <Link className="btn sm" to={`/workspace/${pipelineId ?? ''}/groups`}>
              Quản lý nhóm
            </Link>
            <Link className="btn sm" to={`/workspace/${pipelineId ?? ''}/album`}>
              Tải ảnh lên Album
            </Link>
            <Link className="btn sm" to={`/workspace/${pipelineId ?? ''}/auto-posting`}>
              Cấu hình tự động đăng
            </Link>
            <Link className="btn sm" to={`/workspace/${pipelineId ?? ''}/safety`}>
              Mở Safety Center
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
