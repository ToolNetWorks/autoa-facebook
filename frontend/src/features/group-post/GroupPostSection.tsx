
import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { Alert, Card, DataTable, Skeleton } from '@/components/ui/Surfaces';
import { GroupSelector } from '@/features/group-post/GroupSelector';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

interface SkippedGroup {
  group_id: number;
  label?: string;
  reason: string;
}

interface CampaignResult {
  jobs_created: number;
  jobs: { job_id: number; group_id: number; label: string; posting_mode: string }[];
  skipped: SkippedGroup[];
  deferred: SkippedGroup[];
  duplicates: SkippedGroup[];
  selected: number;
  limit: number;
  published: boolean;
}

export function GroupPostSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [body, setBody] = useState('');
  const [image, setImage] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);
  const [maxGroups, setMaxGroups] = useState(200);
  const [result, setResult] = useState<CampaignResult | null>(null);
  const [scheduledAt, setScheduledAt] = useState(() => {
    const when = new Date(Date.now() + 15 * 60 * 1000);
    const offset = when.getTimezoneOffset();
    return new Date(when.getTime() - offset * 60000).toISOString().slice(0, 16);
  });

  useEffect(() => {
    workspace.reloadGroups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!workspace.pipelineId) {
      setChecking(false);
      return;
    }
    let active = true;
    api
      .get<{ max_groups: number }>(
        `/api/pipelines/${workspace.pipelineId}/group-post/limits`,
      )
      .then((response) => {
        if (active) setMaxGroups(response.max_groups);
      })
      .catch(() => undefined)
      .finally(() => {
        if (active) setChecking(false);
      });
    return () => {
      active = false;
    };
  }, [workspace.pipelineId]);

  const groups = useMemo(
    () => workspace.groups.filter((group) => group.enabled && group.approved_target),
    [workspace.groups],
  );

  async function submit() {
    if (groupIds.length === 0) {
      push('Chọn ít nhất một group', 'warn');
      return;
    }
    if (!image) {
      push('Chọn một ảnh', 'warn');
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      // Kiểm tra danh sách group trước, để không tải ảnh lên khi yêu cầu
      // không hợp lệ.
      const preflight = await api.post<{ ready: unknown[]; skipped: SkippedGroup[] }>(
        `/api/pipelines/${workspace.pipelineId}/group-posts/preflight`,
        {
          group_ids: groupIds,
          body: body.trim() || ' ',
          media: ['uploads/preflight.jpg'],
          scheduled_at: new Date(scheduledAt).toISOString(),
        },
      );
      if (preflight.ready.length === 0) {
        push('Không group nào đủ điều kiện để lên lịch', 'warn');
        setResult({
          jobs_created: 0,
          jobs: [],
          skipped: preflight.skipped,
          deferred: [],
          duplicates: [],
          selected: groupIds.length,
          limit: maxGroups,
          published: false,
        });
        return;
      }
      const form = new FormData();
      form.append('file', image);
      const uploaded = await api.upload<{ path: string }>(
        `/api/uploads?pipeline_id=${workspace.pipelineId}`,
        form,
      );
      const response = await api.post<CampaignResult>(
        `/api/pipelines/${workspace.pipelineId}/group-posts`,
        {
          group_ids: groupIds,
          body: body.trim(),
          media: [uploaded.path],
          scheduled_at: new Date(scheduledAt).toISOString(),
          anonymous_only: 1,
        },
      );
      setResult(response);
      push(
        response.jobs_created > 0
          ? `Đã lên lịch ${response.jobs_created} bài`
          : 'Chưa tạo được bài nào',
        response.jobs_created > 0 ? 'success' : 'warn',
      );
      setBody('');
      setImage(null);
      setGroupIds([]);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lên lịch được', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Tạo bài group</h2>
          <p className="hint">
            Một bài, một ảnh, mỗi group một lịch riêng với chế độ đăng của riêng nó.
          </p>
        </div>
      </div>

      {checking ? (
        <Skeleton variant="block" />
      ) : (
        <GroupSelector
          groups={groups}
          selected={groupIds}
          onChange={setGroupIds}
          maxGroups={maxGroups}
          busy={busy}
        />
      )}

      <Card title="2. Ảnh và caption">
        <div className="stack">
          <Input
            label="Ảnh (1 ảnh)"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(event) => setImage(event.target.files?.[0] ?? null)}
            hint="Ảnh dùng chung cho mọi group trong chiến dịch này."
          />
          <Textarea
            label="Caption"
            name="post_body"
            rows={5}
            value={body}
            onChange={(event) => setBody(event.target.value)}
            placeholder="Nội dung bài đăng"
          />
          <Input
            label="Lịch đăng"
            type="datetime-local"
            name="post_scheduled"
            value={scheduledAt}
            onChange={(event) => setScheduledAt(event.target.value)}
          />
          <Alert tone="info">
            Bài sẽ được đăng theo chế độ ẩn danh hoặc biệt danh đã cấu hình của từng group. Nếu chế độ
            không khả dụng, bài sẽ dừng ở trạng thái cần xem lại.
          </Alert>
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Lên lịch đăng bài
          </Button>
        </div>
      </Card>

      {result ? (
        <Card title="Kết quả chiến dịch">
          <div className="stack-sm">
            <div className="stat-row">
              <div className="stat">
                <span>Đã tạo lịch</span>
                <strong>{result.jobs_created}</strong>
              </div>
              <div className="stat">
                <span>Chờ đến ngày sau</span>
                <strong>{result.deferred.length}</strong>
              </div>
              <div className="stat">
                <span>Cần xử lý</span>
                <strong>{result.skipped.length + result.duplicates.length}</strong>
              </div>
            </div>
            {result.skipped.length > 0 || result.duplicates.length > 0 ? (
              <DataTable head={['Group', 'Lý do']}>
                {[...result.skipped, ...result.duplicates].map((item) => (
                  <tr key={`${item.group_id}-${item.reason}`}>
                    <td>{item.label ?? item.group_id}</td>
                    <td>{item.reason}</td>
                  </tr>
                ))}
              </DataTable>
            ) : null}
            {result.deferred.length > 0 ? (
              <Alert tone="info">
                {result.deferred.length} group đã đủ hạn mức bài hôm nay nên bài sẽ được đăng ở những
                ngày sau.
              </Alert>
            ) : null}
            {result.jobs_created > 0 ? (
              <Alert tone="info">
                Đã lên lịch {result.jobs_created} bài. Bài sẽ được đăng khi tới lịch và còn hạn mức; bài
                chờ duyệt sẽ dừng ở trạng thái cần xem lại.
              </Alert>
            ) : null}
          </div>
        </Card>
      ) : null}
    </div>
  );
}
