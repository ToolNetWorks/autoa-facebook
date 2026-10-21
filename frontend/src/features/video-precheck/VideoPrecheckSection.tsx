
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, DataTable, EmptyState, Progress } from '@/components/ui/Surfaces';
import type { Precheck, PrecheckCategory, PrecheckIssue } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const CATEGORY_LABEL: Record<string, string> = {
  technical: 'Chất lượng kỹ thuật',
  visual: 'Nội dung hình ảnh',
  ocr: 'Văn bản trong video',
  audio: 'Âm thanh và lời thoại',
  caption: 'Caption và hashtag',
  copyright: 'Bản quyền và trùng lặp',
};

const CATEGORY_TONE: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
  pass: 'ok',
  needs_review: 'warn',
  blocked: 'bad',
  unknown: 'neutral',
  check_failed: 'bad',
  not_provided: 'warn',
};

const CATEGORY_STATUS: Record<string, string> = {
  pass: 'Đạt',
  needs_review: 'Cần xem lại',
  blocked: 'Không đạt',
  unknown: 'Chưa kiểm tra được',
  check_failed: 'Lỗi kiểm tra',
  not_provided: 'Chưa cung cấp',
};

const STATUS_TONE: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
  PASS: 'ok',
  BLOCKED: 'bad',
  CHECK_FAILED: 'bad',
  NEEDS_REVIEW: 'warn',
  QUEUED: 'neutral',
  RUNNING: 'neutral',
};

const STATUS_LABEL: Record<string, string> = {
  PASS: 'Phù hợp',
  BLOCKED: 'Không đạt',
  CHECK_FAILED: 'Lỗi kiểm tra',
  NEEDS_REVIEW: 'Cần người duyệt',
  QUEUED: 'Đang chờ',
  RUNNING: 'Đang kiểm tra',
};

function statusTone(status: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  return STATUS_TONE[status] ?? 'neutral';
}

