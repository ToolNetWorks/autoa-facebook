
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, EmptyState, Skeleton } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

interface OptimizeResult {
  id: number;
  status: string;
  risk_level: string;
  meaning_preserved: boolean;
  original_caption: string;
  optimized_caption: string;
  issues: { category: string; severity: string; message: string }[];
  changes: { kind: string; summary: string }[];
  reason: string;
  model_version: string;
  note: string;
}

const STATUS_LABEL: Record<string, string> = {
  APPROVED: 'Phù hợp',
  NEEDS_REVIEW: 'Cần xem lại',
  BLOCKED: 'Không phù hợp',
};

export function AiCaptionSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [caption, setCaption] = useState('');
  const [tone, setTone] = useState('');
  const [target, setTarget] = useState('group');
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!workspace.pipelineId) return;
    api
      .get<{ tone: string }>(`/api/pipelines/${workspace.pipelineId}/caption/settings`)
      .then((settings) => setTone(settings.tone ?? ''))
      .catch(() => undefined);
  }, [workspace.pipelineId]);

  async function optimize() {
    if (caption.trim().length < 3) {
      push('Viết caption gốc trước khi kiểm tra', 'warn');
      return;
    }
    setBusy(true);
    try {
      const response = await api.post<OptimizeResult>(
        `/api/pipelines/${workspace.pipelineId}/caption/optimize`,
        { caption: caption.trim(), target, tone },
      );
      setResult(response);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra được caption', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!result) return;
    setBusy(true);
    try {
      await api.post(`/api/pipelines/${workspace.pipelineId}/caption/approve`, {
        record_id: result.id,
        optimized_caption: result.optimized_caption,
      });
      setCaption(result.optimized_caption);
      push('Đã dùng caption AI', 'success');
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không duyệt được caption', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>AI Caption</h2>
          <p className="hint">
            Kiểm tra caption theo quy định cộng đồng, giảm spam và giữ nguyên thông điệp chính.
          </p>
        </div>
      </div>

      <Card title="1. Caption gốc">
        <div className="stack">
          <Textarea
            label="Caption gốc"
            name="caption_original"
            rows={6}
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
            placeholder="Viết caption bạn muốn đăng..."
          />
          <div className="form-grid">
            <Select
              label="Đăng ở đâu"
              name="caption_target"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
            >
              <option value="group">Bài ảnh trong group</option>
              <option value="reel">Reel trang cá nhân</option>
            </Select>
            <Input
              label="Văn phong mong muốn"
              name="caption_tone"
              value={tone}
              onChange={(event) => setTone(event.target.value)}
              placeholder="VD: thân thiện, ngắn gọn"
            />
          </div>
          <Button variant="primary" loading={busy} onClick={() => void optimize()}>
            Kiểm tra caption
          </Button>
        </div>
      </Card>

      {busy && !result ? <Skeleton variant="block" /> : null}

      {result ? (
        <Card title="2. Caption đề xuất">
          <div className="stack">
            <div className="badge-row">
              <Badge tone={result.status === 'APPROVED' ? 'ok' : result.status === 'BLOCKED' ? 'bad' : 'warn'}>
                {STATUS_LABEL[result.status] ?? result.status}
              </Badge>
              <Badge tone="neutral">Rủi ro: {result.risk_level}</Badge>
              <Badge tone={result.meaning_preserved ? 'ok' : 'warn'}>
                {result.meaning_preserved ? 'Giữ nguyên ý nghĩa' : 'Cần xem lại nghĩa'}
              </Badge>
              {result.model_version ? <Badge tone="neutral">{result.model_version}</Badge> : null}
            </div>

            <div className="form-grid">
              <div className="field">
                <span className="field-label">Bản gốc</span>
                <div className="card tight" style={{ background: 'var(--surface-2)', whiteSpace: 'pre-wrap' }}>
                  {result.original_caption}
                </div>
              </div>
              <div className="field">
                <span className="field-label">Bản AI</span>
                <div className="card tight" style={{ background: 'var(--surface-2)', whiteSpace: 'pre-wrap' }}>
                  {result.optimized_caption}
                </div>
              </div>
            </div>

            {result.changes.length > 0 ? (
              <div className="field">
                <span className="field-label">Thay đổi</span>
                <ul className="stack-sm">
                  {result.changes.map((change, index) => (
                    <li className="hint" key={index}>
                      {change.summary}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {result.issues.length > 0 ? (
              <Alert tone={result.status === 'BLOCKED' ? 'bad' : 'warn'}>
                <ul>
                  {result.issues.map((issue, index) => (
                    <li key={index}>{issue.message}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}

            <p className="hint">{result.reason}</p>
            <div className="btn-row">
              <Button variant="primary" loading={busy} onClick={() => void approve()}>
                Dùng caption này
              </Button>
              <Button onClick={() => void optimize()}>Kiểm tra lại</Button>
              <Button
                onClick={() => {
                  setCaption(result.original_caption);
                  setResult(null);
                }}
              >
                Khôi phục bản gốc
              </Button>
            </div>
          </div>
        </Card>
      ) : !busy ? (
        <EmptyState title="Chưa có kết quả" description="Viết caption gốc rồi bấm Kiểm tra caption." />
      ) : null}

      <Alert tone="info">
        Đánh giá của công cụ không bảo đảm Facebook sẽ duyệt bài. Việc duyệt còn phụ thuộc tài khoản, group và hành vi đăng.
      </Alert>
    </div>
  );
}
