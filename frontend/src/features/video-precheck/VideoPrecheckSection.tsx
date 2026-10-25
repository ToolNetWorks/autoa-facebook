import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card, DataTable, EmptyState, Progress } from '@/components/ui/Surfaces';
import type { Precheck, PrecheckCategory, PrecheckIssue } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const CATEGORY_LABEL: Record<string, string> = {
  technical: 'Chất lượng video',
  visual: 'Nội dung hình ảnh',
  ocr: 'Văn bản trong video',
  caption: 'Caption và hashtag',
  copyright: 'Bản quyền',
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
  unknown: 'Chưa kiểm tra',
  check_failed: 'Lỗi',
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
  PASS: 'Đạt',
  BLOCKED: 'Không đạt',
  CHECK_FAILED: 'Lỗi',
  NEEDS_REVIEW: 'Cần xem lại',
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

function UserFriendlyReason(category: string, reasons: string[]): string {
  if (!reasons || reasons.length === 0) return '';
  const text = reasons.join(' ');
  const mapping: Record<string, string> = {
    technical: text.includes('đen') || text.includes('đóng băng')
      ? 'Video bị tối hoặc đóng khung; cần thêm ánh sáng hoặc nội dung rõ ràng.'
      : 'Video không đạt yêu cầu kỹ thuật.',
    visual: text.toLowerCase().includes('nhạy cảm')
      ? 'Phát hiện nội dung nhạy cảm trong hình ảnh.'
      : 'Nội dung hình ảnh cần xem lại.',
    ocr: 'Văn bản trong video có thể chứa nội dung cần kiểm tra.',
    caption: 'Caption chứa từ ngữ vi phạm.',
    copyright: 'Nội dung video chưa được xác minh bản quyền.',
  };
  return mapping[category] || text.slice(0, 200);
}

function UserFriendlySuggestion(category: string, suggestions: string[]): string {
  if (!suggestions || suggestions.length === 0) return '';
  const text = suggestions.join(' ');
  const mapping: Record<string, string> = {
    technical: 'Thử quay lại với ánh sáng tốt hơn, góc rộng hơn, hoặc thời lượng dài hơn.',
    visual: 'Chỉnh sửa nội dung hình ảnh trước khi đăng lại.',
    ocr: 'Xóa hoặc chỉnh sửa văn bản trong video.',
    caption: 'Điều chỉnh caption theo hướng dẫn an toàn.',
    copyright: 'Cung cấp bằng chứng sở hữu nội dung hoặc dùng nội dung khác.',
  };
  return mapping[category] || text.slice(0, 200);
}

function IssueList({ issues }: { issues: PrecheckIssue[] }) {
  if (!issues || issues.length === 0) return null;
  return (
    <div className="stack">
      {issues.map((issue, index) => (
        <Alert
          key={index}
          tone={issue.action === 'block' ? 'bad' : issue.action === 'review' ? 'warn' : 'info'}
        >
          <div className="issue-row">
            <strong>{issue.label || issue.policy_category}</strong>
            {issue.timestamp != null ? <small className="muted"> ~{formatTimestamp(issue.timestamp)}</small> : null}
            {issue.reason ? <p className="issue-reason">{issue.reason}</p> : null}
            {issue.remediation ? (
              <p className="issue-remediation">
                <strong>Gợi ý:</strong> {issue.remediation}
              </p>
            ) : null}
          </div>
        </Alert>
      ))}
    </div>
  );
}

function CategoryTable({ categories }: { categories: Record<string, PrecheckCategory> }) {
  const entries = Object.entries(categories ?? {}).filter(([, cat]) => cat && cat.checked);
  if (entries.length === 0) return null;
  return (
    <DataTable head={['Hạng mục', 'Kết quả', 'Chi tiết']}>
      {entries.map(([key, category]) => (
        <tr key={key}>
          <td>{CATEGORY_LABEL[key] ?? key}</td>
          <td>
            <Badge tone={CATEGORY_TONE[category.status] ?? 'neutral'}>
              {CATEGORY_STATUS[category.status] ?? category.status}
            </Badge>
          </td>
          <td className="cell-sub">
            {category.reasons && category.reasons.length > 0
              ? UserFriendlyReason(key, category.reasons)
              : ''}
            {category.suggestions && category.suggestions.length > 0
              ? ` ${UserFriendlySuggestion(key, category.suggestions)}`
              : ''}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

function summarizeProblems(categories: Record<string, PrecheckCategory>, issues: PrecheckIssue[]): {
  hasProblems: boolean;
  blocked: boolean;
  messages: string[];
} {
  const messages: string[] = [];
  let blocked = false;

  for (const [key, cat] of Object.entries(categories ?? {})) {
    if (cat.status === 'blocked') {
      blocked = true;
      messages.push(`Hạng mục "${CATEGORY_LABEL[key] ?? key}" không đạt`);
    } else if (cat.status === 'needs_review') {
      messages.push(`Hạng mục "${CATEGORY_LABEL[key] ?? key}" cần xem lại`);
    } else if (cat.status === 'check_failed') {
      blocked = true;
      messages.push(`Hạng mục "${CATEGORY_LABEL[key] ?? key}" lỗi kiểm tra`);
    }
  }

  for (const issue of issues) {
    if (issue.action === 'block') {
      blocked = true;
      messages.push(issue.label || issue.policy_category);
    } else if (issue.action === 'review') {
      messages.push(issue.label || issue.policy_category);
    }
  }

  return { hasProblems: messages.length > 0, blocked, messages };
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
  const needsApproval = check && check.status === 'NEEDS_REVIEW';
  const isBlocked = check && (check.status === 'BLOCKED' || check.status === 'CHECK_FAILED');

  const summary = check
    ? summarizeProblems(check.categories, check.issues ?? [])
    : { hasProblems: false, blocked: false, messages: [] };

  return (
    <div className="stack">
      <div className="section-title">
        <h2>Video Pre-Check</h2>
        <p className="hint">
          Mọi Reel phải qua kiểm tra an toàn nội dung trước khi xuất bản. Kết quả là gợi ý,
          không phải quyết định của Facebook. Bản quyền luôn chưa xác minh trừ khi bạn cung cấp bằng chứng.
        </p>
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
            hint="Caption sẽ được kiểm tra cùng video."
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
            <Badge tone={statusTone(check.status)}>
              {STATUS_LABEL[check.status] ?? check.status}
            </Badge>

            {check.status === 'CHECK_FAILED' ? (
              <Alert tone="bad">
                <p><strong>Không thể hoàn tành kiểm tra.</strong> Nhấn "Kiểm tra lại" để thử lại.</p>
              </Alert>
            ) : null}

            <CategoryTable categories={check.categories} />

            {check.copyright_status ? (
              <Alert tone={check.copyright_status === 'VERIFIED' ? 'info' : 'warn'}>
                <strong>Bản quyền:</strong>{' '}
                {check.copyright_status === 'VERIFIED'
                  ? 'Đã xác minh nguồn gốc nội dung.'
                  : 'Chưa xác minh — bạn cần tự xác nhận quyền sử dụng.'}
              </Alert>
            ) : null}

            <IssueList issues={check.issues ?? []} />

            {summary.hasProblems ? (
              <Alert tone={summary.blocked ? 'bad' : 'warn'}>
                <strong>{summary.blocked ? 'Không đạt' : 'Cần xem lại'}:</strong>
                <ul>
                  {summary.messages.map((msg, index) => (
                    <li key={index}>{msg}</li>
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
                  <b>Đã sẵn sàng cho bài đăng</b> — video qua mọi kiểm tra.
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
          <DataTable head={['Kết quả', 'Thời gian', '']}>
            {history.map((item) => (
              <tr key={item.id}>
                <td>
                  <Badge tone={statusTone(item.status)}>
                    {STATUS_LABEL[item.status] ?? item.status}
                  </Badge>
                </td>
                <td>{item.created_at ? new Date(item.created_at).toLocaleString('vi-VN') : '—'}</td>
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
