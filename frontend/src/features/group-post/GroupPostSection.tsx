import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '@/services/apiClient';
import { useAuth } from '@/app/AuthProvider';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { ConfirmDialog } from '@/components/ui/Modal';
import { Alert, Card, DataTable, Skeleton } from '@/components/ui/Surfaces';
import { GroupSelector } from '@/features/group-post/GroupSelector';
import type { ScheduledPostRow, PostJobRow } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

type TabKey = 'create' | 'scheduled';

interface SkippedGroup {
  group_id: number;
  label?: string;
  reason: string;
}

interface CampaignResult {
  jobs_created: number;
  jobs: { job_id: number; group_id: number; label: string; posting_mode: string }[];
  skipped: SkippedGroup[];
  deferred: SkippedGroup[];
  duplicates: SkippedGroup[];
  selected: number;
  limit: number;
  published: boolean;
}

const STATE_FILTERS = [
  { value: 'all', label: 'Tất cả' },
  { value: 'scheduled', label: 'Chờ đăng' },
  { value: 'needs_review', label: 'Cần xem lại' },
  { value: 'processing', label: 'Đang xử lý' },
  { value: 'posted', label: 'Đã đăng' },
  { value: 'failed', label: 'Thất bại' },
  { value: 'cancelled', label: 'Đã hủy' },
  { value: 'skipped', label: 'Đã bỏ qua' },
] as const;

const STATUS_LABELS: Record<string, string> = {
  queued: 'Chờ đăng',
  pending_approval: 'Chờ duyệt',
  running: 'Đang xử lý',
  submitted_unverified: 'Đã gửi (chưa xác minh)',
  posted: 'Đã đăng',
  failed: 'Thất bại',
  cancelled: 'Đã hủy',
  skipped: 'Đã bỏ qua',
  needs_review: 'Cần xem lại',
  scheduled: 'Chờ đăng',
  processing: 'Đang xử lý',
};

function stateLabel(state: string): string {
  return STATUS_LABELS[state] ?? state;
}

function postStatus(post: ScheduledPostRow): string {
  if (post.pending_count > 0 && post.posted_count === 0) return 'scheduled';
  if (post.failed_count > 0 && post.posted_count === 0) return 'failed';
  if (post.posted_count > 0 && post.failed_count === 0 && post.pending_count === 0) return 'posted';
  if (post.posted_count > 0 && post.pending_count > 0) return 'processing';
  if (post.posted_count > 0 && post.failed_count > 0) return 'failed';
  return 'needs_review';
}

