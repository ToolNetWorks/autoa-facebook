import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import { Badge, Card, DataTable, EmptyState, Skeleton, Stat } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

interface CommentJob {
  id: number;
  pipeline_id: number;
  mode: 'group' | 'feed';
  source_id: number;
  fb_post_id: string;
  account_id: number;
  comment_text: string;
  status: 'pending' | 'scheduled' | 'ready' | 'posting' | 'posted' | 'failed' | 'submitted_unverified' | 'cancelled' | 'duplicate_limit';
  scheduled_for: string;
  posted_at: string | null;
  fb_comment_id: string | null;
  error: string;
  fingerprint_id: number | null;
  idempotency_key: string;
  created_at: string;
  updated_at: string;
}

interface GroupSource {
  id: number;
  pipeline_id: number;
  fb_group_id: string;
  name: string;
  url: string;
  is_active: number;
  allow_auto_comment: number;
  created_at: string;
  updated_at: string;
}

interface FeedSource {
  id: number;
  pipeline_id: number;
  name: string;
  source_type: string;
  target_id: string;
  topics: string[];
  excluded_keywords: string[];
  excluded_authors: string[];
  allowed_domains: string[];
  is_active: number;
  created_at: string;
  updated_at: string;
}

interface AutoCommentSettings {
  pipeline_id: number;
  mode_group_enabled: number;
  mode_feed_enabled: number;
  start_hour: number;
  end_hour: number;
  active_days: number[];
  timezone: string;
  max_comments_per_account_per_day: number;
  min_interval_seconds: number;
  max_comments_per_group_per_day: number;
  max_total_comments_per_day: number;
  status: 'enabled' | 'disabled' | 'paused' | 'error';
  group_account_ids: number[];
  feed_account_ids: number[];
  brand_guidelines: string;
  group_default_rules: string;
  created_at: string;
  updated_at: string;
}

interface Stats {
  total_jobs: number;
  posted: number;
  pending: number;
  failed: number;
  duplicate_limit: number;
  fingerprints: { total: number; uses: number; max_uses: number };
  today_posted: number;
}

const DEFAULT_ACTIVE_DAYS = [0, 1, 2, 3, 4, 5, 6];

