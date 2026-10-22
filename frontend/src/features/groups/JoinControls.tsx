import { useCallback, useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Textarea } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge } from '@/components/ui/Surfaces';

type Tone = 'ok' | 'warn' | 'bad' | 'neutral' | 'info';

const STATUS_LABEL: Record<string, string> = {
  not_member: 'Chưa tham gia',
  join_requesting: 'Đang gửi yêu cầu',
  pending_approval: 'Đang chờ group duyệt',
  joined: 'Đã là thành viên',
  join_rejected: 'Bị từ chối',
  join_failed: 'Tham gia lỗi',
  unknown: 'Chưa xác định',
};

const STATUS_TONE: Record<string, Tone> = {
  not_member: 'warn',
  join_requesting: 'info',
  pending_approval: 'warn',
  joined: 'ok',
  join_rejected: 'bad',
  join_failed: 'bad',
  unknown: 'neutral',
};

export interface MembershipState {
  group_id: number;
  account_id: number;
  membership_status: string;
  status_label: string;
  status_tone: Tone;
  join_questions: string[];
  join_answers: Record<string, string>;
  last_checked_at: string | null;
  join_requested_at: string | null;
  last_error_code: string;
  group_label: string;
  account_label: string;
  auto_join: boolean;
}

/**
 * Điều khiển thành viên của một group: bật tự động yêu cầu tham gia, xem
 * trạng thái, gửi yêu cầu và trả lời câu hỏi của group.
 */
export function JoinControls({
  pipelineId,
  groupId,
  membership,
  onChanged,
  canManage,
}: {
  pipelineId: number;
  groupId: number;
  membership?: MembershipState;
  onChanged: () => void;
  canManage: boolean;
}) {
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  const [questions, setQuestions] = useState<string[] | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  const status = membership?.membership_status ?? 'unknown';

  const requestJoin = async () => {
    setBusy(true);
    try {
      const result = await api.post<{
        membership: string;
        questions: string[];
        message: string;
      }>(`/api/pipelines/${pipelineId}/groups/${groupId}/join`, {});
      if (result.questions && result.questions.length > 0) {
        setQuestions(result.questions);
        setAnswers(membership?.join_answers ?? {});
      } else {
        push(result.message || 'Đã gửi yêu cầu tham gia', 'success');
      }
      onChanged();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không gửi được yêu cầu', 'error');
    } finally {
      setBusy(false);
    }
  };

  const sendAnswers = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ message: string }>(
        `/api/pipelines/${pipelineId}/groups/${groupId}/join/answers`,
        { answers },
      );
      push(result.message || 'Đã gửi yêu cầu tham gia', 'success');
      setQuestions(null);
      onChanged();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không gửi được yêu cầu', 'error');
    } finally {
      setBusy(false);
    }
  };

  const settled = status === 'joined' || status === 'pending_approval' || status === 'join_rejected';

  return (
    <div className="stack-sm">
      <div className="badge-row">
        <Badge tone={STATUS_TONE[status] ?? 'neutral'}>
          {STATUS_LABEL[status] ?? 'Chưa xác định'}
        </Badge>
        {membership?.last_checked_at ? (
          <span className="cell-sub">
            {new Date(membership.last_checked_at).toLocaleString('vi-VN')}
          </span>
        ) : null}
      </div>
      {canManage ? (
        <div className="btn-row">
          <label className="choice">
            <input
              type="checkbox"
              name={`auto_join_${groupId}`}
              checked={false}
              disabled
              readOnly
            />
            <span>Tự động yêu cầu tham gia (đã tắt)</span>
          </label>
          <Alert tone="info">
            Hệ thống không tự động tham gia group, không gửi yêu cầu tham gia và không trả lời
            câu hỏi group. Hành động tham gia (nếu cần) chỉ thực hiện bởi bạn bên ngoài hệ thống.
          </Alert>
          <Button size="sm" loading={busy} onClick={() => void requestJoin()}>
            {settled ? 'Kiểm tra lại' : 'Tham gia group'}
          </Button>
        </div>
      ) : null}
      {status === 'join_rejected' ? (
        <Alert tone="warn">Group đã từ chối yêu cầu tham gia của tài khoản này.</Alert>
      ) : null}
      {status === 'unknown' ? (
        <p className="hint">Chưa xác định được tư cách thành viên. Bấm “Kiểm tra lại” để thử.</p>
      ) : null}
      <Modal
        open={questions !== null}
        title="Group yêu cầu trả lời câu hỏi"
        description="Câu trả lời được gửi đúng như bạn nhập."
        onClose={() => setQuestions(null)}
        footer={
          <>
            <Button onClick={() => setQuestions(null)}>Huỷ</Button>
            <Button variant="primary" loading={busy} onClick={() => void sendAnswers()}>
              Gửi yêu cầu tham gia
            </Button>
          </>
        }
      >
        <div className="stack">
          {(questions ?? []).map((question) => (
            <div key={question}>
              <Textarea
                label={question}
                name={`join_answer_${question}`}
                rows={2}
                value={answers[question] ?? ''}
                onChange={(event) =>
                  setAnswers((current) => ({ ...current, [question]: event.target.value }))
                }
              />
            </div>
          ))}
        </div>
      </Modal>
    </div>
  );
}

/** Danh sách trạng thái thành viên của Workspace, tự cập nhật khi worker chạy. */
export function useMemberships(pipelineId: number | null) {
  const [rows, setRows] = useState<MembershipState[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!pipelineId) {
      setLoading(false);
      return;
    }
    if (!quiet) setLoading(true);
    try {
      const result = await api.get<{ memberships: MembershipState[] }>(
        `/api/pipelines/${pipelineId}/groups/memberships`,
      );
      setRows(result.memberships);
      setError(null);
    } catch (failure) {
      if (!quiet) {
        setError(failure instanceof Error ? failure.message : 'Không tải được trạng thái thành viên');
      }
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [pipelineId]);

  useEffect(() => {
    void load();
  }, [load]);

  const byGroup = new Map<number, MembershipState>();
  rows.forEach((row) => {
    const groupId = Number(row.group_id);
    if (!byGroup.has(groupId)) byGroup.set(groupId, row);
  });

  return { rows, loading, error, reload: load, byGroup };
}
