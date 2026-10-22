import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Badge, Card, DataTable, EmptyState, ErrorState, Skeleton } from '@/components/ui/Surfaces';

type Tone = 'ok' | 'warn' | 'bad' | 'neutral' | 'info';

/** Số dòng hiện trước khi bấm “Xem tất cả”. */
const PREVIEW_ROWS = 8;

/**
 * Trạng thái xác minh thực tế của một cặp Facebook–Group.
 *
 * Đây là trạng thái của bằng chứng đăng bài, không phải kết quả một lần
 * kiểm tra bằng trình duyệt. Group chưa từng đăng thì vẫn đưa được vào luồng
 * đăng tự động, vì bài đầu tiên chính là lần xác minh.
 */
const VERIFICATION_LABEL: Record<string, string> = {
  not_tested: 'Chưa từng đăng — sẽ đăng bài đầu tiên',
  test_in_progress: 'Đang xác minh',
  submitted_unverified: 'Đã gửi, chưa xác nhận được',
  pending_group_approval: 'Đang chờ Admin group duyệt',
  verified_posted: 'Đã xác minh đăng thành công',
  rejected: 'Bài bị từ chối',
  restricted_confirmed: 'Facebook hạn chế quyền đăng',
  check_failed: 'Không kiểm tra được',
};

const VERIFICATION_TONE: Record<string, Tone> = {
  not_tested: 'info',
  test_in_progress: 'info',
  submitted_unverified: 'warn',
  pending_group_approval: 'warn',
  verified_posted: 'ok',
  rejected: 'bad',
  restricted_confirmed: 'bad',
  check_failed: 'bad',
};

/** Trạng thái nghiệp vụ, cách nói chung cho người dùng. */
const DECISION_LABEL: Record<string, string> = {
  first_post_ready: 'Sẵn sàng đăng bài đầu tiên',
  verified_ready: 'Đã xác minh, có thể đăng tiếp',
  waiting_membership: 'Đang chờ tham gia group',
  waiting_approval: 'Đang chờ duyệt',
  needs_action: 'Cần xử lý',
  confirmed_restricted: 'Bị hạn chế quyền đăng',
  safety_blocked: 'Bị chặn vì nội dung',
};

const DECISION_TONE: Record<string, Tone> = {
  first_post_ready: 'info',
  verified_ready: 'ok',
  waiting_membership: 'warn',
  waiting_approval: 'warn',
  needs_action: 'warn',
  confirmed_restricted: 'bad',
  safety_blocked: 'bad',
};

