
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, DataTable, EmptyState } from '@/components/ui/Surfaces';
import type { ManualTask } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function ReelsSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [video, setVideo] = useState<File | null>(null);
  const [preview, setPreview] = useState<string>('');
  const [caption, setCaption] = useState('');
  const [accountId, setAccountId] = useState<number | null>(null);
  const [scheduledAt, setScheduledAt] = useState(() => {
    const when = new Date(Date.now() + 15 * 60 * 1000);
    const offset = when.getTimezoneOffset();
    return new Date(when.getTime() - offset * 60000).toISOString().slice(0, 16);
  });
  const [autoPublish, setAutoPublish] = useState(true);
  const [busy, setBusy] = useState(false);
  const [tasks, setTasks] = useState<ManualTask[]>([]);

  async function loadTasks() {
    if (!workspace.pipelineId) return;
    try {
      const result = await api.get<ManualTask[]>(`/api/manual-tasks?pipeline_id=${workspace.pipelineId}`);
      setTasks(result.filter((task) => task.task_type === 'reel'));
    } catch {
      setTasks([]);
    }
  }

  useEffect(() => {
    void loadTasks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  function pickVideo(file: File | null) {
    setVideo(file);
    setPreview(file ? URL.createObjectURL(file) : '');
  }

  async function submit() {
    if (!video) {
      push('Chọn video trước', 'warn');
      return;
    }
    if (!accountId) {
      push('Chọn tài khoản Facebook', 'warn');
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append('file', video);
      const uploaded = await api.upload<{ path: string }>(
        `/api/video-uploads?pipeline_id=${workspace.pipelineId}`,
        form,
      );
      await api.post('/api/manual-tasks', {
        pipeline_id: workspace.pipelineId,
        account_id: accountId,
        task_type: 'reel',
        body: caption,
        media: [uploaded.path],
        target_url: '',
        auto_publish: autoPublish,
        scheduled_at: new Date(scheduledAt).toISOString(),
      });
      push('Đã lên lịch Reel, hãy kiểm tra video trước khi đăng', 'success');
      setVideo(null);
      setPreview('');
      setCaption('');
      await loadTasks();
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
          <h2>Trang cá nhân · Reels</h2>
          <p className="hint">
            Reel được đăng bằng trình duyệt của tài khoản, không dùng API chính thức. Hãy kiểm tra video trước khi đăng.
          </p>
        </div>
      </div>

      <Card title="1. Video và caption">
        <div className="stack">
          <Input
            label="Video MP4 hoặc MOV"
            type="file"
            accept="video/mp4,video/quicktime"
            onChange={(event) => pickVideo(event.target.files?.[0] ?? null)}
            hint="Tối đa 200 MB."
          />
          {preview ? (
            <video src={preview} controls style={{ width: '100%', maxWidth: '320px', borderRadius: 'var(--r-md)' }} />
          ) : null}
          <Textarea
            label="Caption hoặc mô tả Reel"
            name="reel_caption"
            rows={4}
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
          />
        </div>
      </Card>

      <Card title="2. Tài khoản và lịch đăng">
        <div className="form-grid">
          <Select
            label="Tài khoản Facebook"
            name="reel_account"
            value={accountId ?? ''}
            onChange={(event) => setAccountId(Number(event.target.value) || null)}
          >
            <option value="">Chọn tài khoản...</option>
            {workspace.accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.label}
              </option>
            ))}
          </Select>
          <Input
            label="Lịch đăng"
            type="datetime-local"
            name="reel_scheduled"
            value={scheduledAt}
            onChange={(event) => setScheduledAt(event.target.value)}
          />
        </div>
        <label className="check" style={{ marginTop: 'var(--sp-3)' }}>
          <input type="checkbox" checked={autoPublish} onChange={(event) => setAutoPublish(event.target.checked)} />
          <span className="check-text">
            <b>Tự động thử đăng Reel theo lịch</b>
            <small>Nếu kết quả không rõ, hệ thống không tự thử lại.</small>
          </span>
        </label>
        <div style={{ marginTop: 'var(--sp-4)' }}>
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Lên lịch Reel
          </Button>
        </div>
      </Card>

      <Card title="Lịch sử Reel">
        {tasks.length === 0 ? (
          <EmptyState title="Chưa có Reel nào" description="Lên lịch Reel đầu tiên ở trên." />
        ) : (
          <DataTable head={['Nội dung', 'Tài khoản', 'Trạng thái', 'Lịch đăng']}>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '320px' }}>
                    {task.body || 'Không có caption'}
                  </div>
                </td>
                <td>{task.account_label}</td>
                <td>
                  <Badge tone={task.status === 'posted' ? 'ok' : task.status === 'failed' ? 'bad' : 'warn'}>
                    {task.status}
                  </Badge>
                  {task.details ? <div className="cell-sub">{task.details}</div> : null}
                </td>
                <td>{new Date(task.scheduled_at).toLocaleString('vi-VN')}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Alert tone="warn">
        Facebook có thể đổi giao diện và làm bài đăng thất bại. Hãy kiểm tra trực tiếp trên tài khoản sau khi đăng.
      </Alert>
    </div>
  );
}
