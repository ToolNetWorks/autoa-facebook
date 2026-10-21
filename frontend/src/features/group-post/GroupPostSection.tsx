
import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, EmptyState } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function GroupPostSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [body, setBody] = useState('');
  const [image, setImage] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [scheduledAt, setScheduledAt] = useState(() => {
    const when = new Date(Date.now() + 15 * 60 * 1000);
    const offset = when.getTimezoneOffset();
    return new Date(when.getTime() - offset * 60000).toISOString().slice(0, 16);
  });

  useEffect(() => {
    workspace.reloadGroups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const groups = useMemo(
    () =>
      workspace.groups.filter(
        (group) => group.enabled && group.approved_target && group.screening_status !== 'restricted',
      ),
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
    try {
      const form = new FormData();
      form.append('file', image);
      const uploaded = await api.upload<{ path: string }>(
        `/api/uploads?pipeline_id=${workspace.pipelineId}`,
        form,
      );
      await api.post(`/api/pipelines/${workspace.pipelineId}/group-posts`, {
        group_ids: groupIds,
        body: body.trim(),
        media: [uploaded.path],
        scheduled_at: new Date(scheduledAt).toISOString(),
        anonymous_only: 1,
      });
      push('Đã lên lịch bài cho group đã chọn', 'success');
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
            Mỗi bài dùng đúng một ảnh và một caption. Mỗi group giữ chế độ đăng của riêng nó.
          </p>
        </div>
      </div>

      {groups.length === 0 ? (
        <EmptyState
          title="Chưa có group sẵn sàng"
          description="Thêm group và kiểm tra quyền đăng ở mục Nhóm Facebook."
        />
      ) : (
        <Card title="1. Chọn group">
          <div className="choice-list">
            {groups.map((group) => (
              <label className="check" key={group.id}>
                <input
                  type="checkbox"
                  checked={groupIds.includes(group.id)}
                  onChange={(event) =>
                    setGroupIds((current) =>
                      event.target.checked
                        ? [...current, group.id]
                        : current.filter((id) => id !== group.id),
                    )
                  }
                />
                <span className="check-text">
                  <b>{group.label}</b>
                  <small>
                    {group.account_label ?? 'Chưa gán tài khoản'} ·{' '}
                    {group.posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
                  </small>
                </span>
              </label>
            ))}
          </div>
        </Card>
      )}

      <Card title="2. Ảnh và caption">
        <div className="stack">
          <Input
            label="Ảnh (1 ảnh)"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(event) => setImage(event.target.files?.[0] ?? null)}
            hint="Bài đăng ảnh dùng đúng một ảnh cho mỗi group."
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
          {groupIds.length > 0 ? (
            <div className="badge-row">
              {groupIds.map((id) => {
                const group = groups.find((row) => row.id === id);
                return (
                  <Badge key={id} tone={group?.posting_mode === 'nickname' ? 'info' : 'neutral'}>
                    {group?.label ?? id}
                  </Badge>
                );
              })}
            </div>
          ) : null}
          <Alert tone="info">
            Bài sẽ được đăng theo chế độ ẩn danh hoặc biệt danh đã cấu hình của từng group. Nếu chế độ không khả dụng, bài sẽ dừng ở trạng thái cần xem lại.
          </Alert>
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Lên lịch đăng bài
          </Button>
        </div>
      </Card>
    </div>
  );
}
