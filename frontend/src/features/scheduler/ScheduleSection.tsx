
import { useMemo, useState } from 'react';
import { Badge, Card, DataTable, EmptyState, Stat, Tabs } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'queued', label: 'Trong hàng đợi' },
  { id: 'posted', label: 'Đã đăng' },
  { id: 'failed', label: 'Lỗi' },
];

export function ScheduleSection({ workspace }: { workspace: WorkspaceData }) {
  const [filter, setFilter] = useState('all');
  const jobs = workspace.jobs;

  const counts = useMemo(
    () => ({
      queued: jobs.filter((job) => ['queued', 'running', 'pending_approval'].includes(job.state)).length,
      posted: jobs.filter((job) => job.state === 'posted').length,
      failed: jobs.filter((job) => ['failed', 'needs_review'].includes(job.state)).length,
    }),
    [jobs],
  );

  const visible = jobs.filter((job) => {
    if (filter === 'queued') return ['queued', 'running', 'pending_approval'].includes(job.state);
    if (filter === 'posted') return job.state === 'posted';
    if (filter === 'failed') return ['failed', 'needs_review'].includes(job.state);
    return true;
  });

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Lịch đăng và hàng đợi</h2>
          <p className="hint">Các bài sẽ được đăng theo thứ tự ưu tiên của group, vẫn giữ giới hạn và khoảng nghỉ.</p>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Trong hàng đợi" value={counts.queued} />
        <Stat label="Đã đăng" value={counts.posted} />
        <Stat label="Cần xem lại" value={counts.failed} />
        <Stat label="Tổng bài" value={jobs.length} />
      </div>

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? jobs.length
              : entry.id === 'queued'
                ? counts.queued
                : entry.id === 'posted'
                  ? counts.posted
                  : counts.failed,
        }))}
        active={filter}
        onChange={setFilter}
      />

      {visible.length === 0 ? (
        <EmptyState title="Không có bài nào" description="Các bài đã lên lịch sẽ hiện ở đây." />
      ) : (
        <Card>
          <DataTable head={['Bài đăng', 'Nhóm', 'Tài khoản', 'Trạng thái', 'Lịch đăng']}>
            {visible.map((job) => (
              <tr key={job.id}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '280px' }}>
                    {job.body || 'Không có caption'}
                  </div>
                </td>
                <td className="truncate" style={{ maxWidth: '180px' }}>
                  {job.group_name}
                </td>
                <td>{job.account_name}</td>
                <td>
                  <Badge
                    tone={
                      job.state === 'posted'
                        ? 'ok'
                        : job.state === 'failed'
                          ? 'bad'
                          : job.state === 'needs_review'
                            ? 'warn'
                            : 'info'
                    }
                  >
                    {job.state}
                  </Badge>
                </td>
                <td>{new Date(job.scheduled_at).toLocaleString('vi-VN')}</td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}
    </div>
  );
}
