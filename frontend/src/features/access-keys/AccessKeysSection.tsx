
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, Card, DataTable, EmptyState, Skeleton } from '@/components/ui/Surfaces';
import type { AccessKey } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function AccessKeysSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [keys, setKeys] = useState<AccessKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);

  async function load() {
    if (!workspace.pipelineId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const result = await api.get<AccessKey[]>(`/api/admin/pipelines/${workspace.pipelineId}/key`);
      setKeys(result);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được key', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  async function issue() {
    setBusy(true);
    try {
      const result = await api.post<{ key: string }>(`/api/admin/pipelines/${workspace.pipelineId}/key`);
      setIssued(result.key);
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tạo được key', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!window.confirm('Thu hồi toàn bộ key của Workspace này? Các phiên CTV đang dùng key sẽ mất hiệu lực.')) return;
    setBusy(true);
    try {
      await api.delete(`/api/admin/pipelines/${workspace.pipelineId}/key`);
      push('Đã thu hồi key', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thu hồi được', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Pipeline Access Key</h2>
          <p className="hint">
            CTV đăng nhập bằng tên, mật khẩu và key của Workspace. Key là yếu tố xác nhận thứ hai, không thay thế mật khẩu.
          </p>
        </div>
        <div className="btn-row">
          <Button size="sm" onClick={() => void revoke()} disabled={busy}>
            Thu hồi key
          </Button>
          <Button size="sm" variant="primary" onClick={() => void issue()} loading={busy}>
            Tạo key mới
          </Button>
        </div>
      </div>

      {loading ? (
        <Skeleton variant="block" />
      ) : keys.length === 0 ? (
        <EmptyState
          title="Chưa có key nào"
          description="Tạo key để cấp cho CTV của Workspace này."
        />
      ) : (
        <Card>
          <DataTable head={['Tiền tố', 'Đã tạo', 'Hết hạn', 'Trạng thái']}>
            {keys.map((key) => (
              <tr key={key.id}>
                <td className="mono">{key.key_prefix}…</td>
                <td>{new Date(key.created_at).toLocaleString('vi-VN')}</td>
                <td>{key.expires_at ? new Date(key.expires_at).toLocaleDateString('vi-VN') : '—'}</td>
                <td>
                  <Badge tone={key.revoked_at ? 'bad' : 'ok'}>
                    {key.revoked_at ? 'Đã thu hồi' : 'Đang dùng'}
                  </Badge>
                </td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Modal
        open={issued !== null}
        title="Pipeline Access Key mới"
        description="Key chỉ hiện đúng một lần. Hãy lưu lại và gửi riêng cho CTV."
        onClose={() => setIssued(null)}
        footer={
          <Button variant="primary" onClick={() => setIssued(null)}>
            Tôi đã lưu
          </Button>
        }
      >
        <div className="stack">
          <code className="mono" style={{ wordBreak: 'break-all' }}>
            {issued}
          </code>
          <Alert tone="warn">
            Ai có key này vẫn cần đúng tài khoản CTV và mật khẩu mới đăng nhập được.
          </Alert>
        </div>
      </Modal>
    </div>
  );
}
