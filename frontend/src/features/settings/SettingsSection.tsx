
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { Alert, Card } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

export function SettingsSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const pipeline = workspace.pipeline;
    if (!pipeline) return;
    setName(pipeline.name);
    setDescription(pipeline.description);
    setEnabled(Boolean(pipeline.enabled));
  }, [workspace.pipeline]);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/api/pipelines/${workspace.pipelineId}`, { enabled });
      push('Đã lưu cài đặt Workspace', 'success');
      workspace.reloadAll();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <Card title="Thông tin Workspace" description="Tên và mô tả giúp nhận biết Workspace khi có nhiều pipeline.">
        <div className="stack">
          <Input label="Tên pipeline" name="settings_name" value={name} onChange={(event) => setName(event.target.value)} disabled />
          <Textarea
            label="Mô tả"
            name="settings_description"
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <label className="check">
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
            <span className="check-text">
              <b>Workspace đang hoạt động</b>
              <small>Tắt sẽ dừng mọi job đăng của Workspace này.</small>
            </span>
          </label>
          <div>
            <Button variant="primary" loading={busy} onClick={() => void save()}>
              Lưu cài đặt
            </Button>
          </div>
        </div>
      </Card>

      <Alert tone="info">
        Tên pipeline không thể đổi sau khi tạo. Muốn đổi hãy tạo pipeline mới và chuyển dữ liệu.
      </Alert>
    </div>
  );
}
