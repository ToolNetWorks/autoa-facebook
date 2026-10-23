import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, DataTable, EmptyState, Skeleton } from '@/components/ui/Surfaces';

const WEEKDAYS = [
  { value: 1, label: 'Thứ hai' },
  { value: 2, label: 'Thứ ba' },
  { value: 3, label: 'Thứ tư' },
  { value: 4, label: 'Thứ năm' },
  { value: 5, label: 'Thứ sáu' },
  { value: 6, label: 'Thứ bảy' },
  { value: 0, label: 'Chủ nhật' },
];

interface ScheduleSettings {
  mode: 'spread' | 'windows';
  timezone: string;
  start_time: string;
  end_time: string;
  windows: { start: string; end: string }[];
  min_interval_minutes: number;
  max_posts_per_account_per_day: number;
  days_enabled: number[];
  group_cooldown_minutes: number;
  paused: boolean;
}

interface PreviewAccount {
  account_id: number;
  label: string;
  daily_limit: number;
  used: number;
  remaining: number;
  at_limit: boolean;
  paused: boolean;
  planned: number;
  slots: { group: string; media_id: number; scheduled_for: string }[];
}

interface PreviewPayload {
  settings: ScheduleSettings;
  accounts: PreviewAccount[];
  notes: string[];
  reason: string;
  expected_jobs: number;
  next_allowed: string | null;
}