function formatTimestamp(ts: number | null | undefined): string {
  if (ts == null) return '—';
  const total = Math.floor(ts);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function IssueList({ issues }: { issues: PrecheckIssue[] }) {
  if (!issues || issues.length === 0) return null;
  return (
    <div className="stack">
      {issues.map((issue, index) => (
        <Alert
          key={`${issue.policy_category}-${issue.timestamp ?? index}`}
          tone={issue.action === 'block' ? 'bad' : issue.action === 'review' ? 'warn' : 'info'}
        >
          <div className="issue-row">
            <div className="issue-main">
              <strong>{issue.label || issue.policy_category}</strong>
              <span className="badge-wrap">
                <Badge tone={issue.action === 'block' ? 'bad' : issue.action === 'review' ? 'warn' : 'ok'}>
                  {issue.action === 'block' ? 'Chặn' : issue.action === 'review' ? 'Xem lại' : 'Thông tin'}
                </Badge>
                <Badge tone="neutral">{issue.risk_level}</Badge>
                {issue.timestamp != null ? <Badge tone="neutral">~{formatTimestamp(issue.timestamp)}</Badge> : null}
              </span>
            </div>
            {issue.reason ? <p className="issue-reason">{issue.reason}</p> : null}
            {issue.remediation ? (
              <p className="issue-remediation">
                <strong>Nguyên nhận:</strong> {issue.remediation}
              </p>
            ) : null}
            {issue.policy_ref ? <p className="issue-ref">{issue.policy_ref}</p> : null}
          </div>
        </Alert>
      ))}
    </div>
  );
}

function CategoryTable({ categories }: { categories: Record<string, PrecheckCategory> }) {
  return (
    <DataTable head={['Hạng mục', 'Kết quả', 'Chi tiết', 'Ghi chú']}>
      {Object.entries(categories ?? {}).map(([key, category]) => (
        <tr key={key}>
          <td>{CATEGORY_LABEL[key] ?? key}</td>
          <td>
            <Badge tone={CATEGORY_TONE[category.status] ?? 'neutral'}>
              {CATEGORY_STATUS[category.status] ?? category.status}
            </Badge>
          </td>
          <td className="cell-sub" style={{ maxWidth: '320px' }}>
            {(category.reasons ?? []).join(' · ')}
          </td>
          <td className="cell-sub" style={{ maxWidth: '420px' }}>
            {(category.suggestions ?? []).join(' · ')}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

export function VideoPrecheckSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [video, setVideo] = useState<File | null>(null);
  const [caption, setCaption] = useState('');
  const [accountId, setAccountId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<Precheck | null>(null);
  const [history, setHistory] = useState<Precheck[]>([]);

  async function loadHistory() {
    if (!workspace.pipelineId) return;
    try {
      const result = await api.get<{ prechecks: Precheck[] }>(
        `/api/pipelines/${workspace.pipelineId}/reels/precheck/history`,
      );
      setHistory(result.prechecks);
    } catch {
      setHistory([]);
    }
  }

  useEffect(() => {
    void loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  async function runCheck() {
    if (!video) {
      push('Chọn video trước', 'warn');
      return;
    }
    if (!accountId) {
      push('Chọn tài khoản Facebook', 'warn');
      return;
    }
    setBusy(true);
    setCheck(null);
    try {
      const form = new FormData();
      form.append('file', video);
      const uploaded = await api.upload<{ path: string }>(
        `/api/video-uploads?pipeline_id=${workspace.pipelineId}`,
        form,
      );
      const created = await api.post<{ id: number }>(`/api/pipelines/${workspace.pipelineId}/reels/precheck`, {
        account_id: accountId,
        media_path: uploaded.path,
        caption: caption.trim(),
      });
      await poll(created.id);
      await loadHistory();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra được video', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function poll(id: number) {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const result = await api.get<{ precheck: Precheck }>(
        `/api/pipelines/${workspace.pipelineId}/reels/precheck/${id}`,
      );
      setCheck(result.precheck);
      if (!['queued', 'running'].includes(result.precheck.status)) return;
      await new Promise((resolve) => window.setTimeout(resolve, 2500));
    }
  }

  async function retry() {
    if (!check) return;
    setBusy(true);
    try {
      await api.post(`/api/pipelines/${workspace.pipelineId}/reels/precheck/${check.id}/retry`, {});
      await poll(check.id);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra lại được', 'error');
    } finally {
      setBusy(false);
    }
  }

  const canPublish = check && check.status === 'PASS';
  const needsApproval = check && check.status === 'NEEDS_REVIEW' && !check.approved;
  const isBlocked = check && (check.status === 'BLOCKED' || check.status === 'CHECK_FAILED');

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Video Pre-Check</h2>
          <p className="hint">
            Mọi Reel phải qua kiểm tra an toàn nội dung trước khi xuất bản. Kết quả là gợi ý của công cụ,
            không phải quyết định của Facebook. Bản quyền luôn UNVERIFIED trừ khi bạn tải lên bằng chứng.
          </p>
        </div>
      </div>

      <Card title="1. Video cần kiểm tra">
        <div className="stack">
          <Input
            label="Video MP4 hoặc MOV"
            type="file"
            accept="video/mp4,video/quicktime"
            onChange={(event) => setVideo(event.target.files?.[0] ?? null)}
          />
          {video ? (
            <video
              src={URL.createObjectURL(video)}
              controls
              style={{ maxWidth: '100%', maxHeight: '200px', borderRadius: '8px' }}
            />
          ) : null}
          <Select
            label="Tài khoản Facebook"
            name="precheck_account"
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
          <Textarea
            label="Caption dự kiến"
            name="precheck_caption"
            rows={4}
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
            hint="Caption cũng được kiểm tra, và phải khớp với caption dùng khi đăng."
          />
          <Button variant="primary" loading={busy} onClick={() => void runCheck()}>
            Kiểm tra trước video
          </Button>
          {busy ? <Progress indeterminate /> : null}
        </div>
      </Card>

      {check ? (
        <Card title="2. Kết quả kiểm tra">
          <div className="stack">
            <div className="badge-row">
              <Badge tone={statusTone(check.status)}>
                {STATUS_LABEL[check.status] ?? check.status}
              </Badge>
              {check.vision_model ? <Badge tone="neutral">{check.vision_model}</Badge> : null}
              {check.policy_version ? <Badge tone="neutral">Bản đề {check.policy_version}</Badge> : null}
            </div>

            {check.status === 'CHECK_FAILED' ? (
              <Alert tone="bad">
                <p>
                  <strong>Kiểm tra thất bại:</strong> {check.error || 'Lỗi hệ thống.'}
                  {check.vision_model ? ` (mô hình: ${check.vision_model})` : ''}
                </p>
                <p>Không thể xuất bản khi kiểm tra lỗi. Nhấn "Kiểm tra lại" để thử lại.</p>
              </Alert>
            ) : null}

            <CategoryTable categories={check.categories} />

            {check.copyright_status ? (
              <Alert tone={check.copyright_status === 'VERIFIED' ? 'info' : 'warn'}>
                <strong>Bản quyền:</strong> {check.copyright_status === 'VERIFIED'
                  ? 'Đã xác minh nguồn gốc nội dung.'
                  : 'UNVERIFIED — bạn cần tự xác nhận quyền sử dụng.'}
              </Alert>
            ) : null}

            <IssueList issues={check.issues ?? []} />

            {check.warnings?.length ? (
              <Alert tone="warn">
                <ul>
                  {check.warnings.map((warning, index) => (
                    <li key={index}>{warning}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}

            {check.recommendations?.length ? (
              <Alert tone="info">
                <ul>
                  {check.recommendations.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}

            <div className="btn-row">
              {canPublish ? (
                <Alert tone="info">
                  <b>Đã sẵn sàng cho bài đăng</b> — video đạt mọi kiểm tra.
                </Alert>
              ) : isBlocked ? (
                <Button onClick={() => void retry()} loading={busy}>
                  Kiểm tra lại
                </Button>
              ) : needsApproval ? (
                <Button onClick={() => void retry()} loading={busy}>
                  Kiểm tra lại
                </Button>
              ) : (
                <Button onClick={() => void retry()} loading={busy}>
                  Kiểm tra lại
                </Button>
              )}
            </div>
          </div>
        </Card>
      ) : null}

      <Card title="Lịch sử kiểm tra">
        {history.length === 0 ? (
          <EmptyState title="Chưa có video nào được kiểm tra" />
        ) : (
          <DataTable head={['Tệp', 'Trạng thái', 'Kiểm tra lúc', '']}>
            {history.map((item) => (
              <tr key={item.id}>
                <td className="truncate" style={{ maxWidth: '240px' }}>
                  {item.media_id}
                </td>
                <td>
                  <Badge tone={STATUS_TONE[item.status] ?? 'neutral'}>
                    {STATUS_LABEL[item.status] ?? item.status}
                  </Badge>
                </td>
                <td>{item.checked_at ? new Date(item.checked_at).toLocaleString('vi-VN') : '—'}</td>
                <td className="actions">
                  <Button size="sm" onClick={() => setCheck(item)}>
                    Xem
                  </Button>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>
    </div>
  );
}
