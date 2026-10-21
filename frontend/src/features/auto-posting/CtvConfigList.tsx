import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Badge, Card, DataTable, EmptyState, ErrorState, Skeleton } from '@/components/ui/Surfaces';
import type { CtvAutoPostSettings } from '@/types/api';

type ConfigRow = CtvAutoPostSettings & { configured?: boolean };

/** The administrator's view of the configuration of every collaborator. */
export function CtvConfigList({ pipelineId }: { pipelineId: number }) {
  const { push } = useToast();
  const [rows, setRows] = useState<ConfigRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const response = await api.get<{ configs: ConfigRow[] }>(
        `/api/pipelines/${pipelineId}/auto-post/ctv-configs`,
      );
      setRows(response.configs);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Không tải được danh sách cấu hình');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  async function toggle(row: ConfigRow, enabled: boolean) {
    if (!row.ctv_user_id) return;
    setBusy(row.ctv_user_id);
    try {
      const path = `/api/pipelines/${pipelineId}/auto-post/ctv-configs/${row.ctv_user_id}/${
        enabled ? 'activate' : 'pause'
      }`;
      await api.post(path);
      push(enabled ? `Đã bật tự động đăng cho ${row.username}` : `Đã tạm dừng ${row.username}`, 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không đổi được trạng thái', 'error');
    } finally {
      setBusy(null);
    }
  }

  if (loading && rows.length === 0) {
    return <Skeleton variant="block" />;
  }

  if (error) {
    return (
      <ErrorState
        title="Không tải được cấu hình của CTV"
        description={error}
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
      title="Cấu hình của từng CTV"
      description="Mỗi CTV vận hành lịch đăng riêng. Quản trị có thể tạm dừng từng người."
    >
      {rows.length === 0 ? (
        <EmptyState title="Chưa có CTV" description="Chưa có tài khoản CTV nào trong Workspace này." />
      ) : (
        <DataTable head={['CTV', 'Trạng thái', 'Chủ đề', 'Bài/Facebook/ngày', 'Khung giờ', 'Thao tác']}>
          {rows.map((row) => (
            <tr key={row.ctv_user_id ?? row.username}>
              <td>{row.username}</td>
              <td>
                {row.configured === false ? (
                  <Badge tone="neutral">Chưa cấu hình</Badge>
                ) : row.enabled ? (
                  <Badge tone="ok">Đang bật</Badge>
                ) : (
                  <Badge tone="warn">Tạm dừng</Badge>
                )}
              </td>
              <td>{row.topic || '—'}</td>
              <td>{row.max_posts_per_account_daily}</td>
              <td>
                {row.start_time} – {row.end_time}
              </td>
              <td>
                {row.configured === false ? (
                  <span className="hint">Chờ CTV tự thiết lập</span>
                ) : (
                  <Button
                    size="sm"
                    loading={busy === row.ctv_user_id}
                    onClick={() => void toggle(row, !row.enabled)}
                  >
                    {row.enabled ? 'Tạm dừng' : 'Bật lại'}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </Card>
  );
}
