import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Badge, Card, DataTable, EmptyState, ErrorState, Skeleton } from '@/components/ui/Surfaces';


type Tone = 'ok' | 'warn' | 'bad' | 'neutral' | 'info';

/** Số dòng hiện trước khi bấm “Xem tất cả”. */
const PREVIEW_ROWS = 8;

const LABELS: Record<string, string> = {
  priority: 'Đủ điều kiện đăng trực tiếp',
  review: 'Cần Admin group duyệt bài',
  unknown: 'Chưa xác minh',
  not_member: 'Chưa tham gia group',
  not_accessible: 'Không xem được nội dung group',
  restricted: 'Facebook hạn chế quyền đăng',
  failed: 'Không kiểm tra được',
};

const TONE: Record<string, Tone> = {
  priority: 'ok',
  review: 'warn',
  not_member: 'warn',
  not_accessible: 'warn',
  restricted: 'bad',
  failed: 'bad',
  unknown: 'neutral',
};

interface ScreeningResult {
  id: number;
  group_id: number;
  account_id: number;
  group_label: string;
  account_label: string;
  moderation_status: string;
  evidence_summary: string;
  next_action: string;
  last_checked_at: string | null;
}

/**
 * Trạng thái của từng group trong cấu hình tự động đăng.
 *
 * Mỗi dòng là một cặp Facebook–Group, nên người dùng thấy đúng group nào
 * sẽ được đăng bằng tài khoản nào. Group chưa đủ điều kiện vẫn nằm trong
 * cấu hình, chỉ là chưa được xếp lịch.
 */
export function GroupStatusList({
  pipelineId,
  accounts,
  groups,
  selectedGroupIds,
}: {
  pipelineId: number;
  accounts: { id: number; label: string }[];
  groups: { id: number; label: string; account_id?: number | null }[];
  selectedGroupIds: number[];
}) {
  const { push } = useToast();
  const [results, setResults] = useState<ScreeningResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await api.get<{ results: ScreeningResult[] }>(
        `/api/pipelines/${pipelineId}/groups/screening-results`,
      );
      setResults(response.results);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Không tải được trạng thái group');
    } finally {
      setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void load();
  }, [load]);

  const byPair = useMemo(() => {
    const map = new Map<string, ScreeningResult>();
    results.forEach((item) => {
      map.set(`${item.account_id}:${item.group_id}`, item);
    });
    return map;
  }, [results]);

  const eligibleByAccount = useMemo(() => {
    const counts: Record<number, number> = {};
    results.forEach((item) => {
      if (item.moderation_status === 'priority' || item.moderation_status === 'review') {
        counts[item.account_id] = (counts[item.account_id] ?? 0) + 1;
      }
    });
    return counts;
  }, [results]);

  const rows = useMemo(() => {
    const selected = selectedGroupIds.length > 0 ? selectedGroupIds : groups.map((group) => group.id);
    const list: {
      group: { id: number; label: string; account_id?: number | null };
      accountId: number;
      accountLabel: string;
      status: string;
      reason: string;
      action: string;
      checked: string | null;
    }[] = [];
    selected.forEach((groupId) => {
      const group = groups.find((item) => item.id === groupId);
      if (!group) return;
      const candidates = accounts.filter((account) => account.id === group.account_id);
      const pool = candidates.length > 0 ? candidates : accounts;
      const usable = pool.find((account) => byPair.has(`${account.id}:${groupId}`));
      const target = usable ?? pool[0] ?? accounts[0];
      if (!target) return;
      const found = byPair.get(`${target.id}:${groupId}`);
      list.push({
        group,
        accountId: target.id,
        accountLabel: target.label,
        status: found?.moderation_status ?? 'unknown',
        reason: found?.evidence_summary ?? 'Chưa kiểm tra quyền đăng của cặp Facebook–Group này.',
        action: found?.next_action ?? 'Bấm Kiểm tra lại để hệ thống xác minh.',
        checked: found?.last_checked_at ?? null,
      });
    });
    return list;
  }, [selectedGroupIds, groups, accounts, byPair]);

  async function recheck(groupId: number) {
    setBusy(groupId);
    try {
      await api.post(`/api/pipelines/${pipelineId}/groups/${groupId}/recheck`, { force: true });
      push('Đã xếp hàng kiểm tra lại group', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra lại được', 'error');
    } finally {
      setBusy(null);
    }
  }

  if (loading && results.length === 0) {
    return <Skeleton variant="block" />;
  }

  if (error) {
    return (
      <ErrorState
        title="Không tải được trạng thái group. Vui lòng thử lại."
        action={
          <Button size="sm" onClick={() => void load()}>
            Thử lại
          </Button>
        }
      />
    );
  }

  return (
    <Card
      title="Trạng thái group"
      description="Chỉ group đủ điều kiện mới được đưa vào lịch tự động. Group còn lại vẫn nằm trong cấu hình của bạn."
    >
      {accounts.length > 0 ? (
        <div className="stat-row" style={{ marginBottom: 'var(--sp-3)' }}>
          {accounts.map((account) => (
            <div className="stat" key={account.id}>
              <span>{account.label}</span>
              <strong>{eligibleByAccount[account.id] ?? 0}</strong>
            </div>
          ))}
        </div>
      ) : null}

      {rows.length === 0 ? (
        <EmptyState title="Chưa chọn group nào" description="Chọn group ở phần cấu hình phía trên." />
      ) : (
        <DataTable head={['Group', 'Facebook', 'Trạng thái', 'Lý do', 'Cách xử lý', 'Thao tác']}>
          {(expanded ? rows : rows.slice(0, PREVIEW_ROWS)).map((row) => (
            <tr key={`${row.group.id}:${row.accountId}`}>
              <td>
                <div className="cell-title truncate" style={{ maxWidth: '180px' }}>
                  {row.group.label}
                </div>
              </td>
              <td>
                <div className="cell-sub truncate" style={{ maxWidth: '120px' }}>
                  {row.accountLabel}
                </div>
              </td>
              <td>
                <Badge tone={TONE[row.status] ?? 'neutral'}>{LABELS[row.status] ?? row.status}</Badge>
              </td>
              <td>
                <div className="cell-sub">{row.reason}</div>
              </td>
              <td>
                <div className="cell-sub">{row.action}</div>
              </td>
              <td className="actions">
                <Button size="sm" loading={busy === row.group.id} onClick={() => void recheck(row.group.id)}>
                  Kiểm tra lại
                </Button>
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      {rows.length > PREVIEW_ROWS ? (
        <div className="btn-row" style={{ marginTop: 'var(--sp-3)' }}>
          <Button size="sm" onClick={() => setExpanded((value) => !value)}>
            {expanded ? 'Thu gọn' : `Xem tất cả ${rows.length} group`}
          </Button>
        </div>
      ) : null}

      {rows.some((row) => row.status === 'failed') ? (
        <Alert tone="warn">
          Có group chưa kiểm tra được. Bấm “Kiểm tra lại” để thử lại, hoặc đăng nhập lại Facebook nếu phiên đã hết hạn.
        </Alert>
      ) : null}
    </Card>
  );
}
