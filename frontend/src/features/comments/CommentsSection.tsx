
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, DataTable, EmptyState } from '@/components/ui/Surfaces';
import { taskStatusLabel, taskStatusTone } from '@/lib/statusLabels';
import type { ManualTask } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function CommentsSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [tasks, setTasks] = useState<ManualTask[]>([]);
  const [body, setBody] = useState('');
  const [targetUrl, setTargetUrl] = useState('');
  const [accountId, setAccountId] = useState<number | null>(null);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    if (!workspace.pipelineId) return;
    try {
      const result = await api.get<ManualTask[]>(`/api/manual-tasks?pipeline_id=${workspace.pipelineId}`);
      setTasks(result.filter((task) => task.task_type === 'comment'));
    } catch {
      setTasks([]);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  async function submit() {
    if (!body.trim() || !targetUrl.trim() || !accountId) {
      push('Cần có nội dung, link bài viết và tài khoản', 'warn');
      return;
    }
    setBusy(true);
    try {
      await api.post('/api/manual-tasks', {
        pipeline_id: workspace.pipelineId,
        account_id: accountId,
        group_id: groupId,
        task_type: 'comment',
        body: body.trim(),
        media: [],
        target_url: targetUrl.trim(),
        auto_publish: false,
        scheduled_at: new Date().toISOString(),
      });
      push('Đã tạo nhiệm vụ bình luận, hãy kiểm tra trước khi bình luận', 'success');
      setBody('');
      setTargetUrl('');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tạo được nhiệm vụ', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function complete(task: ManualTask) {
    try {
      await api.patch(`/api/manual-tasks/${task.id}`, { status: 'completed' });
      push('Đã đánh dấu hoàn tất', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được', 'error');
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Bình luận</h2>
          <p className="hint">Nhiệm vụ bình luận luôn thủ công, hệ thống không tự đăng hàng loạt.</p>
        </div>
      </div>

      <Card title="Tạo nhiệm vụ bình luận">
        <div className="stack">
          <div className="form-grid">
            <Select
              label="Tài khoản Facebook"
              name="comment_account"
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
            <Select
              label="Group (không bắt buộc)"
              name="comment_group"
              value={groupId ?? ''}
              onChange={(event) => setGroupId(Number(event.target.value) || null)}
            >
              <option value="">Không chọn group</option>
              {workspace.groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.label}
                </option>
              ))}
            </Select>
          </div>
          <Input
            label="Link bài viết cần bình luận"
            name="comment_target"
            value={targetUrl}
            onChange={(event) => setTargetUrl(event.target.value)}
            placeholder="https://www.facebook.com/groups/.../posts/..."
          />
          <Textarea
            label="Nội dung bình luận"
            name="comment_body"
            rows={3}
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Tạo nhiệm vụ
          </Button>
        </div>
      </Card>

      <Card title="Nhiệm vụ bình luận">
        {tasks.length === 0 ? (
          <EmptyState title="Chưa có nhiệm vụ bình luận" />
        ) : (
          <DataTable head={['Nội dung', 'Tài khoản', 'Trạng thái', '']}>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '320px' }}>
                    {task.body}
                  </div>
                  <div className="cell-sub truncate" style={{ maxWidth: '320px' }}>
                    {task.target_url}
                  </div>
                </td>
                <td>{task.account_label}</td>
                <td>
                  <Badge tone={taskStatusTone(task.status)}>{taskStatusLabel(task.status)}</Badge>
                </td>
                <td className="actions">
                  {task.status !== 'completed' ? (
                    <Button size="sm" onClick={() => void complete(task)}>
                      Đã bình luận
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Alert tone="warn">
        Hãy bình luận thủ công và kiểm tra nội dung trước khi gửi. Không dùng công cụ này để gửi hàng loạt.
      </Alert>
    </div>
  );
}