export function GroupPostSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const { session } = useAuth();
  const [searchParams] = useSearchParams();
  const requestedPost = Number(searchParams.get('post_id'));
  const [activeTab, setActiveTab] = useState<TabKey>('create');
  const [scheduledPosts, setScheduledPosts] = useState<ScheduledPostRow[]>([]);
  const [postsLoading, setPostsLoading] = useState(false);
  const [postsError, setPostsError] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [expandedPost, setExpandedPost] = useState<number | null>(null);
  const [jobsMap, setJobsMap] = useState<Record<number, PostJobRow[]>>({});
  const [jobsLoading, setJobsLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [cancelTarget, setCancelTarget] = useState<{ postId: number; jobId: number } | null>(null);

  const pipelineId = workspace.pipelineId;

  useEffect(() => {
    if (!pipelineId) return;
    loadPosts();
  }, [pipelineId, filter, search, page]);

  useEffect(() => {
    if (!Number.isSafeInteger(requestedPost) || requestedPost <= 0) return;
    setActiveTab('scheduled');
    setExpandedPost(requestedPost);
    setJobsMap({});
    // The existing endpoint verifies ownership; an invalid/foreign ID is an error.
    void loadJobs(requestedPost);
  }, [requestedPost, pipelineId]);

  async function loadPosts() {
    setPostsLoading(true);
    setPostsError(null);
    try {
      const params = new URLSearchParams({
        limit: String(page * 50),
      });
      if (filter !== 'all') params.set('state', filter);
      if (search.trim()) params.set('search', search.trim());
      const data = await api.get<ScheduledPostRow[]>(
        `/api/pipelines/${pipelineId}/posts?${params.toString()}`,
      );
      setScheduledPosts(data);
    } catch (failure) {
      setPostsError(failure instanceof Error ? failure.message : 'Không tải được danh sách');
    } finally {
      setPostsLoading(false);
    }
  }

  async function loadJobs(postId: number) {
    setJobsLoading(true);
    try {
      const jobs = await api.get<PostJobRow[]>(`/api/posts/${postId}/jobs`);
      setJobsMap((prev) => ({ ...prev, [postId]: jobs }));
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được chi tiết job', 'error');
    } finally {
      setJobsLoading(false);
    }
  }

  async function cancelJob(_postId: number, jobId: number) {
    try {
      await api.patch<{ ok: boolean }>(`/api/jobs/${jobId}/cancel`);
      push('Đã hủy bài', 'success');
      setExpandedPost(null);
      loadPosts();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thể hủy', 'error');
    } finally {
      setCancelTarget(null);
    }
  }

  function handleSearchSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPage(1);
    loadPosts();
  }

  const hasPermission = session?.permissions.includes('group_posts.publish') ?? false;

  return (
    <div className="stack">
      <div className="section-title">
        <h2>Tạo bài group</h2>
        <p className="hint">
          Một bài, một ảnh, mỗi group một lịch riêng với chế độ đăng của riêng nó.
        </p>
      </div>

      <div className="tabs">
        <button
          type="button"
          className={activeTab === 'create' ? 'tab active' : 'tab'}
          onClick={() => setActiveTab('create')}
        >
          Tạo bài
        </button>
        <button
          type="button"
          className={activeTab === 'scheduled' ? 'tab active' : 'tab'}
          onClick={() => {
            setActiveTab('scheduled');
            loadPosts();
          }}
        >
          Bài đã lên lịch
        </button>
      </div>

      {activeTab === 'create' && (
        <CreatePostForm workspace={workspace} onPosted={() => {
          setActiveTab('scheduled');
          loadPosts();
        }} />
      )}

      {activeTab === 'scheduled' && (
        <ScheduledPostsList
          posts={scheduledPosts}
          loading={postsLoading}
          error={postsError}
          filter={filter}
          onFilterChange={setFilter}
          search={search}
          onSearchChange={setSearch}
          onSearchSubmit={handleSearchSubmit}
          expandedPost={expandedPost}
          onToggleExpand={setExpandedPost}
          jobsMap={jobsMap}
          jobsLoading={jobsLoading}
          onJobsLoaded={loadJobs}
          onCancel={cancelJob}
          canManage={hasPermission}
        />
      )}

      <ConfirmDialog
        open={cancelTarget !== null}
        title="Xác nhận hủy bài"
        message="Bài này sẽ được hủy không thể khôi phục. Bạn có chắc muốn tiếp tục?"
        onConfirm={() => {
          if (cancelTarget) {
            void cancelJob(cancelTarget.postId, cancelTarget.jobId);
          }
        }}
        onCancel={() => setCancelTarget(null)}
      />
    </div>
  );
}