export function ScheduleModal({ pipelineId, open, onClose, onChanged }: {
  pipelineId: number | null;
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { push } = useToast();
  const [settings, setSettings] = useState<ScheduleSettings | null>(null);
  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  async function load() {
    if (!pipelineId) return;
    setLoading(true);
    try {
      // Load schedule and preview independently to handle partial failures
      const [scheduleResponse, previewResponse] = await Promise.allSettled([
        api.get<{ schedule: ScheduleSettings }>(`/api/pipelines/${pipelineId}/schedule`),
        api.get<PreviewPayload>(`/api/pipelines/${pipelineId}/schedule/preview`),
      ]);

      if (scheduleResponse.status === 'fulfilled') {
        setSettings(scheduleResponse.value.schedule);
      } else {
        push(`Không tải được cấu hình lịch: ${scheduleResponse.reason?.message ?? 'Lỗi không xác định'}`, 'error');
      }

      if (previewResponse.status === 'fulfilled') {
        setPreview(previewResponse.value);
      } else {
        push(`Không tải được bản xem trước: ${previewResponse.reason?.message ?? 'Lỗi không xác định'}`, 'error');
      }
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được lịch đăng', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (open) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pipelineId]);

  function update(patch: Partial<ScheduleSettings>) {
    setSettings((current) => (current ? { ...current, ...patch } : current));
  }

  function toggleDay(day: number) {
    if (!settings) return;
    const days = settings.days_enabled.includes(day)
      ? settings.days_enabled.filter((value) => value !== day)
      : [...settings.days_enabled, day];
    update({ days_enabled: days });
  }

  async function save() {
    if (!settings || !pipelineId) return;
    setSaving(true);
    try {
      await api.put(`/api/pipelines/${pipelineId}/schedule`, {
        mode: settings.mode,
        timezone: settings.timezone,
        start_time: settings.start_time,
        end_time: settings.end_time,
        min_interval_minutes: settings.min_interval_minutes,
        max_posts_per_account_per_day: settings.max_posts_per_account_per_day,
        days_enabled_json: JSON.stringify(settings.days_enabled),
        group_cooldown_minutes: settings.group_cooldown_minutes,
        paused: settings.paused,
      });
      push('Đã lưu lịch đăng cho toàn bộ Facebook', 'success');
      await load();
      onChanged();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được lịch đăng', 'error');
    } finally {
      setSaving(false);
    }
  }

  async function allocate() {
    if (!pipelineId) return;
    setRunning(true);
    try {
      const result = await api.post<{ created: unknown[]; reason: string; planned: number }>(
        `/api/pipelines/${pipelineId}/schedule/allocate`,
      );
      push(
        result.created.length > 0
          ? `Đã xếp lịch ${result.created.length} bài`
          : result.reason || 'Chưa có bài nào đủ điều kiện để xếp lịch',
        result.created.length > 0 ? 'success' : 'info',
      );
      await load();
      onChanged();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xếp lịch được', 'error');
    } finally {
      setRunning(false);
    }
  }

  return (
    <Modal
      open={open}
      wide
      title="Lịch đăng"
      description="Một lịch chung cho toàn bộ Facebook của Workspace. Mỗi tài khoản vẫn có hạn mức và lịch sử riêng."
      onClose={onClose}
      footer={
        <>
          <Button variant="primary" loading={saving} onClick={() => void save()}>
            Lưu lịch
          </Button>
          <Button loading={running} onClick={() => void allocate()}>
            Xếp lịch ngay
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Đóng
          </Button>
        </>
      }
    >
      {loading && !settings ? (
        <Skeleton variant="block" />
      ) : settings ? (
        <div className="stack">
          <Switch
            id="schedule_paused"
            checked={settings.paused}
            onChange={(checked) => update({ paused: checked })}
            label="Tạm dừng lịch tự động"
            description="Bài đã xếp lịch vẫn giữ nguyên, hệ thống không xếp thêm."
          />

          <Select
            label="Chế độ"
            name="schedule_mode"
            value={settings.mode}
            onChange={(event) => update({ mode: event.target.value as 'spread' | 'windows' })}
          >
            <option value="spread">Tự động phân bổ đều trong khung giờ</option>
            <option value="windows">Tự chọn một hoặc nhiều khung giờ</option>
          </Select>

          <div className="form-grid">
            <Input
              label="Từ giờ"
              name="schedule_start"
              type="time"
              value={settings.start_time}
              onChange={(event) => update({ start_time: event.target.value })}
            />
            <Input
              label="Đến giờ"
              name="schedule_end"
              type="time"
              value={settings.end_time}
              onChange={(event) => update({ end_time: event.target.value })}
            />
          </div>
          <p className="hint">Khung giờ vượt qua nửa đêm (ví dụ 22:00 đến 02:00) vẫn dùng được.</p>

          <Input
            label="Múi giờ của Workspace"
            name="schedule_timezone"
            value={settings.timezone}
            onChange={(event) => update({ timezone: event.target.value })}
          />

          <div>
            <span className="field-label">Ngày được phép đăng</span>
            <div className="choice-grid">
              {WEEKDAYS.map((day) => (
                <label className="choice" key={day.value}>
                  <input
                    type="checkbox"
                    name={`schedule-day-${day.value}`}
                    checked={settings.days_enabled.includes(day.value)}
                    onChange={() => toggleDay(day.value)}
                  />
                  <span>{day.label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="form-grid">
            <Input
              label="Số bài tối đa / Facebook / ngày"
              name="schedule_max"
              type="number"
              min={1}
              max={50}
              value={settings.max_posts_per_account_per_day}
              onChange={(event) =>
                update({ max_posts_per_account_per_day: Number(event.target.value) })
              }
            />
            <Input
              label="Khoảng cách tối thiểu giữa hai bài (phút)"
              name="schedule_interval"
              type="number"
              min={0}
              max={1440}
              value={settings.min_interval_minutes}
              onChange={(event) => update({ min_interval_minutes: Number(event.target.value) })}
            />
            <Input
              label="Khoảng nghỉ tối thiểu của một group (phút)"
              name="schedule_cooldown"
              type="number"
              min={0}
              max={1440}
              value={settings.group_cooldown_minutes}
              onChange={(event) => update({ group_cooldown_minutes: Number(event.target.value) })}
            />
          </div>

          {preview?.reason ? (
            <Alert tone="info">{preview.reason}</Alert>
          ) : null}
          {preview && preview.next_allowed ? (
            <Alert tone="info">Khung giờ cho phép đăng tiếp theo bắt đầu {preview.next_allowed}.</Alert>
          ) : null}
          {preview && preview.notes.length > 0 ? (
            <Alert tone="warn">{preview.notes.slice(0, 3).join(' · ')}</Alert>
          ) : null}

          <div>
            <span className="field-label">
              Lịch dự kiến · {preview?.expected_jobs ?? 0} bài
            </span>
            {!preview || preview.accounts.length === 0 ? (
              <EmptyState title="Chưa có lịch" description="Chưa có tài khoản nào đủ điều kiện để xếp lịch." />
            ) : (
              <DataTable head={['Facebook', 'Hạn mức', 'Đã dùng', 'Số bài dự kiến', 'Thời điểm']}>
                {preview.accounts.map((account) => (
                  <tr key={account.account_id}>
                    <td>{account.label}</td>
                    <td>{account.daily_limit}</td>
                    <td>{account.used}</td>
                    <td>
                      {account.at_limit ? (
                        <Badge tone="warn">Đủ hạn mức</Badge>
                      ) : (
                        <Badge tone="ok">{account.planned}</Badge>
                      )}
                    </td>
                    <td>
                      {account.slots.length === 0 ? (
                        '—'
                      ) : (
                        account.slots.map((slot) => (
                          <div key={slot.scheduled_for} className="cell-sub">
                            {new Date(slot.scheduled_for).toLocaleString('vi-VN', {
                              day: '2-digit',
                              month: '2-digit',
                              hour: '2-digit',
                              minute: '2-digit',
                            })}{' '}
                            · {slot.group}
                          </div>
                        ))
                      )}
                    </td>
                  </tr>
                ))}
              </DataTable>
            )}
          </div>

          <p className="hint">
            Khoảng cách thời gian chỉ là một lớp kiểm soát để giảm rủi ro spam. Hệ thống không cam kết tránh
            được hạn chế của Facebook.
          </p>
        </div>
      ) : null}
    </Modal>
  );
}
