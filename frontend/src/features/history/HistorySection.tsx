
import { useMemo, useState } from 'react';
import { Badge, Card, DataTable, EmptyState, Stat, Tabs } from '@/components/ui/Surfaces';
import { jobStateLabel, jobStateTone } from '@/lib/statusLabels';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'posted', label: 'Đã đăng' },
  { id: 'pending', label: 'Chờ duyệt' },
  { id: 'failed', label: 'Lỗi' },
];

export function HistorySection({ workspace }: { workspace: WorkspaceData }) {
  const [filter, setFilter] = useState('all');
  const jobs = workspace.jobs;

  const counts = useMemo(
    () => ({
      posted: jobs.filter((job) => job.state === 'posted').length,
      pending: jobs.filter((job) => ['pending_approval', 'submitted_unverified'].includes(job.state)).length,
      failed: jobs.filter((job) => ['failed', 'needs_review'].includes(job.state)).length,
    }),
    [jobs],
  );

  const visible = jobs.filter((job) => {
    if (filter === 'posted') return job.state === 'posted';
    if (filter === 'pending') return ['pending_approval', 'submitted_unverified'].includes(job.state);
    if (filter === 'failed') return ['failed', 'needs_review'].includes(job.state);
    return true;
  });

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Lịch sử</h2>
          <p className="hint">Toàn bộ bài đã lên lịch của Workspace này.</p>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Tổng bài" value={jobs.length} />
        <Stat label="Đã đăng" value={counts.posted} />
        <Stat label="Chờ duyệt" value={counts.pending} />
        <Stat label="Lỗi" value={counts.failed} />
      </div>

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? jobs.length
              : entry.id === 'posted'
                ? counts.posted
                : entry.id === 'pending'
                  ? counts.pending
                  : counts.failed,
        }))}
        active={filter}
        onChange={setFilter}
      />

      {visible.length === 0 ? (
        <EmptyState title="Chưa có lịch sử" description="Các bài đã đăng sẽ hiện ở đây." />
      ) : (
        <Card>
          <DataTable head={['Bài đăng', 'Nhóm', 'Chế độ', 'Trạng thái', 'Cập nhật lúc']}>
            {visible.map((job) => (
              <tr key={job.id}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '300px' }}>
                    {job.body || 'Không có caption'}
                  </div>
                </td>
                <td className="truncate" style={{ maxWidth: '180px' }}>
                  {job.group_name}
                </td>
                <td>
                  <Badge tone={job.requested_posting_mode === 'nickname' ? 'info' : 'neutral'}>
                    {job.requested_posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
                  </Badge>
                </td>
                <td>
                  <Badge tone={jobStateTone(job.state)}>
                    {jobStateLabel(job.state)}
                  </Badge>
                </td>
                <td>{new Date(job.updated_at).toLocaleString('vi-VN')}</td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}
    </div>
  );
}
