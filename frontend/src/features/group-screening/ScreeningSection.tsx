
import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Badge, Card, DataTable, EmptyState, Tabs } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const LABELS: Record<string, string> = {
  priority: 'Đã xác minh đăng trực tiếp',
  review: 'Cần Admin group duyệt',
  unknown: 'Chưa xác định',
  restricted: 'Nhóm hạn chế',
  failed: 'Không kiểm tra được',
};

const TONE: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
  priority: 'ok',
  review: 'warn',
  restricted: 'bad',
  failed: 'bad',
  unknown: 'neutral',
};

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'priority', label: 'Ưu tiên' },
  { id: 'review', label: 'Chờ duyệt' },
  { id: 'unknown', label: 'Chưa xác định' },
  { id: 'restricted', label: 'Hạn chế' },
];

export function ScreeningSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [filter, setFilter] = useState('all');
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    workspace.reloadGroups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const groups = workspace.groups;
  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    groups.forEach((group) => {
      result[group.screening_status] = (result[group.screening_status] ?? 0) + 1;
    });
    return result;
  }, [groups]);

  const visible = groups.filter((group) => filter === 'all' || group.screening_status === filter);

  async function recheck(groupId: number) {
    setBusy(groupId);
    try {
      await api.post(`/api/pipelines/${workspace.pipelineId}/groups/${groupId}/recheck`, { force: true });
      push('Đã xếp hàng kiểm tra lại group', 'success');
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra được', 'error');
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Group Screening Center</h2>
          <p className="hint">
            Hệ thống tự phân loại group theo bằng chứng quan sát được. Group chưa xác định sẽ chờ kiểm tra, không tự đăng.
          </p>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Tổng group" value={groups.length} />
        <Stat label="Ưu tiên" value={counts.priority ?? 0} />
        <Stat label="Chờ duyệt" value={counts.review ?? 0} />
        <Stat label="Chưa xác định" value={counts.unknown ?? 0} />
        <Stat label="Hạn chế" value={(counts.restricted ?? 0) + (counts.failed ?? 0)} />
      </div>

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? groups.length
              : entry.id === 'restricted'
                ? (counts.restricted ?? 0) + (counts.failed ?? 0)
                : counts[entry.id] ?? 0,
        }))}
        active={filter}
        onChange={setFilter}
      />

      {groups.length === 0 ? (
        <EmptyState title="Chưa có group nào" description="Thêm group ở mục Nhóm Facebook trước." />
      ) : (
        <Card>
          <DataTable head={['Group', 'Chế độ', 'Phân loại', 'Bằng chứng', 'Thao tác']}>
            {visible.map((group) => (
              <tr key={group.id}>
                <td>
                  <div className="cell-title">{group.label}</div>
                  <div className="cell-sub truncate" style={{ maxWidth: '220px' }}>
                    {group.url}
                  </div>
                </td>
                <td>
                  <Badge tone={group.posting_mode === 'nickname' ? 'info' : 'neutral'}>
                    {group.posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
                  </Badge>
                </td>
                <td>
                  <Badge tone={TONE[group.screening_status] ?? 'neutral'}>
                    {LABELS[group.screening_status] ?? group.screening_status}
                  </Badge>
                </td>
                <td>
                  <div className="cell-sub">
                    {group.last_checked_at
                      ? new Date(group.last_checked_at).toLocaleString('vi-VN')
                      : 'chưa kiểm tra'}
                  </div>
                  <div className="cell-sub">
                    nguồn: {group.screening_source === 'manual' ? 'xác nhận thủ công' : group.screening_source}
                  </div>
                </td>
                <td className="actions">
                  <Button size="sm" loading={busy === group.id} onClick={() => void recheck(group.id)}>
                    Kiểm tra lại
                  </Button>
                </td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Alert tone="info">
        Một lần đăng thành công không có nghĩa các bài sau chắc chắn không cần duyệt. Ưu tiên chỉ quyết định thứ tự giữa các bài đủ điều kiện.
      </Alert>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
