import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import { Alert, Badge, Card, DataTable, EmptyState, Skeleton, Stat } from '@/components/ui/Surfaces';
import { ScheduleModal } from '@/features/auto-posting/ScheduleModal';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

interface AccountLimit {
  account_id: number;
  label: string;
  enabled: boolean;
  session_status: string;
  daily_limit: number | null;
  min_gap_minutes: number | null;
  group_daily_limit: number | null;
  paused: boolean;
}

interface AccountState {
  account_id: number;
  label: string;
  session_status: string;
  daily_limit: number;
  min_gap_minutes: number;
  paused: boolean;
  published: number;
  reserved: number;
  used: number;
  remaining: number;
  at_limit: boolean;
  last_post_at: string | null;
  next_allowed_at: string | null;
  eligible_groups: number;
}

interface Settings {
  enabled: boolean;
  scope: 'all' | 'selected';
  account_ids: number[];
  group_ids: number[];
  daily_limit_per_account: number;
  min_gap_minutes: number;
  pipeline_daily_limit: number;
  topic: string;
  ai_caption: boolean;
  anti_duplicate: boolean;
  require_approval: boolean;
  apply_to_new_accounts: boolean;
  timezone: string;
}

interface Dashboard {
  settings: Settings;
  accounts: AccountState[];
  blockers: { group_id: number; label: string; screening_status: string; reason: string }[];
  totals: {
    accounts: number;
    at_limit: number;
    eligible_groups: number;
    total_groups: number;
    published_today: number;
    waiting: number;
    blocked_duplicate: number;
  };
}

const SAMPLE: Partial<Settings> = {
  daily_limit_per_account: 3,
  min_gap_minutes: 60,
  pipeline_daily_limit: 0,
  ai_caption: true,
  anti_duplicate: true,
  require_approval: true,
  apply_to_new_accounts: true,
  scope: 'all',
  timezone: 'Asia/Ho_Chi_Minh',
};