export function AutoCommentSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [settings, setSettings] = useState<AutoCommentSettings | null>(null);
  const [groupSources, setGroupSources] = useState<GroupSource[]>([]);
  const [feedSources, setFeedSources] = useState<FeedSource[]>([]);
  const [jobs, setJobs] = useState<CommentJob[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<'group' | 'feed'>('group');
  const [showAdvanced, setShowAdvanced] = useState(false);

  const pipelineId = workspace.pipelineId;

  async function load() {
    if (!pipelineId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const [settingsRes, groupRes, feedRes, jobsRes, statsRes] = await Promise.all([
        api.get<{ settings: AutoCommentSettings | null }>(`/api/pipelines/${pipelineId}/auto-comment/settings`),
        api.get<{ sources: GroupSource[] }>(`/api/pipelines/${pipelineId}/auto-comment/group-sources`),
        api.get<{ sources: FeedSource[] }>(`/api/pipelines/${pipelineId}/auto-comment/feed-sources`),
        api.get<{ jobs: CommentJob[] }>(`/api/pipelines/${pipelineId}/auto-comment/jobs?limit=50`),
        api.get<{ stats: Stats }>(`/api/pipelines/${pipelineId}/auto-comment/stats`),
      ]);
      setSettings(settingsRes.settings);
      setGroupSources(groupRes.sources);
      setFeedSources(feedRes.sources);
      setJobs(jobsRes.jobs);
      setStats(statsRes.stats);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được cấu hình bình luận', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  const accounts = workspace.accounts ?? [];

  function update(patch: Partial<AutoCommentSettings>) {
    setSettings((current) => (current ? { ...current, ...patch } : current));
  }

  async function save() {
    if (!settings || !pipelineId) return;
    setSaving(true);
    try {
      await api.put(`/api/pipelines/${pipelineId}/auto-comment/settings`, {
        mode_group_enabled: settings.mode_group_enabled,
        mode_feed_enabled: settings.mode_feed_enabled,
        start_hour: settings.start_hour,
        end_hour: settings.end_hour,
        active_days: settings.active_days,
        timezone: settings.timezone,
        max_comments_per_account_per_day: settings.max_comments_per_account_per_day,
        min_interval_seconds: settings.min_interval_seconds,
        max_comments_per_group_per_day: settings.max_comments_per_group_per_day,
        max_total_comments_per_day: settings.max_total_comments_per_day,
        status: settings.status,
        group_account_ids: settings.group_account_ids,
        feed_account_ids: settings.feed_account_ids,
        brand_guidelines: settings.brand_guidelines,
        group_default_rules: settings.group_default_rules,
      });
      push('Đã lưu cấu hình bình luận tự động cho toàn bộ Workspace', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được cấu hình', 'error');
    } finally {
      setSaving(false);
    }
  }

  // --- Group Sources ---
  async function addGroupSource(fb_group_id: string, name: string, url: string) {
    if (!pipelineId) return;
    try {
      await api.post(`/api/pipelines/${pipelineId}/auto-comment/group-sources`, {
        fb_group_id, name, url, allow_auto_comment: true,
      });
      push('Đã thêm nguồn group', 'success');
      await loadGroupSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thêm được group', 'error');
    }
  }

  async function loadGroupSources() {
    if (!pipelineId) return;
    try {
      const res = await api.get<{ sources: GroupSource[] }>(`/api/pipelines/${pipelineId}/auto-comment/group-sources`);
      setGroupSources(res.sources);
    } catch {}
  }

  async function toggleGroupSource(sourceId: number, isActive: boolean) {
    if (!pipelineId) return;
    try {
      await api.patch(`/api/pipelines/${pipelineId}/auto-comment/group-sources/${sourceId}`, { is_active: isActive ? 1 : 0 });
      await loadGroupSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được', 'error');
    }
  }

  async function deleteGroupSource(sourceId: number) {
    if (!pipelineId) return;
    try {
      await api.delete(`/api/pipelines/${pipelineId}/auto-comment/group-sources/${sourceId}`);
      push('Đã xóa group', 'success');
      await loadGroupSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được', 'error');
    }
  }

  // --- Feed Sources ---
  async function addFeedSource(data: { name: string; source_type: string; target_id: string; topics: string[]; excluded_keywords: string[]; excluded_authors: string[]; allowed_domains: string[] }) {
    if (!pipelineId) return;
    try {
      await api.post(`/api/pipelines/${pipelineId}/auto-comment/feed-sources`, data);
      push('Đã thêm nguồn feed', 'success');
      await loadFeedSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thêm được feed', 'error');
    }
  }

  async function loadFeedSources() {
    if (!pipelineId) return;
    try {
      const res = await api.get<{ sources: FeedSource[] }>(`/api/pipelines/${pipelineId}/auto-comment/feed-sources`);
      setFeedSources(res.sources);
    } catch {}
  }

  async function toggleFeedSource(sourceId: number, isActive: boolean) {
    if (!pipelineId) return;
    try {
      await api.patch(`/api/pipelines/${pipelineId}/auto-comment/feed-sources/${sourceId}`, { is_active: isActive ? 1 : 0 });
      await loadFeedSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được', 'error');
    }
  }

  async function deleteFeedSource(sourceId: number) {
    if (!pipelineId) return;
    try {
      await api.delete(`/api/pipelines/${pipelineId}/auto-comment/feed-sources/${sourceId}`);
      push('Đã xóa feed', 'success');
      await loadFeedSources();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được', 'error');
    }
  }

  // --- Jobs ---
  async function approveJob(jobId: number) {
    if (!pipelineId) return;
    try {
      await api.post(`/api/pipelines/${pipelineId}/auto-comment/jobs/${jobId}/approve`, {});
      push('Đã duyệt bình luận', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không duyệt được', 'error');
    }
  }

  async function cancelJob(jobId: number) {
    if (!pipelineId) return;
    try {
      await api.post(`/api/pipelines/${pipelineId}/auto-comment/jobs/${jobId}/cancel`, {});
      push('Đã hủy bình luận', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không hủy được', 'error');
    }
  }

  const modeLabel = { group: 'Group', feed: 'News Feed' };
  const statusLabel: Record<string, string> = {
    pending: 'Chờ',
    scheduled: 'Đã xếp lịch',
    ready: 'Sẵn sàng',
    posting: 'Đang đăng',
    posted: 'Đã đăng',
    failed: 'Thất bại',
    submitted_unverified: 'Chờ xác minh',
    cancelled: 'Đã hủy',
    duplicate_limit: 'Trùng lặp',
  };
  const statusTone: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
    pending: 'neutral',
    scheduled: 'neutral',
    ready: 'ok',
    posting: 'neutral',
    posted: 'ok',
    failed: 'bad',
    submitted_unverified: 'warn',
    cancelled: 'neutral',
    duplicate_limit: 'warn',
  };

  if (loading && !settings) {
    return (
      <div className="stack">
        <div className="section-title">
          <h2>Bình luận tự động</h2>
          <p className="hint">Workspace {workspace.pipeline?.name ?? ''} · một cấu hình cho toàn bộ Facebook.</p>
        </div>
        <div className="stack-sm">
          <Skeleton variant="block" />
          <Skeleton variant="block" />
        </div>
      </div>
    );
  }

  if (!settings) {
    return <EmptyState title="Chưa có cấu hình" description="Không tải được cấu hình bình luận tự động." />;
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Bình luận tự động</h2>
          <p className="hint">Workspace {workspace.pipeline?.name ?? ''} · một cấu hình cho toàn bộ Facebook.</p>
        </div>
        <div className="btn-row">
          <Button size="sm" onClick={load}>
            Làm mới
          </Button>
          <Button variant="primary" loading={saving} onClick={() => void save()}>
            Lưu cấu hình
          </Button>
        </div>
      </div>

      <div className="stat-row">
        <Stat label="Tổng job" value={stats?.total_jobs ?? 0} />
        <Stat label="Đã đăng" value={stats?.posted ?? 0} muted={stats?.posted === 0} />
        <Stat label="Đang chờ" value={stats?.pending ?? 0} muted={stats?.pending === 0} />
        <Stat label="Thất bại" value={stats?.failed ?? 0} muted={stats?.failed === 0} />
        <Stat label="Trùng lặp" value={stats?.duplicate_limit ?? 0} muted={stats?.duplicate_limit === 0} />
        <Stat label="Hôm nay" value={stats?.today_posted ?? 0} />
      </div>

      <div className="tab-bar" role="tablist">
        <button
          role="tab"
          aria-selected={activeTab === 'group'}
          onClick={() => setActiveTab('group')}
          className={activeTab === 'group' ? 'active' : ''}
        >
          Bình luận Group
        </button>
        <button
          role="tab"
          aria-selected={activeTab === 'feed'}
          onClick={() => setActiveTab('feed')}
          className={activeTab === 'feed' ? 'active' : ''}
        >
          Bình luận News Feed
        </button>
      </div>

      {/* Common Settings */}
      <Card title="Cấu hình chung cho toàn Pipeline" description="Một cấu hình áp dụng cho mọi Facebook trong Workspace.">
        <div className="stack">
          <Switch
            id="comment_mode_group"
            checked={Boolean(settings.mode_group_enabled)}
            onChange={(checked) => update({ mode_group_enabled: checked ? 1 : 0 })}
            label="Bật bình luận Group"
            description="Tự động bình luận vào các bài viết trong Group được chỉ định."
          />
          <Switch
            id="comment_mode_feed"
            checked={Boolean(settings.mode_feed_enabled)}
            onChange={(checked) => update({ mode_feed_enabled: checked ? 1 : 0 })}
            label="Bật bình luận News Feed"
            description="Tự động bình luận vào các bài viết trên News Feed theo chủ đề."
          />

          <Input
            label="Giờ bắt đầu (0-23)"
            name="comment_start_hour"
            type="number"
            min={0}
            max={23}
            value={settings.start_hour}
            onChange={(event) => update({ start_hour: Number(event.target.value) })}
          />
          <Input
            label="Giờ kết thúc (0-23)"
            name="comment_end_hour"
            type="number"
            min={0}
            max={23}
            value={settings.end_hour}
            onChange={(event) => update({ end_hour: Number(event.target.value) })}
          />

          <Select
            label="Ngày hoạt động"
            name="comment_active_days"
            value={settings.active_days.join(',')}
            onChange={(event) => {
              const days = event.target.value.split(',').map(Number);
              update({ active_days: days });
            }}
          >
            {DEFAULT_ACTIVE_DAYS.map((d) => (
              <option key={d} value={d}>{['T2','T3','T4','T5','T6','T7','CN'][d]}</option>
            ))}
          </Select>

          <Input
            label="Số bình luận tối đa / Facebook / ngày"
            name="comment_daily_limit"
            type="number"
            min={1}
            max={20}
            value={settings.max_comments_per_account_per_day}
            onChange={(event) => update({ max_comments_per_account_per_day: Number(event.target.value) })}
          />
          <Input
            label="Khoảng cách tối thiểu (giây)"
            name="comment_interval"
            type="number"
            min={30}
            max={3600}
            value={settings.min_interval_seconds}
            onChange={(event) => update({ min_interval_seconds: Number(event.target.value) })}
            hint="Khoảng nghĩ 5p là chuẩn"
          />
          <Input
            label="Số bình luận tối đa / Group / ngày"
            name="comment_group_limit"
            type="number"
            min={1}
            max={10}
            value={settings.max_comments_per_group_per_day}
            onChange={(event) => update({ max_comments_per_group_per_day: Number(event.target.value) })}
          />
          <Input
            label="Tổng bình luận tối đa / Pipeline / ngày (0 = không giới hạn)"
            name="comment_pipeline_limit"
            type="number"
            min={0}
            max={200}
            value={settings.max_total_comments_per_day}
            onChange={(event) => update({ max_total_comments_per_day: Number(event.target.value) })}
          />

          <Input
            label="Múi giờ"
            name="comment_timezone"
            value={settings.timezone}
            onChange={(event) => update({ timezone: event.target.value })}
          />

          <details open={showAdvanced} onToggle={(event) => setShowAdvanced((event.target as HTMLDetailsElement).open)}>
            <summary>Cài đặt nâng cao</summary>
            <div className="stack-sm">
              <Textarea
                label="Hướng dẫn thương hiệu"
                name="brand_guidelines"
                rows={4}
                value={settings.brand_guidelines}
                onChange={(event) => update({ brand_guidelines: event.target.value })}
                hint="Ví dụ: giọng văn, từ khóa cấm, phong cách viết, CTA cho phép..."
              />
              <Textarea
                label="Quy định Group mặc định"
                name="group_default_rules"
                rows={3}
                value={settings.group_default_rules}
                onChange={(event) => update({ group_default_rules: event.target.value })}
                hint="Quy định chung cho các Group không có quy định riêng."
              />
            </div>
          </details>

          <Switch
            id="comment_status"
            checked={settings.status === 'enabled'}
            onChange={(checked) => update({ status: checked ? 'enabled' : 'disabled' })}
            label="Bật hệ thống bình luận tự động"
            description="Tắt để tạm dừng toàn bộ mà không mất cấu hình."
          />

          <div className="choice-grid">
            <label className="choice">
              <input
                type="checkbox"
                name="comment_group_accounts"
                checked={settings.group_account_ids.length > 0}
                onChange={() => {
                  // Simplified: toggle all accounts
                }}
              />
              <span>Giới hạn Facebook cho Group mode</span>
            </label>
            <label className="choice">
              <input
                type="checkbox"
                name="comment_feed_accounts"
                checked={settings.feed_account_ids.length > 0}
                onChange={() => {
                  // Simplified: toggle all accounts
                }}
              />
              <span>Giới hạn Facebook cho Feed mode</span>
            </label>
          </div>

          <div className="btn-row">
            <Button variant="primary" loading={saving} onClick={() => void save()}>
              Lưu cấu hình
            </Button>
          </div>

          <p className="hint">
            Hệ thống chỉ bình luận vào bài viết có thể truy cập hợp pháp. Giới hạn 3 lần/nội dung trên toàn Pipeline.
            Facebook bị hạn chế sẽ tự động dừng.
          </p>
        </div>
      </Card>

      {/* Group Sources Tab */}
      {activeTab === 'group' && (
        <>
          <Card title="Nguồn Group" description="Các Group được phép bình luận tự động. Chỉ xét bài viết công khai hoặc nhóm bạn là thành viên.">
            <div className="stack-sm">
              <div className="form-row">
                <Input
                  label="Group ID (Facebook)"
                  name="new_group_fb_id"
                  placeholder="VD: 1234567890"
                  style={{ width: '200px' }}
                />
                <Input
                  label="Tên hiển thị"
                  name="new_group_name"
                  placeholder="Tên nhóm"
                  style={{ width: '200px' }}
                />
                <Input
                  label="URL Group"
                  name="new_group_url"
                  placeholder="https://facebook.com/groups/..."
                  style={{ width: '250px' }}
                />
                <Button onClick={() => {
                  const fb = (document.querySelector('[name="new_group_fb_id"]') as HTMLInputElement)?.value;
                  const name = (document.querySelector('[name="new_group_name"]') as HTMLInputElement)?.value;
                  const url = (document.querySelector('[name="new_group_url"]') as HTMLInputElement)?.value;
                  if (fb) addGroupSource(fb, name || fb, url || '');
                }}>
                  Thêm Group
                </Button>
              </div>
            </div>

            {groupSources.length === 0 ? (
              <EmptyState title="Chưa có Group nào" description="Thêm Group để bắt đầu bình luận tự động." />
            ) : (
              <DataTable head={['Group', 'ID', 'Trạng thái', 'Cho phép bình luận', '']}>
                {groupSources.map((src) => (
                  <tr key={src.id}>
                    <td>{src.name || src.fb_group_id}</td>
                    <td className="truncate">{src.fb_group_id}</td>
                    <td>
                      <Switch
                        checked={Boolean(src.is_active)}
                        onChange={(checked) => toggleGroupSource(src.id, checked)}
                        label="Hoạt động"
                      />
                    </td>
                    <td>
                      <Badge tone={src.allow_auto_comment ? 'ok' : 'neutral'}>
                        {src.allow_auto_comment ? 'Có' : 'Không'}
                      </Badge>
                    </td>
                    <td className="actions">
                      <Button size="sm" onClick={() => deleteGroupSource(src.id)}>
                        Xóa
                      </Button>
                    </td>
                  </tr>
                ))}
              </DataTable>
            )}
          </Card>
        </>
      )}

      {/* Feed Sources Tab */}
      {activeTab === 'feed' && (
        <>
          <Card title="Nguồn News Feed" description="Các nguồn trên News Feed được phép bình luận. Lọc theo chủ đề, từ khóa cấm, tác giả cấm.">
            <div className="stack-sm">
              <div className="form-row">
                <Input
                  label="Tên nguồn"
                  name="new_feed_name"
                  placeholder="VD: Trang tin tức công nghệ"
                  style={{ width: '200px' }}
                />
                <Select
                  name="new_feed_type"
                  style={{ width: '160px' }}
                >
                  <option value="keyword">Từ khóa</option>
                  <option value="profile">Cá nhân</option>
                  <option value="page">Fanpage</option>
                </Select>
                <Input
                  label="Target ID"
                  name="new_feed_target"
                  placeholder="ID profile/page hoặc từ khóa"
                  style={{ width: '200px' }}
                />
                <Button onClick={() => {
                  const name = (document.querySelector('[name="new_feed_name"]') as HTMLInputElement)?.value;
                  const type = (document.querySelector('[name="new_feed_type"]') as HTMLSelectElement)?.value || 'keyword';
                  const target = (document.querySelector('[name="new_feed_target"]') as HTMLInputElement)?.value;
                  if (name && target) addFeedSource({ name, source_type: type, target_id: target, topics: [], excluded_keywords: [], excluded_authors: [], allowed_domains: [] });
                }}>
                  Thêm nguồn
                </Button>
              </div>
            </div>

            {feedSources.length === 0 ? (
              <EmptyState title="Chưa có nguồn Feed nào" description="Thêm nguồn News Feed để bắt đầu." />
            ) : (
              <DataTable head={['Nguồn', 'Loại', 'Target', 'Chủ đề', 'Trạng thái', '']}>
                {feedSources.map((src) => (
                  <tr key={src.id}>
                    <td>{src.name}</td>
                    <td>
                      <Badge tone="neutral">{src.source_type}</Badge>
                    </td>
                    <td className="truncate">{src.target_id}</td>
                    <td>{src.topics.join(', ') || '—'}</td>
                    <td>
                      <Switch
                        checked={Boolean(src.is_active)}
                        onChange={(checked) => toggleFeedSource(src.id, checked)}
                        label="Hoạt động"
                      />
                    </td>
                    <td className="actions">
                      <Button size="sm" onClick={() => deleteFeedSource(src.id)}>
                        Xóa
                      </Button>
                    </td>
                  </tr>
                ))}
              </DataTable>
            )}
          </Card>
        </>
      )}

      {/* Jobs / History */}
      <Card title="Lịch sử bình luận" description="Tất cả job bình luận tự động trong Pipeline.">
        {jobs.length === 0 ? (
          <EmptyState title="Chưa có job bình luận" description="Job sẽ xuất hiện khi hệ thống tìm thấy bài viết phù hợp." />
        ) : (
          <DataTable head={['Loại', 'Bài viết', 'Facebook', 'Bình luận', 'Trạng thái', 'Lịch', '']}>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  <Badge tone={job.mode === 'group' ? 'ok' : 'neutral'}>{modeLabel[job.mode]}</Badge>
                </td>
                <td className="truncate" style={{ maxWidth: '200px' }}>
                  <a href={`https://facebook.com/${job.fb_post_id}`} target="_blank" rel="noopener noreferrer">
                    {job.fb_post_id}
                  </a>
                </td>
                <td>{accounts.find(a => a.id === job.account_id)?.label || `ID:${job.account_id}`}</td>
                <td className="truncate" style={{ maxWidth: '300px' }}>{job.comment_text}</td>
                <td>
                  <Badge tone={statusTone[job.status] || 'neutral'}>{statusLabel[job.status]}</Badge>
                </td>
                <td>{job.scheduled_for ? new Date(job.scheduled_for).toLocaleString('vi-VN') : '—'}</td>
                <td className="actions">
                  {job.status === 'submitted_unverified' && (
                    <Button size="sm" onClick={() => approveJob(job.id)}>Duyệt</Button>
                  )}
                  {(job.status === 'pending' || job.status === 'scheduled') && (
                    <Button size="sm" variant="ghost" onClick={() => cancelJob(job.id)}>Hủy</Button>
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>
    </div>
  );
}