function CreatePostForm({ workspace, onPosted }: { workspace: WorkspaceData; onPosted: () => void }) {
  const { push } = useToast();
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [body, setBody] = useState('');
  const [image, setImage] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(true);
  const [maxGroups, setMaxGroups] = useState(200);
  const [result, setResult] = useState<CampaignResult | null>(null);
  const [scheduledAt, setScheduledAt] = useState(() => {
    const when = new Date(Date.now() + 15 * 60 * 1000);
    const offset = when.getTimezoneOffset();
    return new Date(when.getTime() - offset * 60000).toISOString().slice(0, 16);
  });

  useEffect(() => {
    workspace.reloadGroups();
  }, []);

  useEffect(() => {
    if (!workspace.pipelineId) {
      setChecking(false);
      return;
    }
    api
      .get<{ max_groups: number }>(
        `/api/pipelines/${workspace.pipelineId}/group-post/limits`,
      )
      .then((response) => {
        setMaxGroups(response.max_groups);
      })
      .catch(() => undefined)
      .finally(() => {
        setChecking(false);
      });
  }, [workspace.pipelineId]);

  const groups = useMemo(
    () => workspace.groups.filter((group) => group.enabled && group.approved_target),
    [workspace.groups],
  );

  async function submit() {
    if (groupIds.length === 0) {
      push('Chọn ít nhất một group', 'warn');
      return;
    }
    if (!image) {
      push('Chọn một ảnh', 'warn');
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const preflight = await api.post<{ ready: unknown[]; skipped: SkippedGroup[] }>(
        `/api/pipelines/${workspace.pipelineId}/group-posts/preflight`,
        {
          group_ids: groupIds,
          body: body.trim() || ' ',
          media: ['uploads/preflight.jpg'],
          scheduled_at: new Date(scheduledAt).toISOString(),
        },
      );
      if (preflight.ready.length === 0) {
        push('Không group nào đủ điều kiện để lên lịch', 'warn');
        return;
      }
      const form = new FormData();
      form.append('file', image);
      const uploaded = await api.upload<{ path: string }>(
        `/api/uploads?pipeline_id=${workspace.pipelineId}`,
        form,
      );
      const response = await api.post<CampaignResult>(
        `/api/pipelines/${workspace.pipelineId}/group-posts`,
        {
          group_ids: groupIds,
          body: body.trim(),
          media: [uploaded.path],
          scheduled_at: new Date(scheduledAt).toISOString(),
          anonymous_only: 1,
        },
      );
      push(
        response.jobs_created > 0
          ? `Đã lên lịch ${response.jobs_created} bài`
          : 'Chưa tạo được bài nào',
        response.jobs_created > 0 ? 'success' : 'warn',
      );
      onPosted();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lên lịch được', 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {checking ? (
        <Skeleton variant="block" />
      ) : (
        <GroupSelector
          groups={groups}
          selected={groupIds}
          onChange={setGroupIds}
          maxGroups={maxGroups}
          busy={busy}
        />
      )}

      <Card title="2. Ảnh và caption">
        <div className="stack">
          <Input
            label="Ảnh (1 ảnh)"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(event) => setImage(event.target.files?.[0] ?? null)}
            hint="Ảnh dùng chung cho mọi group trong chiến dịch này."
          />
          <Textarea
            label="Caption"
            name="post_body"
            rows={5}
            value={body}
            onChange={(event) => setBody(event.target.value)}
            placeholder="Nội dung bài đăng"
          />
          <Input
            label="Lịch đăng"
            type="datetime-local"
            name="post_scheduled"
            value={scheduledAt}
            onChange={(event) => setScheduledAt(event.target.value)}
          />
          <Button variant="primary" loading={busy} onClick={() => void submit()}>
            Lên lịch đăng bài
          </Button>
        </div>
      </Card>

      {result && result.jobs_created > 0 ? (
        <Alert tone="info">
          Đã lên lịch {result.jobs_created} bài. Xem tại tab <strong>Bài đã lên lịch</strong>.
        </Alert>
      ) : null}
    </>
  );
}