export function AutoPostingSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [limits, setLimits] = useState<AccountLimit[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);

  const pipelineId = workspace.pipelineId;

  async function load() {
    if (!pipelineId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const [dashboardResponse, limitResponse] = await Promise.all([
        api.get<Dashboard>(`/api/pipelines/${pipelineId}/auto-post/dashboard`),
        api.get<{ limits: AccountLimit[] }>(`/api/pipelines/${pipelineId}/auto-post/limits`),
      ]);
      setDashboard(dashboardResponse);
      setLimits(limitResponse.limits);
      setSettings(dashboardResponse.settings);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được cấu hình', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  const groups = workspace.groups ?? [];
  const accounts = workspace.accounts ?? [];

  const accountOptions = useMemo(
    () => accounts.map((account) => ({ id: account.id, label: account.label })),
    [accounts],
  );
  const groupOptions = useMemo(
    () => groups.map((group) => ({ id: group.id, label: group.label })),
    [groups],
  );

  function update(patch: Partial<Settings>) {
    setSettings((current) => (current ? { ...current, ...patch } : current));
  }

  async function save() {
    if (!settings || !pipelineId) return;
    setSaving(true);
    try {
      await api.put(`/api/pipelines/${pipelineId}/auto-post/settings`, {
        enabled: settings.enabled,
        scope: settings.scope,
        account_ids: settings.account_ids,
        group_ids: settings.group_ids,
        daily_limit_per_account: settings.daily_limit_per_account,
        min_gap_minutes: settings.min_gap_minutes,
        pipeline_daily_limit: settings.pipeline_daily_limit,
        topic: settings.topic,
        ai_caption: settings.ai_caption,
        anti_duplicate: settings.anti_duplicate,
        require_approval: settings.require_approval,
        apply_to_new_accounts: settings.apply_to_new_accounts,
        timezone: settings.timezone,
      });
      push('Đã lưu cấu hình tự động đăng cho toàn bộ Workspace', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được cấu hình', 'error');
    } finally {
      setSaving(false);
    }
  }

  async function runNow() {
    if (!pipelineId) return;
    setRunning(true);
    try {
      const result = await api.post<{ created: unknown[]; skipped: string[] }>(
        `/api/pipelines/${pipelineId}/auto-post/distribute`,
      );
      const created = result.created.length;
      push(
        created > 0
          ? `Đã xếp lịch ${created} bài theo hạn mức từng Facebook`
          : result.skipped[0] || 'Chưa có bài nào đủ điều kiện để xếp lịch',
        created > 0 ? 'success' : 'info',
      );
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xếp lịch được', 'error');
    } finally {
      setRunning(false);
    }
  }

  async function saveLimit(accountId: number, patch: Partial<AccountLimit>) {
    if (!pipelineId) return;
    try {
      await api.put(`/api/pipelines/${pipelineId}/auto-post/limits/${accountId}`, patch);
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được hạn mức', 'error');
    }
  }

  if (loading && !dashboard) {
    return (
      <div className="stack">
        <Skeleton variant="block" />
        <Skeleton variant="block" />
      </div>
    );
  }
  if (!settings || !dashboard) {
    return <EmptyState title="Chưa có cấu hình" description="Không tải được cấu hình tự động đăng." />;
  }

  const totals = dashboard.totals;

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Tự động đăng Group</h2>
          <p className="hint">Workspace {workspace.pipeline?.name ?? ''} · cấu hình một lần cho toàn bộ Facebook.</p>
        </div>
        <div className="btn-row">
          <Button size="sm" onClick={load}>
            Làm mới
          </Button>
          <Button size="sm" onClick={() => setScheduleOpen(true)}>
            Lịch đăng
          </Button>
          <Button size="sm" variant="primary" loading={running} onClick={() => void runNow()}>
            Xếp lịch ngay
          </Button>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Facebook đang hoạt động" value={totals.accounts} />
        <Stat label="Đạt giới hạn hôm nay" value={totals.at_limit} muted={totals.at_limit === 0} />
        <Stat label="Group đủ điều kiện" value={totals.eligible_groups} muted={totals.eligible_groups === 0} />
        <Stat label="Bài đã xác nhận đăng" value={totals.published_today} />
        <Stat label="Bài đang chờ" value={totals.waiting} />
        <Stat label="Bài bị chặn trùng" value={totals.blocked_duplicate} muted={totals.blocked_duplicate === 0} />
      </div>

      {totals.eligible_groups === 0 && totals.total_groups > 0 ? (
        <Alert tone="warn">
          Chưa có group nào đủ điều kiện đăng tự động. Cần xác minh quyền đăng và hỗ trợ đúng chế độ ẩn danh
          hoặc biệt danh trước khi hệ thống xếp lịch.
        </Alert>
      ) : null}

      <Card
        title="Cấu hình chung cho toàn Pipeline"
        description="Áp dụng một lần cho mọi Facebook trong Workspace."
      >
        <div className="stack">
          <Switch
            id="auto_enabled"
            checked={settings.enabled}
            onChange={(checked) => update({ enabled: checked })}
            label="Bật tự động đăng group"
            description="Hệ thống tự chuẩn bị và xếp lịch bài cho các Facebook đủ điều kiện."
          />

          <Select
            label="Áp dụng cho"
            name="auto_scope"
            value={settings.scope}
            onChange={(event) => update({ scope: event.target.value as 'all' | 'selected' })}
          >
            <option value="all">Tất cả Facebook đang hoạt động trong Pipeline</option>
            <option value="selected">Chỉ những Facebook được chọn</option>
          </Select>

          {settings.scope === 'selected' ? (
            <div className="choice-grid">
              {accountOptions.map((account) => (
                <label className="choice" key={account.id}>
                  <input
                    type="checkbox"
                    name="auto_accounts"
                    checked={settings.account_ids.includes(account.id)}
                    onChange={(event) =>
                      update({
                        account_ids: event.target.checked
                          ? [...settings.account_ids, account.id]
                          : settings.account_ids.filter((id) => id !== account.id),
                      })
                    }
                  />
                  <span>{account.label}</span>
                </label>
              ))}
            </div>
          ) : null}

          <Input
            label="Số bài tối đa / Facebook / ngày"
            name="auto_daily_limit"
            type="number"
            min={1}
            max={50}
            value={settings.daily_limit_per_account}
            onChange={(event) => update({ daily_limit_per_account: Number(event.target.value) })}
          />

          <div className="choice-grid">
            {groupOptions.map((group) => (
              <label className="choice" key={group.id}>
                <input
                  type="checkbox"
                  name="auto_groups"
                  checked={settings.group_ids.length === 0 || settings.group_ids.includes(group.id)}
                  onChange={(event) => {
                    const all = groupOptions.map((option) => option.id);
                    const current = settings.group_ids.length === 0 ? all : settings.group_ids;
                    update({
                      group_ids: event.target.checked
                        ? [...new Set([...current, group.id])]
                        : current.filter((id) => id !== group.id),
                    });
                  }}
                />
                <span>{group.label}</span>
              </label>
            ))}
          </div>
          <p className="hint">
            Không chọn group nào nghĩa là dùng toàn bộ group của Workspace. Group chưa xác minh hoặc bị hạn chế
            sẽ không được xếp lịch.
          </p>

          <Textarea
            label="Chủ đề / caption mẫu"
            name="auto_topic"
            rows={2}
            value={settings.topic}
            onChange={(event) => update({ topic: event.target.value })}
            hint="AI dùng chủ đề này để chọn ảnh phù hợp và viết caption."
          />

          <Switch
            id="auto_ai_caption"
            checked={settings.ai_caption}
            onChange={(checked) => update({ ai_caption: checked })}
            label="Bật AI Caption"
            description="AI viết caption theo ảnh và chủ đề, sau đó caption vẫn phải qua kiểm tra an toàn."
          />

          <Switch
            id="auto_anti_duplicate"
            checked={settings.anti_duplicate}
            onChange={(checked) => update({ anti_duplicate: checked })}
            label="Bật chống đăng trùng toàn Pipeline"
            description="Chặn lặp ảnh giống hệt, ảnh gần giống và caption đã dùng."
          />

          <Switch
            id="auto_approval"
            checked={settings.require_approval}
            onChange={(checked) => update({ require_approval: checked })}
            label="Cần duyệt trước khi đăng"
            description="Bài được xếp lịch vẫn phải được duyệt trước khi đăng thật."
          />

          <Switch
            id="auto_new_accounts"
            checked={settings.apply_to_new_accounts}
            onChange={(checked) => update({ apply_to_new_accounts: checked })}
            label="Áp dụng cho Facebook mới"
            description="Tài khoản thêm vào Pipeline sau này tự dùng cấu hình này."
          />

          <details open={advanced} onToggle={(event) => setAdvanced((event.target as HTMLDetailsElement).open)}>
            <summary>Cài đặt nâng cao</summary>
            <div className="stack-sm">
              <Input
                label="Khoảng cách tối thiểu giữa hai bài (phút)"
                name="auto_gap"
                type="number"
                min={0}
                max={1440}
                value={settings.min_gap_minutes}
                onChange={(event) => update({ min_gap_minutes: Number(event.target.value) })}
              />
              <Input
                label="Hạn mức tổng của Pipeline mỗi ngày (0 = không giới hạn)"
                name="auto_pipeline_limit"
                type="number"
                min={0}
                max={500}
                value={settings.pipeline_daily_limit}
                onChange={(event) => update({ pipeline_daily_limit: Number(event.target.value) })}
              />
              <Input
                label="Múi giờ tính ngày"
                name="auto_timezone"
                value={settings.timezone}
                onChange={(event) => update({ timezone: event.target.value })}
              />
            </div>
          </details>

          <div className="btn-row">
            <Button variant="primary" loading={saving} onClick={() => void save()}>
              Lưu cấu hình
            </Button>
            <Button
              onClick={() => {
                update(SAMPLE as Partial<Settings>);
                push('Đã điền cấu hình mẫu, nhớ lưu để áp dụng', 'info');
              }}
            >
              Sao chép cấu hình mẫu
            </Button>
          </div>

          <p className="hint">
            Quy tắc hoạt động: hệ thống chọn ảnh từ Album, tạo caption, kiểm tra an toàn rồi xếp lịch theo hạn mức
            từng Facebook. Group chưa xác minh hoặc bị hạn chế sẽ không được tự động đăng.
          </p>
        </div>
      </Card>

      <Card title="Hạn mức theo từng Facebook" description="Số bài đã dùng hôm nay và phần còn lại.">
        {limits.length === 0 ? (
          <EmptyState title="Chưa có Facebook" description="Thêm tài khoản Facebook vào Workspace trước." />
        ) : (
          <DataTable head={['Facebook', 'Phạm vi', 'Phiên', 'Đã dùng', 'Hạn mức', 'Còn lại', 'Trạng thái', 'Tạm dừng']}>
            {limits.map((limit) => {
              const account = dashboard.accounts.find((item) => item.account_id === limit.account_id);
              const inScope = settings.scope === 'all'
                || settings.account_ids.includes(limit.account_id);
              const used = account?.used ?? 0;
              const dailyLimit = limit.daily_limit ?? account?.daily_limit ?? settings.daily_limit_per_account;
              const remaining = Math.max(0, dailyLimit - used);
              return (
                <tr key={limit.account_id}>
                  <td>{limit.label}</td>
                  <td>
                    {inScope ? (
                      <Badge tone="ok">Có</Badge>
                    ) : (
                      <Badge tone="neutral">Không</Badge>
                    )}
                  </td>
                  <td>
                    <Badge tone={limit.session_status === 'connected' ? 'ok' : 'neutral'}>
                      {limit.session_status === 'connected' ? 'Đã đăng nhập' : 'Chưa kiểm tra'}
                    </Badge>
                  </td>
                  <td>
                    {used}
                    {(account?.reserved ?? 0) > 0 ? ` (${account?.reserved} chờ xác minh)` : ''}
                  </td>
                  <td>
                    <input
                      className="limit-input"
                      type="number"
                      min={1}
                      max={50}
                      name={`limit-${limit.account_id}`}
                      value={limit.daily_limit ?? ''}
                      placeholder={String(dailyLimit)}
                      onChange={(event) =>
                        setLimits((current) =>
                          current.map((item) =>
                            item.account_id === limit.account_id
                              ? {
                                  ...item,
                                  daily_limit: event.target.value === '' ? null : Number(event.target.value),
                                }
                              : item,
                          ),
                        )
                      }
                      onBlur={(event) =>
                        void saveLimit(limit.account_id, {
                          daily_limit: event.target.value === '' ? null : Number(event.target.value),
                        })
                      }
                    />
                  </td>
                  <td>{remaining}</td>
                  <td>
                    {limit.paused ? (
                      <Badge tone="warn">Tạm dừng</Badge>
                    ) : used >= dailyLimit ? (
                      <Badge tone="warn">Đủ hạn mức</Badge>
                    ) : (
                      <Badge tone="ok">Còn hạn mức</Badge>
                    )}
                  </td>
                  <td>
                    <Button
                      size="sm"
                      onClick={() => void saveLimit(limit.account_id, { paused: !limit.paused })}
                    >
                      {limit.paused ? 'Tiếp tục' : 'Tạm dừng'}
                    </Button>
                  </td>
                </tr>
              );
            })}
          </DataTable>
        )}
      </Card>

      <ScheduleModal
        pipelineId={pipelineId}
        open={scheduleOpen}
        onClose={() => setScheduleOpen(false)}
        onChanged={() => void load()}
      />

      {dashboard.blockers.length > 0 ? (
        <Card title="Group chưa đủ điều kiện" description="Lý do để mỗi group không được xếp lịch.">
          <DataTable head={['Group', 'Trạng thái', 'Lý do']}>
            {dashboard.blockers.map((blocker) => (
              <tr key={blocker.group_id}>
                <td>{blocker.label}</td>
                <td>
                  <Badge tone="neutral">{blocker.screening_status}</Badge>
                </td>
                <td>{blocker.reason}</td>
              </tr>
            ))}
          </DataTable>
        </Card>
      ) : null}
    </div>
  );
}
