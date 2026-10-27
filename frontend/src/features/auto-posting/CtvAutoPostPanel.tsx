import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import { Alert, Card, DataTable, EmptyState, ErrorState, Skeleton } from '@/components/ui/Surfaces';
import { Modal } from '@/components/ui/Modal';
import { GroupStatusList } from '@/features/auto-posting/GroupStatusList';
import { AutoPostJobList } from '@/features/auto-posting/AutoPostJobList';
import type { CtvAutoPostConfig, CtvAutoPostSettings } from '@/types/api';
import type { FacebookAccount, GroupRow } from '@/types/api';

const WEEKDAYS = [
  { value: 1, label: 'T2' },
  { value: 2, label: 'T3' },
  { value: 3, label: 'T4' },
  { value: 4, label: 'T5' },
  { value: 5, label: 'T6' },
  { value: 6, label: 'T7' },
  { value: 0, label: 'CN' },
];

const IMAGE_RULES = [
  { value: 'rotate', label: 'Xoay vòng ảnh (chọn ảnh hợp chủ đề nhất)' },
  { value: 'unused_first', label: 'Ưu tiên ảnh chưa dùng bao giờ' },
  { value: 'random', label: 'Chọn ngẫu nhiên trong ngày' },
];

interface PreviewPayload {
  enabled: boolean;
  reason: string;
  expected_jobs: number;
  next_allowed: string | null;
  notes: string[];
  schedule: { start_time: string; end_time: string; timezone: string; days_enabled: number[] };
  accounts: {
    account_id: number;
    label: string;
    used: number;
    daily_limit: number;
    remaining: number;
    planned: number;
    slots: { group: string; scheduled_for: string }[];
  }[];
}

