
import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Badge, Card, DataTable, EmptyState, Stat, Tabs } from '@/components/ui/Surfaces';
import { jobStateLabel, jobStateTone } from '@/lib/statusLabels';
import type { JobRow, Precheck } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'review', label: 'Cần xem lại' },
  { id: 'failed', label: 'Thất bại' },
  { id: 'pending', label: 'Đang chờ duyệt' },
];

export function SafetySection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [filter, setFilter] = useState('all');
  const [prechecks, setPrechecks] = useState<Precheck[]>([]);
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    if (!workspace.pipelineId) return;
    api
      .get<{ prechecks: Precheck[] }>(`/api/pipelines/${workspace.pipelineId}/reels/precheck/history`)
      .then((result) => setPrechecks(result.prechecks))
      .catch(() => setPrechecks([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  const jobs = workspace.jobs;
  const counts = useMemo(
    () => ({
      review: jobs.filter((job) => job.state === 'needs_review').length,
      failed: jobs.filter((job) => job.state === 'failed').length,
      pending: jobs.filter((job) => job.state === 'pending_approval').length,
      submitted: jobs.filter((job) => job.state === 'submitted_unverified').length,
    }),
    [jobs],
  );

  const visible = jobs.filter((job) => {
    if (filter === 'review') return job.state === 'needs_review';
    if (filter === 'failed') return job.state === 'failed';
    if (filter === 'pending') return ['pending_approval', 'submitted_unverified'].includes(job.state);
    return ['needs_review', 'failed', 'pending_approval', 'submitted_unverified'].includes(job.state);
  });

  async function review(job: JobRow, state: string) {
    setBusy(job.id);
    try {
      await api.post(`/api/jobs/${job.id}/review`, { state });
      push('Đã cập nhật trạng thái bài', 'success');
      workspace.reloadAll();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được', 'error');
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Safety Center</h2>
          <p className="hint">
            Các bài cần người xem lại trước khi đăng lại, và các video chưa đạt kiểm tra.
          </p>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Cần xem lại" value={counts.review} />
        <Stat label="Thất bại" value={counts.failed} />
        <Stat label="Chờ Admin duyệt" value={counts.pending} />
        <Stat label="Chưa xác minh kết quả" value={counts.submitted} />
      </div>

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? visible.length
              : entry.id === 'review'
                ? counts.review
                : entry.id === 'failed'
                  ? counts.failed
                  : counts.pending + counts.submitted,
        }))}
        active={filter}
        onChange={setFilter}
      />

      {visible.length === 0 ? (
        <EmptyState title="Không có bài nào cần xem lại" description="Mọi bài đang bình thường." />
      ) : (
        <Card>
          <DataTable head={['Bài đăng', 'Nhóm', 'Chế độ', 'Trạng thái', 'Thao tác']}>
            {visible.map((job) => (
              <tr key={job.id}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '280px' }}>
                    {job.body || 'Không có caption'}
                  </div>
                  <div className="cell-sub">{job.account_name}</div>
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
                  <Badge tone={jobStateTone(job.state)}>{jobStateLabel(job.state)}</Badge>
                  {job.details ? <div className="cell-sub">{job.details}</div> : null}
                </td>
                <td className="actions">
                  <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                    <Button
                      size="sm"
                      loading={busy === job.id}
                      onClick={() => void review(job, 'posted')}
                    >
                      Đã đăng
                    </Button>
                    <Button size="sm" onClick={() => void review(job, 'cancelled')}>
                      Bỏ qua
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

          {prechecks.filter((item) => item.status !== 'PASS').length > 0 ? (
        <Card title="Video chưa đạt kiểm tra">
          <ul className="stack-sm">
            {prechecks
              .filter((item) => item.status !== 'PASS')
              .map((item) => (
                <li className="row-between" key={item.id}>
                  <span className="truncate">Video #{item.id}</span>
                  <Badge tone={item.status === 'BLOCKED' ? 'bad' : 'warn'}>{item.status === 'BLOCKED' ? 'Không đạt' : item.status === 'CHECK_FAILED' ? 'Lỗi' : 'Cần xem lại'}</Badge>
                </li>
              ))}
          </ul>
        </Card>
      ) : null}

      <Alert tone="info">
        Một bài đang chờ duyệt không phải là bài đã đăng. Hãy kiểm tra trực tiếp trên group trước khi đánh dấu.
      </Alert>
    </div>
  );
}
