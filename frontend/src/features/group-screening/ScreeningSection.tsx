
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Badge, Card, DataTable, EmptyState, ErrorState, Skeleton, Tabs } from '@/components/ui/Surfaces';
import { Modal } from '@/components/ui/Modal';
import { usePermissions } from '@/hooks/usePermissions';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

type Tone = 'ok' | 'warn' | 'bad' | 'neutral' | 'info';

/** Sáu trạng thái xác minh, đúng như backend phân loại. */
const LABELS: Record<string, string> = {
  not_tested: 'Chưa xác minh',
  test_in_progress: 'Đang xác minh',
  submitted_unverified: 'Đã gửi, chưa xác nhận được',
  pending_group_approval: 'Đang chờ Admin group duyệt',
  verified_posted: 'Đã xác minh đăng thành công',
  rejected: 'Bài bị từ chối',
  restricted_confirmed: 'Facebook hạn chế quyền đăng',
  check_failed: 'Không xác minh được',
};

const TONE: Record<string, Tone> = {
  not_tested: 'neutral',
  test_in_progress: 'info',
  submitted_unverified: 'warn',
  pending_group_approval: 'warn',
  verified_posted: 'ok',
  rejected: 'bad',
  restricted_confirmed: 'bad',
  check_failed: 'bad',
};

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'verified', label: 'Đã xác minh' },
  { id: 'review', label: 'Chờ group duyệt' },
  { id: 'pending', label: 'Chưa xác minh' },
  { id: 'failed', label: 'Xác minh lỗi' },
  { id: 'restricted', label: 'Bị hạn chế' },
];

interface VerificationDraft {
  body: string;
  media: string[];
  media_name: string;
  account_label: string;
  group_label: string;
  posting_mode: string;
  nickname: string;
  group_id: number;
}

interface Verification {
  group_id: number;
  account_id: number;
  group_label: string;
  account_label: string;
  posting_mode: string;
  verification_status: string;
  status_label: string;
  status_tone: Tone;
  reason: string;
  has_evidence: boolean;
  verified_post_url: string;
  verified_posting_mode: string;
  submitted_at: string | null;
  verified_at: string | null;
  last_error_code: string;
}

const POLL_INTERVAL_MS = 5000;