function ScheduledPostsList({
  posts,
  loading,
  error,
  filter,
  onFilterChange,
  search,
  onSearchChange,
  onSearchSubmit,
  expandedPost,
  onToggleExpand,
  jobsMap,
  jobsLoading,
  onJobsLoaded,
  onCancel,
  canManage,
}: {
  posts: ScheduledPostRow[];
  loading: boolean;
  error: string | null;
  filter: string;
  onFilterChange: (value: string) => void;
  search: string;
  onSearchChange: (value: string) => void;
  onSearchSubmit: (e: React.FormEvent) => void;
  expandedPost: number | null;
  onToggleExpand: (id: number | null) => void;
  jobsMap: Record<number, PostJobRow[]>;
  jobsLoading: boolean;
  onJobsLoaded: (postId: number) => void;
  onCancel: (postId: number, jobId: number) => void;
  canManage: boolean;
}) {
  return (
    <div className="stack">
      <div className="filters">
        <div className="filter-group">
          {STATE_FILTERS.map((f) => (
            <button
              key={f.value}
              type="button"
              className={filter === f.value ? 'filter active' : 'filter'}
              onClick={() => onFilterChange(f.value)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <form onSubmit={onSearchSubmit} className="search-row">
          <Input
            label=""
            name="search"
            placeholder="Tìm theo nội dung, group, tài khoản..."
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            className="search-input"
          />
          <Button type="submit" variant="ghost" size="sm">
            Tìm
          </Button>
        </form>
      </div>

      {loading ? (
        <Skeleton variant="block" />
      ) : error ? (
        <Alert tone="bad">{error}</Alert>
      ) : posts.length === 0 ? (
        <Alert tone="info">Chưa có bài nào được lên lịch.</Alert>
      ) : (
        <DataTable head={['Nội dung', 'Trạng thái', 'Lịch đăng', 'Group/Job']}>
          {posts.map((post) => {
            const status = postStatus(post);
            const expanded = expandedPost === post.id;
            const identityLabel = post.anonymous_only ? 'Ẩn danh' : 'Biệt danh';
            return (
              <tr key={post.id}>
                <td>
                  <div className="post-preview">
                    <span className="post-body">{post.body}</span>
                    <span className={`badge ${status === 'posted' ? 'ok' : status === 'failed' || status === 'cancelled' ? 'bad' : status === 'needs_review' ? 'warn' : 'info'}`} aria-label={stateLabel(status)}>
                      {stateLabel(status)}
                    </span>
                  </div>
                </td>
                <td>
                  {post.first_scheduled
                    ? new Date(post.first_scheduled).toLocaleString('vi-VN')
                    : 'Chưa lên lịch'}
                </td>
                <td>
                  {post.job_count} group, {post.pending_count} chờ, {post.posted_count} đã đăng
                  {post.failed_count > 0 ? `, ${post.failed_count} lỗi` : ''}
                  <br />
                  <small className="muted">{identityLabel}</small>
                </td>
                <td>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => {
                      if (expanded) {
                        onToggleExpand(null);
                      } else {
                        onToggleExpand(post.id);
                        onJobsLoaded(post.id);
                      }
                    }}
                  >
                    {expanded ? 'Ẩn' : 'Xem'}
                  </button>
                </td>
              </tr>
            );
          })}
        </DataTable>
      )}

      {expandedPost && (
        <ExpandedJobs
          postId={expandedPost}
          jobs={jobsMap[expandedPost] ?? []}
          loading={jobsLoading}
          onCancel={onCancel}
          canManage={canManage}
        />
      )}
    </div>
  );
}

function ExpandedJobs({
  postId,
  jobs,
  loading,
  onCancel,
  canManage,
}: {
  postId: number;
  jobs: PostJobRow[];
  loading: boolean;
  onCancel: (postId: number, jobId: number) => void;
  canManage: boolean;
}) {
  if (loading) {
    return <Skeleton variant="block" />;
  }

  return (
    <Card title={`Chi tiết job (${jobs.length} group)`}>
      <DataTable head={['Group', 'Tài khoản', 'Trạng thái', 'Chế độ', 'Lịch', 'Thao tác']}>
        {jobs.map((job) => (
          <tr key={job.id}>
            <td>
              <a href={job.group_url} target="_blank" rel="noopener" className="link">
                {job.group_name}
              </a>
            </td>
            <td>{job.account_name}</td>
            <td>{stateLabel(job.state)}</td>
            <td>
              {job.requested_posting_mode === 'anonymous' ? 'Ẩn danh' : job.requested_nickname || 'Biệt danh'}
            </td>
            <td>
              {job.scheduled_at
                ? new Date(job.scheduled_at).toLocaleString('vi-VN')
                : '—'}
            </td>
            <td>
              {job.actual_posting_identity && job.state === 'posted' ? (
                <a href={job.permalink} target="_blank" rel="noopener" className="link">
                  Link bài
                </a>
              ) : null}
              {job.state === 'queued' && canManage ? (
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => onCancel(postId, job.id)}
                >
                  Hủy
                </button>
              ) : null}
              {job.state === 'failed' && job.failure_reason ? (
                <span className="muted" title={job.failure_reason}>
                  {job.failure_reason.slice(0, 60)}
                </span>
              ) : null}
              {job.state === 'submitted_unverified' ? (
                <span className="warn">Chưa xác minh kết quả đăng</span>
              ) : null}
            </td>
          </tr>
        ))}
      </DataTable>
    </Card>
  );
}