export function CtvAutoPostPanel({
  pipelineId,
  accounts,
  groups,
}: {
  pipelineId: number;
  accounts: FacebookAccount[];
  groups: GroupRow[];
}) {
  const { push } = useToast();
  const [config, setConfig] = useState<CtvAutoPostConfig | null>(null);
  const [draft, setDraft] = useState<CtvAutoPostSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [saving, setSaving] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [preview, setPreview] = useState<PreviewPayload | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [mainTab, setMainTab] = useState<'config' | 'jobs'>('config');

  async function load() {
    setLoading(true);
    setLoadError(null);
    setForbidden(false);
    try {
      const response = await api.get<CtvAutoPostConfig>(
        `/api/pipelines/${pipelineId}/auto-post/my-config`,
      );
      setConfig(response);
      setDraft(response.settings);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 403) {
        setForbidden(true);
      } else {
        setLoadError(failure instanceof Error ? failure.message : 'Không tải được cấu hình');
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  function update(patch: Partial<CtvAutoPostSettings>) {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }

  function updateRules(patch: Partial<CtvAutoPostSettings['schedule_rules']>) {
    setDraft((current) =>
      current ? { ...current, schedule_rules: { ...current.schedule_rules, ...patch } } : current,
    );
  }

  async function save(activate: boolean) {
    if (!draft) return;
    setSaving(true);
    try {
      const response = await api.put<{ settings: CtvAutoPostSettings }>(
        `/api/pipelines/${pipelineId}/auto-post/my-config`,
        {
          enabled: activate ? true : draft.enabled,
          account_scope: draft.account_scope,
          selected_account_ids: draft.selected_account_ids,
          selected_group_ids: draft.selected_group_ids,
          topic: draft.topic,
          caption_template: draft.caption_template,
          max_posts_per_account_daily: draft.max_posts_per_account_daily,
          start_time: draft.start_time,
          end_time: draft.end_time,
          timezone: draft.timezone,
          schedule_rules: draft.schedule_rules,
          ai_caption_enabled: draft.ai_caption_enabled,
          approval_required: draft.approval_required,
          anti_duplicate: draft.anti_duplicate,
          apply_to_new_accounts: draft.apply_to_new_accounts,
        },
      );
      setDraft(response.settings);
      setConfig((current) => (current ? { ...current, configured: true, settings: response.settings } : current));
      push(activate ? 'Đã lưu và bật tự động đăng của bạn' : 'Đã lưu cấu hình', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được cấu hình', 'error');
    } finally {
      setSaving(false);
    }
  }

  async function toggle(enabled: boolean) {
    setSaving(true);
    try {
      const path = `/api/pipelines/${pipelineId}/auto-post/my-config/${enabled ? 'activate' : 'pause'}`;
      const response = await api.post<{ settings: CtvAutoPostSettings }>(path);
      setDraft(response.settings);
      setConfig((current) => (current ? { ...current, settings: response.settings } : current));
      push(enabled ? 'Đã bật tự động đăng của bạn' : 'Đã tạm dừng tự động đăng của bạn', 'success');
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không đổi được trạng thái', 'error');
    } finally {
      setSaving(false);
    }
  }

  async function openPreview() {
    setPreviewOpen(true);
    setPreviewLoading(true);
    try {
      const response = await api.get<PreviewPayload>(
        `/api/pipelines/${pipelineId}/auto-post/my-config/preview`,
      );
      setPreview(response);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xem được lịch đăng', 'error');
    } finally {
      setPreviewLoading(false);
    }
  }

  const accountOptions = useMemo(
    () => accounts.map((account) => ({ id: account.id, label: account.label })),
    [accounts],
  );
  const groupOptions = useMemo(
    () => groups.map((group) => ({ id: group.id, label: group.label })),
    [groups],
  );

  if (loading && !draft) {
    return (
      <div className="stack">
        <Skeleton variant="block" />
        <Skeleton variant="block" />
      </div>
    );
  }

  if (forbidden) {
    return (
      <ErrorState
        title="Bạn không có quyền tự động đăng group"
        description="Tài khoản của bạn chưa được cấp quyền cấu hình tự động đăng. Nhờ quản trị cấp quyền trong phần Cộng tác viên."
        action={
          <Button size="sm" onClick={() => void load()}>
            Thử lại
          </Button>
        }
      />
    );
  }

  if (loadError || !draft) {
    return (
      <ErrorState
        title="Không tải được cấu hình tự động đăng"
        description={loadError ?? 'Đã xảy ra lỗi khi tải dữ liệu.'}
        action={
          <Button size="sm" onClick={() => void load()}>
            Thử lại
          </Button>
        }
      />
    );
  }

  const configured = config?.configured ?? false;
  const policy = config?.policy;
  const limitCeiling = policy?.daily_limit_per_account ?? draft.max_posts_per_account_daily;

  return (
    <div className="stack">
      {!configured ? (
        <Alert tone="info" title="Chưa thiết lập.">
          Bạn chưa thiết lập tự động đăng group. Hãy chọn Facebook, group, lịch đăng và giới hạn của riêng bạn,
          rồi bấm <b>Lưu và kích hoạt</b>.
        </Alert>
      ) : null}

      <div className="tabs" role="tablist" style={{ marginBottom: 'var(--sp-2)' }}>
        <button
          type="button"
          role="tab"
          aria-selected={mainTab === 'config'}
          className={`tab ${mainTab === 'config' ? 'active' : ''}`}
          onClick={() => setMainTab('config')}
        >
          Cấu hình tự động
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mainTab === 'jobs'}
          className={`tab ${mainTab === 'jobs' ? 'active' : ''}`}
          onClick={() => setMainTab('jobs')}
        >
          Danh sách bài đăng
        </button>
      </div>

      {mainTab === 'jobs' ? (
        <AutoPostJobList pipelineId={pipelineId} />
      ) : (
        <>
          <Card
            title="Cấu hình của tôi"
            description="Chỉ áp dụng cho tài khoản của bạn trong Workspace này."
        actions={
          <>
            <Button size="sm" onClick={() => void openPreview()}>
              Xem trước lịch đăng
            </Button>
            <Button size="sm" onClick={() => void load()}>
              Làm mới
            </Button>
          </>
        }
      >
        <div className="stack">
          <Switch
            id="ctv_auto_enabled"
            checked={draft.enabled}
            onChange={(checked) => void toggle(checked)}
            label="Bật tự động đăng group"
            description={
              configured
                ? 'Hệ thống tự chuẩn bị bài và xếp lịch theo giới hạn của bạn.'
                : 'Trạng thái ban đầu là tắt. Bật lên sau khi bấm Lưu và kích hoạt.'
            }
          />

          <Select
            label="Áp dụng cho"
            name="ctv_scope"
            value={draft.account_scope}
            onChange={(event) =>
              update({ account_scope: event.target.value as 'all' | 'selected' })
            }
          >
            <option value="all">Tất cả Facebook được phép</option>
            <option value="selected">Chỉ những Facebook được chọn</option>
          </Select>

          {draft.account_scope === 'selected' ? (
            accountOptions.length === 0 ? (
              <p className="hint">Workspace chưa có Facebook nào cho bạn dùng.</p>
            ) : (
              <div className="stack-sm">
                <div className="choice-actions">
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => update({ selected_account_ids: accountOptions.map((item) => item.id) })}
                  >
                    Chọn tất cả
                  </button>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => update({ selected_account_ids: [] })}
                  >
                    Bỏ chọn tất cả
                  </button>
                </div>
                <div className="choice-scroll">
                  <div className="choice-grid">
                    {accountOptions.map((account) => (
                      <label className="choice" key={account.id}>
                        <input
                          type="checkbox"
                          name="ctv_accounts"
                          checked={draft.selected_account_ids.includes(account.id)}
                          onChange={(event) =>
                            update({
                              selected_account_ids: event.target.checked
                                ? [...draft.selected_account_ids, account.id]
                                : draft.selected_account_ids.filter((id) => id !== account.id),
                            })
                          }
                        />
                        <span>{account.label}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </div>
            )
          ) : null}

          <div>
            <span className="field-label">Group sẽ tự động đăng</span>
            {groupOptions.length === 0 ? (
              <p className="hint">Workspace chưa có group nào.</p>
            ) : (
              <div className="stack-sm">
                <div className="choice-actions">
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => update({ selected_group_ids: groupOptions.map((item) => item.id) })}
                  >
                    Chọn tất cả
                  </button>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => update({ selected_group_ids: [] })}
                  >
                    Bỏ chọn tất cả
                  </button>
                </div>
                <div className="choice-scroll">
                  <div className="choice-grid">
                    {groupOptions.map((group) => (
                      <label className="choice" key={group.id}>
                        <input
                          type="checkbox"
                          name="ctv_groups"
                          checked={
                            draft.selected_group_ids.length === 0 ||
                            draft.selected_group_ids.includes(group.id)
                          }
                          onChange={(event) => {
                            const all = groupOptions.map((option) => option.id);
                            const current =
                              draft.selected_group_ids.length === 0
                                ? all
                                : draft.selected_group_ids;
                            update({
                              selected_group_ids: event.target.checked
                                ? [...new Set([...current, group.id])]
                                : current.filter((id) => id !== group.id),
                            });
                          }}
                        />
                        <span>{group.label}</span>
                      </label>
                    ))}
                  </div>
                </div>
              </div>
            )}
            <p className="hint">Không chọn group nào nghĩa là dùng toàn bộ group của Workspace.</p>
          </div>

          <Textarea
            label="Chủ đề / Caption mẫu"
            name="ctv_topic"
            rows={2}
            value={draft.topic}
            onChange={(event) => update({ topic: event.target.value })}
            hint="AI dùng chủ đề này để chọn ảnh phù hợp và viết caption."
          />

          <Textarea
            label="Caption mẫu (không bắt buộc)"
            name="ctv_caption_template"
            rows={2}
            value={draft.caption_template}
            onChange={(event) => update({ caption_template: event.target.value })}
            hint="Dùng làm mẫu cho caption của bạn."
          />

          <div className="form-grid">
            <Input
              label="Số bài tối đa / Facebook / ngày"
              name="ctv_daily_limit"
              type="number"
              min={1}
              max={Math.max(1, limitCeiling)}
              value={draft.max_posts_per_account_daily}
              onChange={(event) =>
                update({ max_posts_per_account_daily: Number(event.target.value) })
              }
              hint={policy ? `Quản trị giới hạn tối đa ${policy.daily_limit_per_account} bài.` : undefined}
            />
            <Input
              label="Khung giờ bắt đầu"
              name="ctv_start_time"
              type="time"
              value={draft.start_time}
              onChange={(event) => update({ start_time: event.target.value })}
            />
            <Input
              label="Khung giờ kết thúc"
              name="ctv_end_time"
              type="time"
              value={draft.end_time}
              onChange={(event) => update({ end_time: event.target.value })}
            />
          </div>

          <details
            open={advanced}
            onToggle={(event) => setAdvanced((event.target as HTMLDetailsElement).open)}
          >
            <summary>Cài đặt nâng cao</summary>
            <div className="stack-sm">
              <div>
                <span className="field-label">Ngày trong tuần được phép đăng</span>
                <div className="choice-grid">
                  {WEEKDAYS.map((day) => (
                    <label className="choice" key={day.value}>
                      <input
                        type="checkbox"
                        name={`ctv_day_${day.value}`}
                        checked={draft.schedule_rules.days_enabled.includes(day.value)}
                        onChange={(event) => {
                          const days = draft.schedule_rules.days_enabled;
                          updateRules({
                            days_enabled: event.target.checked
                              ? [...new Set([...days, day.value])].sort()
                              : days.filter((item) => item !== day.value),
                          });
                        }}
                      />
                      <span>{day.label}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div className="form-grid">
                <Input
                  label="Khoảng nghỉ tối thiểu (phút)"
                  name="ctv_gap"
                  type="number"
                  min={0}
                  max={1440}
                  value={draft.schedule_rules.min_gap_minutes}
                  onChange={(event) =>
                    updateRules({ min_gap_minutes: Number(event.target.value) })
                  }
                />
                <Input
                  label="Giới hạn theo group (bài/ngày, 0 = không giới hạn)"
                  name="ctv_group_limit"
                  type="number"
                  min={0}
                  max={50}
                  value={draft.schedule_rules.group_daily_limit}
                  onChange={(event) =>
                    updateRules({ group_daily_limit: Number(event.target.value) })
                  }
                />
              </div>

              <Select
                label="Quy tắc chọn ảnh trong Album"
                name="ctv_image_rule"
                value={draft.schedule_rules.image_selection_rule}
                onChange={(event) =>
                  updateRules({
                    image_selection_rule: event.target
                      .value as 'rotate' | 'unused_first' | 'random',
                  })
                }
              >
                {IMAGE_RULES.map((rule) => (
                  <option key={rule.value} value={rule.value}>
                    {rule.label}
                  </option>
                ))}
              </Select>

              <Switch
                id="ctv_ai_caption"
                checked={draft.ai_caption_enabled}
                onChange={(checked) => update({ ai_caption_enabled: checked })}
                label="Dùng AI viết caption"
                description="Caption vẫn phải qua kiểm tra an toàn trước khi đăng."
              />

              <Switch
                id="ctv_approval"
                checked={draft.approval_required}
                onChange={(checked) => update({ approval_required: checked })}
                label="Cần duyệt trước khi đăng"
                description="Bài được xếp lịch vẫn phải được duyệt trước khi đăng thật."
              />

              <Switch
                id="ctv_anti_duplicate"
                checked={draft.anti_duplicate}
                onChange={(checked) => update({ anti_duplicate: checked })}
                label="Chống đăng trùng toàn Workspace"
                description="Chặn lặp ảnh giống hệt, ảnh gần giống và caption đã dùng."
              />

              <Input
                label="Múi giờ tính ngày"
                name="ctv_timezone"
                value={draft.timezone}
                onChange={(event) => update({ timezone: event.target.value })}
              />
            </div>
          </details>

          <div className="btn-row">
            {configured ? (
              <Button variant="primary" loading={saving} onClick={() => void save(false)}>
                Lưu cấu hình
              </Button>
            ) : null}
            <Button
              variant={configured ? 'default' : 'primary'}
              loading={saving}
              onClick={() => void save(true)}
            >
              Lưu và kích hoạt
            </Button>
          </div>
        </div>
      </Card>

      <GroupStatusList
        pipelineId={pipelineId}
        accounts={accounts.map((account) => ({ id: account.id, label: account.label }))}
        groups={groups.map((group) => ({ id: group.id, label: group.label, account_id: group.account_id }))}
        selectedGroupIds={draft.selected_group_ids}
      />

        </>
      )}

      <Modal
        open={previewOpen}
        title="Lịch đăng dự kiến"
        description={preview?.reason || 'Các bài sẽ được đăng theo cấu hình của bạn.'}
        onClose={() => setPreviewOpen(false)}
        footer={
          <Button onClick={() => setPreviewOpen(false)}>Đóng</Button>
        }
      >
        {previewLoading ? (
          <Skeleton variant="block" />
        ) : preview && preview.accounts.length > 0 ? (
          <DataTable head={['Facebook', 'Đã dùng', 'Hạn mức', 'Còn lại', 'Sắp đăng', 'Khung giờ']}>
            {preview.accounts.map((account) => (
              <tr key={account.account_id}>
                <td>{account.label}</td>
                <td>{account.used}</td>
                <td>{account.daily_limit}</td>
                <td>{account.remaining}</td>
                <td>{account.planned}</td>
                <td>
                  {account.slots.length > 0
                    ? account.slots
                        .map((slot) => `${slot.group} · ${slot.scheduled_for.replace('T', ' ').slice(0, 16)}`)
                        .join(', ')
                    : '—'}
                </td>
              </tr>
            ))}
          </DataTable>
        ) : (
          <EmptyState
            title="Chưa có lịch"
            description={preview?.reason || 'Chưa đủ điều kiện để xếp lịch bài nào.'}
          />
        )}
        {preview && preview.notes.length > 0 ? (
          <div className="stack-sm" style={{ marginTop: 'var(--sp-3)' }}>
            {preview.notes.map((note) => (
              <p className="hint" key={note}>
                {note}
              </p>
            ))}
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