export function ScreeningSection({ workspace }: { workspace: WorkspaceData }) {
  const { can } = usePermissions();
  const { push } = useToast();
  const [rows, setRows] = useState<Verification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const [busy, setBusy] = useState<number | null>(null);
  const [draft, setDraft] = useState<VerificationDraft | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!workspace.pipelineId) {
      setLoading(false);
      return;
    }
    if (!quiet) setLoading(true);
    try {
      const response = await api.get<{ verifications: Verification[] }>(
        `/api/pipelines/${workspace.pipelineId}/groups/verifications`,
      );
      setRows(response.verifications);
      setError(null);
    } catch (failure) {
      if (!quiet) {
        setError(failure instanceof Error ? failure.message : 'Không tải được kết quả xác minh');
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [workspace.pipelineId]);

  useEffect(() => {
    void load();
  }, [load]);

  const running = rows.some(
    (item) => item.verification_status === 'test_in_progress'
      || item.verification_status === 'submitted_unverified',
  );

  // Trong lúc một lần xác minh đang chạy, trang tự cập nhật.
  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => {
      void load(true);
    }, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [running, rows, load]);

  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    rows.forEach((item) => {
      result[item.verification_status] = (result[item.verification_status] ?? 0) + 1;
    });
    return result;
  }, [rows]);

  const visible = useMemo(() => {
    if (filter === 'all') return rows;
    if (filter === 'verified') return rows.filter((item) => item.verification_status === 'verified_posted');
    if (filter === 'review') {
      return rows.filter((item) => item.verification_status === 'pending_group_approval');
    }
    if (filter === 'pending') return rows.filter((item) => item.verification_status === 'not_tested');
    if (filter === 'failed') {
      return rows.filter(
        (item) => item.verification_status === 'check_failed'
          || item.verification_status === 'submitted_unverified',
      );
    }
    return rows.filter((item) => item.verification_status === 'restricted_confirmed');
  }, [rows, filter]);

  /** Buoc 1: chuan bi mot bai viet that de xem truoc. */
  async function prepare(item: Verification) {
    setBusy(item.group_id);
    try {
      const response = await api.post<{ draft: VerificationDraft }>(
        `/api/pipelines/${workspace.pipelineId}/groups/${item.group_id}/verification/prepare`,
      );
      setDraft(response.draft);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không chuẩn bị được bài xác minh', 'error');
    } finally {
      setBusy(null);
    }
  }

  /** Buoc 3: CTV xac nhan gui. */
  async function confirm() {
    if (!draft) return;
    setBusy(draft.group_id);
    try {
      await api.post(
        `/api/pipelines/${workspace.pipelineId}/groups/${draft.group_id}/verification/confirm`,
      );
      push('Đã gửi bài xác minh. Hệ thống sẽ báo lại kết quả thật.', 'info');
      setDraft(null);
      await load(true);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không gửi được bài xác minh', 'error');
    } finally {
      setBusy(null);
    }
  }

  async function recheck(item: Verification) {
    setBusy(item.group_id);
    try {
      await api.post(`/api/pipelines/${workspace.pipelineId}/groups/${item.group_id}/recheck`, { force: true });
      push('Đã xếp hàng kiểm tra lại group', 'success');
      await load(true);
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
          <h2>Group Screening</h2>
          <p className="hint">
            Một group chỉ được coi là xác minh khi đã có bài đăng thật trong group bằng đúng tài khoản.
          </p>
        </div>
      </div>

      {error ? (
        <ErrorState
          title="Không thể tải kết quả xác minh. Vui lòng thử lại."
          action={
            <Button size="sm" onClick={() => void load()}>
              Thử lại
            </Button>
          }
        />
      ) : null}

      {loading && rows.length === 0 ? (
        <Skeleton variant="block" />
      ) : null}

      {!loading && rows.length === 0 ? (
        <EmptyState
          title="Chưa có cặp Facebook–Group nào cần xác minh"
          description="Thêm Facebook và group ở mục Nhóm Facebook trước."
        />
      ) : null}

      {!loading && rows.length > 0 ? (
        <>
          <div className="stat-row">
            <Stat label="Cặp đã xác minh" value={counts.verified_posted ?? 0} />
            <Stat label="Chờ group duyệt" value={counts.pending_group_approval ?? 0} />
            <Stat label="Chưa xác minh" value={counts.not_tested ?? 0} />
            <Stat label="Đang xác minh" value={counts.test_in_progress ?? 0} />
            <Stat label="Xác minh lỗi" value={counts.check_failed ?? 0} />
            <Stat label="Bị từ chối" value={counts.rejected ?? 0} />
            <Stat label="Facebook hạn chế" value={counts.restricted_confirmed ?? 0} />
          </div>

          {running ? (
            <Alert tone="info">Đang xác minh. Trang tự cập nhật khi có kết quả.</Alert>
          ) : null}

          <Tabs
            tabs={FILTERS.map((entry) => ({
              id: entry.id,
              label: entry.label,
              count:
                entry.id === 'all'
                  ? rows.length
                  : entry.id === 'verified'
                    ? (counts.verified_posted ?? 0)
                    : entry.id === 'review'
                      ? (counts.pending_group_approval ?? 0)
                      : entry.id === 'pending'
                        ? (counts.not_tested ?? 0)
                        : entry.id === 'failed'
                          ? (counts.check_failed ?? 0) + (counts.submitted_unverified ?? 0)
                          : (counts.restricted_confirmed ?? 0),
            }))}
            active={filter}
            onChange={setFilter}
          />

          <Card>
            <DataTable head={['Group', 'Facebook', 'Chế độ', 'Xác minh', 'Thời điểm', 'Bằng chứng', 'Thao tác']}>
              {visible.map((item) => (
                <tr key={`${item.group_id}:${item.account_id}`}>
                  <td>
                    <div className="cell-title truncate" style={{ maxWidth: '180px' }}>
                      {item.group_label}
                    </div>
                  </td>
                  <td>
                    <div className="cell-sub truncate" style={{ maxWidth: '130px' }}>
                      {item.account_label}
                    </div>
                  </td>
                  <td>
                    <Badge tone={item.posting_mode === 'nickname' ? 'info' : 'neutral'}>
                      {item.posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={TONE[item.verification_status] ?? 'neutral'}>
                      {LABELS[item.verification_status] ?? item.verification_status}
                    </Badge>
                  </td>
                  <td>
                    <div className="cell-sub">
                      {item.verified_at
                        ? new Date(item.verified_at).toLocaleString('vi-VN')
                        : item.submitted_at
                          ? `đã gửi ${new Date(item.submitted_at).toLocaleString('vi-VN')}`
                          : 'chưa xác minh'}
                    </div>
                  </td>
                  <td>
                    {item.verified_post_url ? (
                      <a href={item.verified_post_url} target="_blank" rel="noreferrer">
                        Xem bài xác minh
                      </a>
                    ) : (
                      <div className="cell-sub">{item.reason}</div>
                    )}
                  </td>
                  <td className="actions">
                    <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                      {can('group_posts.create') && item.verification_status === 'not_tested' ? (
                        <Button size="sm" loading={busy === item.group_id} onClick={() => void prepare(item)}>
                          Xác minh bằng bài đăng
                        </Button>
                      ) : null}
                      {can('groups.manage') ? (
                        <Button size="sm" loading={busy === item.group_id} onClick={() => void recheck(item)}>
                          Kiểm tra lại
                        </Button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </DataTable>
          </Card>
        </>
      ) : null}

      <Modal
        open={draft !== null}
        title="Xem trước bài xác minh"
        description="Bài này sẽ được đăng thật vào group để xác minh quyền đăng."
        onClose={() => setDraft(null)}
        footer={
          <>
            <Button onClick={() => setDraft(null)}>Huỷ</Button>
            <Button variant="primary" loading={busy === draft?.group_id} onClick={() => void confirm()}>
              Xác nhận gửi
            </Button>
          </>
        }
      >
        {draft ? (
          <div className="stack">
            <div className="badge-row">
              <Badge tone="info">{draft.group_label}</Badge>
              <Badge tone="neutral">{draft.account_label}</Badge>
              <Badge tone="neutral">
                {draft.posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
              </Badge>
            </div>
            <p>{draft.body}</p>
            {draft.media.length > 0 ? (
              <div className="cell-sub">Ảnh: {draft.media_name || draft.media[0]}</div>
            ) : (
              <Alert tone="warn">Bài xác minh chưa có ảnh. Thêm ảnh vào Album trước.</Alert>
            )}
            {draft.posting_mode === 'nickname' ? (
              <div className="cell-sub">Biệt danh: {draft.nickname || 'chưa có tên'}</div>
            ) : null}
          </div>
        ) : null}
      </Modal>
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