interface Verification {
  group_id: number;
  account_id: number;
  group_label: string;
  account_label: string;
  posting_mode: string;
  nickname: string;
  verification_status: string;
  status_label: string;
  status_tone: Tone;
  reason: string;
  next_action: string;
  has_evidence: boolean;
  auto_postable: boolean;
  verified_post_url: string;
  verified_at: string | null;
  submitted_at: string | null;
  decision: string;
  membership?: { membership_status: string; status_label: string };
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
  const [results, setResults] = useState<Verification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await api.get<{ verifications: Verification[] }>(
        `/api/pipelines/${pipelineId}/groups/verifications`,
      );
      setResults(response.verifications);
      setError(null);
    } catch (failure) {
      if (!quiet) {
        setError(failure instanceof Error ? failure.message : 'Không tải được trạng thái group');
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void load();
  }, [load]);

  const byPair = useMemo(() => {
    const map = new Map<string, Verification>();
    results.forEach((item) => {
      map.set(`${item.account_id}:${item.group_id}`, item);
    });
    return map;
  }, [results]);

  /** Số group theo từng trạng thái nghiệp vụ, để người dùng thấy công việc thực tế. */
  const counts = useMemo(() => {
    const total: Record<string, number> = {};
    results.forEach((item) => {
      const key = item.decision || 'needs_action';
      total[key] = (total[key] ?? 0) + 1;
    });
    return total;
  }, [results]);

  const rows = useMemo(() => {
    const selected = selectedGroupIds.length > 0 ? selectedGroupIds : groups.map((group) => group.id);
    const list: {
      group: { id: number; label: string; account_id?: number | null };
      accountId: number;
      accountLabel: string;
      verification: Verification | undefined;
    }[] = [];
    selected.forEach((groupId) => {
      const group = groups.find((item) => item.id === groupId);
      if (!group) return;
      const candidates = accounts.filter((account) => account.id === group.account_id);
      const pool = candidates.length > 0 ? candidates : accounts;
      const usable = pool.find((account) => byPair.has(`${account.id}:${groupId}`));
      const target = usable ?? pool[0] ?? accounts[0];
      if (!target) return;
      list.push({
        group,
        accountId: target.id,
        accountLabel: target.label,
        verification: byPair.get(`${target.id}:${groupId}`),
      });
    });
    return list;
  }, [selectedGroupIds, groups, accounts, byPair]);

  async function recheck(groupId: number) {
    setBusy(groupId);
    try {
      await api.post(`/api/pipelines/${pipelineId}/groups/${groupId}/recheck`, { force: true });
      push('Đã xếp hàng kiểm tra lại group', 'success');
      await load(true);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra được', 'error');
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
      description="Group chưa từng đăng vẫn được đưa vào lịch: bài đầu tiên chính là lần xác minh. Chỉ group đã xác minh mới được đăng tiếp."
    >
      <div className="stat-row" style={{ marginBottom: 'var(--sp-3)' }}>
        <div className="stat">
          <span>Group đã chọn</span>
          <strong>{rows.length}</strong>
        </div>
        <div className="stat">
          <span>Sẵn sàng đăng bài đầu tiên</span>
          <strong>{counts.first_post_ready ?? 0}</strong>
        </div>
        <div className="stat">
          <span>Đã xác minh đăng</span>
          <strong>{counts.verified_ready ?? 0}</strong>
        </div>
        <div className="stat">
          <span>Chờ group duyệt</span>
          <strong>{counts.waiting_approval ?? 0}</strong>
        </div>
        <div className="stat">
          <span>Cần xử lý</span>
          <strong>{(counts.needs_action ?? 0) + (counts.waiting_membership ?? 0)}</strong>
        </div>
      </div>

      {rows.length === 0 ? (
        <EmptyState title="Chưa chọn group nào" description="Chọn group ở phần cấu hình phía trên." />
      ) : (
        <DataTable head={['Group', 'Facebook', 'Trạng thái', 'Lý do', 'Cách xử lý', 'Thao tác']}>
          {(expanded ? rows : rows.slice(0, PREVIEW_ROWS)).map((row) => {
            const item = row.verification;
            const decision = item?.decision ?? 'needs_action';
            return (
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
                  <Badge tone={VERIFICATION_TONE[item?.verification_status ?? 'not_tested'] ?? 'neutral'}>
                    {VERIFICATION_LABEL[item?.verification_status ?? 'not_tested'] ?? 'Chưa xác minh'}
                  </Badge>
                  <Badge tone={DECISION_TONE[decision] ?? 'neutral'}>
                    {DECISION_LABEL[decision] ?? decision}
                  </Badge>
                </td>
                <td>
                  <div className="cell-sub">
                    {item?.reason ?? 'Chưa có bằng chứng đăng bài cho cặp này.'}
                  </div>
                </td>
                <td>
                  <div className="cell-sub">
                    {item?.next_action ?? 'Bấm Xếp lịch ngay để chuẩn bị bài đầu tiên.'}
                  </div>
                </td>
                <td className="actions">
                  {item?.verified_post_url ? (
                    <a href={item.verified_post_url} target="_blank" rel="noreferrer">
                      Xem bài
                    </a>
                  ) : null}
                  <Button
                    size="sm"
                    loading={busy === row.group.id}
                    onClick={() => void recheck(row.group.id)}
                  >
                    Kiểm tra lại
                  </Button>
                </td>
              </tr>
            );
          })}
        </DataTable>
      )}

      {rows.length > PREVIEW_ROWS ? (
        <div className="btn-row" style={{ marginTop: 'var(--sp-3)' }}>
          <Button size="sm" onClick={() => setExpanded((value) => !value)}>
            {expanded ? 'Thu gọn' : `Xem tất cả ${rows.length} group`}
          </Button>
        </div>
      ) : null}

      {Object.keys(counts).some((key) => key === 'check_failed' || key === 'needs_action') ? (
        <Alert tone="warn">
          Có group chưa xác minh được. Bấm “Kiểm tra lại” để thử lại, hoặc đăng nhập lại Facebook nếu phiên đã hết hạn.
        </Alert>
      ) : null}
    </Card>
  );
}
