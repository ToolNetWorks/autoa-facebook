import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Select } from '@/components/ui/Field';
import { Alert, Badge, Card, DataTable, EmptyState, Skeleton } from '@/components/ui/Surfaces';
import { Kpi, KpiRow, TrendChart, formatNumber, measured } from '@/features/analytics/AnalyticsWidgets';
import { AppShell } from '@/app/layout/AppShell';
import { ADMIN_NAV } from '@/app/navigation';
import { useAuth } from '@/app/AuthProvider';
import type {
  AnalyticsCollaboratorRow,
  AnalyticsKpis,
  AnalyticsPost,
  AnalyticsPoint,
  AnalyticsSources,
} from '@/types/api';


/** The frame of a report: the same menu as the rest of the administrator pages. */
function AnalyticsShell({ title, onBack, children }: { title: string; onBack: () => void; children: ReactNode }) {
  const navigate = useNavigate();
  const { signOut } = useAuth();
  return (
    <AppShell
      nav={[{ heading: 'Quản trị', items: ADMIN_NAV }]}
      activeId="dashboard"
      onNavigate={(id) => {
        if (id === 'dashboard') navigate('/');
        else navigate(`/workspace/${id}`);
      }}
      title={title}
      actions={
        <>
          <Button size="sm" onClick={onBack}>
            Về Dashboard
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void signOut()}>
            Đăng xuất
          </Button>
        </>
      }
    >
      {children}
    </AppShell>
  );
}

const RANGES = [
  { id: '7', label: '7 ngày' },
  { id: '30', label: '30 ngày' },
  { id: '90', label: '90 ngày' },
  { id: '365', label: '1 năm' },
];

const STATUSES = [
  { id: '', label: 'Mọi trạng thái' },
  { id: 'posted', label: 'Đã đăng' },
  { id: 'pending_approval', label: 'Chờ duyệt' },
  { id: 'submitted_unverified', label: 'Chờ xác minh' },
  { id: 'failed', label: 'Bị từ chối' },
];

const CONTENT_TYPES = [
  { id: '', label: 'Mọi nội dung' },
  { id: 'group_post', label: 'Bài Group' },
  { id: 'reel', label: 'Reel' },
];

interface ReportPayload {
  pipeline: { id: number; name: string; description: string; enabled: boolean };
  kpis: AnalyticsKpis;
  timeseries: AnalyticsPoint[];
  top_posts: AnalyticsPost[];
  collaborators: AnalyticsCollaboratorRow[];
  accounts: { id: number; label: string }[];
  sources: AnalyticsSources;
}

export function PipelineReportPage() {
  const { pipelineId } = useParams();
  const navigate = useNavigate();
  const { push } = useToast();
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState('30');
  const [accountId, setAccountId] = useState('');
  const [ctvId, setCtvId] = useState('');
  const [contentType, setContentType] = useState('');
  const [status, setStatus] = useState('');

  const query = useMemo(() => {
    const parts = [`days=${days}`];
    if (accountId) parts.push(`account_id=${accountId}`);
    if (ctvId) parts.push(`ctv_id=${ctvId}`);
    if (contentType) parts.push(`content_type=${contentType}`);
    if (status) parts.push(`status=${status}`);
    return parts.join('&');
  }, [days, accountId, ctvId, contentType, status]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api
      .get<ReportPayload>(`/api/admin/analytics/pipelines/${pipelineId}?${query}`)
      .then((result) => {
        if (active) setReport(result);
      })
      .catch((failure) => {
        if (!active) return;
        push(failure instanceof Error ? failure.message : 'Không tải được báo cáo', 'error');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [pipelineId, query, push]);

  if (loading && !report) {
    return (
      <div className="stack">
        <Skeleton variant="block" />
        <Skeleton variant="block" />
      </div>
    );
  }
  if (!report) {
    return (
      <EmptyState
        title="Chưa có báo cáo"
        description="Không tải được số liệu của Workspace này."
        action={<Button onClick={() => navigate('/')}>Về Dashboard</Button>}
      />
    );
  }

  const { kpis, pipeline } = report;

  return (
    <AnalyticsShell title={pipeline.name} onBack={() => navigate('/')}>
      <div className="stack">
        <div className="section-title">
          <div>
            <h2>{pipeline.name}</h2>
          <p className="hint">{pipeline.description || 'Báo cáo hiệu suất của Workspace.'}</p>
        </div>
        <div className="btn-row">
          <Button size="sm" onClick={() => navigate('/')}>
            Về Dashboard
          </Button>
        </div>
      </div>

      <div className="filter-bar">
        <Select label="Khoảng thời gian" name="analytics_days" value={days} onChange={(event) => setDays(event.target.value)}>
          {RANGES.map((range) => (
            <option key={range.id} value={range.id}>
              {range.label}
            </option>
          ))}
        </Select>
        <Select label="Tài khoản Facebook" name="analytics_account" value={accountId} onChange={(event) => setAccountId(event.target.value)}>
          <option value="">Tất cả tài khoản</option>
          {report.accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.label}
            </option>
          ))}
        </Select>
        <Select label="CTV" name="analytics_ctv" value={ctvId} onChange={(event) => setCtvId(event.target.value)}>
          <option value="">Tất cả CTV</option>
          {report.collaborators.map((member) => (
            <option key={member.user_id} value={member.user_id}>
              {member.username}
            </option>
          ))}
        </Select>
        <Select label="Loại nội dung" name="analytics_content" value={contentType} onChange={(event) => setContentType(event.target.value)}>
          {CONTENT_TYPES.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </Select>
        <Select label="Trạng thái" name="analytics_status" value={status} onChange={(event) => setStatus(event.target.value)}>
          {STATUSES.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </Select>
      </div>

      <KpiRow kpis={kpis} />

      <TrendChart points={report.timeseries} />

      <Card title="Hiệu suất từng CTV" description="Bài đã đăng và tương tác thực sự đo được.">
        {report.collaborators.length === 0 ? (
          <EmptyState title="Chưa có CTV" description="Workspace này chưa có CTV nào hoạt động." />
        ) : (
          <DataTable head={['CTV', 'Đã tạo', 'Đã đăng', 'Chờ duyệt', 'Tương tác', 'Tỷ lệ đăng', 'Hoạt động gần nhất']}>
            {report.collaborators.map((member) => (
              <tr key={member.user_id}>
                <td>
                  <Link className="link" to={`/analytics/collaborator/${member.user_id}`}>
                    {member.username}
                  </Link>
                </td>
                <td>{member.posts_created}</td>
                <td>{member.posts_published}</td>
                <td>{member.posts_waiting}</td>
                <td>{measured(member.engagement).text}</td>
                <td>{member.posts_created ? `${member.posts_published * 100 / member.posts_created >= 0 ? Math.round((member.posts_published / member.posts_created) * 100) : 0}%` : '—'}</td>
                <td>{member.last_activity ? new Date(member.last_activity).toLocaleString('vi-VN') : '—'}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Card title="Bài nổi bật" description="Xếp theo tương tác đo được.">
        {report.top_posts.length === 0 ? (
          <EmptyState title="Chưa có bài" description="Workspace này chưa có bài nào trong khoảng thời gian đã chọn." />
        ) : (
          <DataTable head={['Bài', 'Group / Reel', 'Tài khoản', 'Ngày', 'Thích', 'Bình luận', 'Chia sẻ', 'Lượt xem', 'Trạng thái']}>
            {report.top_posts.map((post) => (
              <tr key={`${post.content_type}-${post.post_id}`}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '260px' }}>
                    {post.permalink ? (
                      <a className="link" href={post.permalink} target="_blank" rel="noreferrer noopener">
                        {post.title}
                      </a>
                    ) : (
                      post.title
                    )}
                  </div>
                </td>
                <td>{post.group_label ?? 'Reel'}</td>
                <td>{post.account_label ?? '—'}</td>
                <td>{post.published_at ? new Date(post.published_at).toLocaleDateString('vi-VN') : '—'}</td>
                <td>{measured(post.reactions).text}</td>
                <td>{measured(post.comments).text}</td>
                <td>{measured(post.shares).text}</td>
                <td>{measured(post.views).text}</td>
                <td>
                  {post.removed ? (
                    <Badge tone="bad">Đã bị gỡ</Badge>
                  ) : post.measured ? (
                    <Badge tone="ok">Đã đo</Badge>
                  ) : (
                    <Badge tone="neutral">Chưa có dữ liệu</Badge>
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      {!report.sources.graph_api && !report.sources.facebook_session ? (
        <Alert tone="warn">
          Chưa bật nguồn đo chỉ số. Số bài đã đăng vẫn chính xác, còn tương tác sẽ hiện "Chưa có dữ liệu".
        </Alert>
      ) : null}
      </div>
    </AnalyticsShell>
  );
}

export function CollaboratorReportPage() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const { push } = useToast();
  const [report, setReport] = useState<{
    collaborator: { user_id: number; username: string; status: string; pipeline_id: number | null; pipeline_name: string | null };
    kpis: AnalyticsKpis;
    timeseries: AnalyticsPoint[];
    top_posts: AnalyticsPost[];
  } | null>(null);
  const [days, setDays] = useState('30');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api
      .get<typeof report>(`/api/admin/analytics/collaborators/${userId}?days=${days}`)
      .then((result) => {
        if (active) setReport(result);
      })
      .catch((failure) => {
        if (!active) return;
        push(failure instanceof Error ? failure.message : 'Không tải được báo cáo', 'error');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [userId, days, push]);

  if (loading && !report) {
    return <Skeleton variant="block" />;
  }
  if (!report) {
    return <EmptyState title="Chưa có báo cáo" description="Không tải được số liệu của CTV này." />;
  }

  const { collaborator, kpis } = report;

  return (
    <AnalyticsShell title={collaborator.username} onBack={() => navigate('/')}>
      <div className="stack">
        <div className="section-title">
          <div>
            <h2>{collaborator.username}</h2>
          <p className="hint">
            Workspace: {collaborator.pipeline_name ?? '—'} · Trạng thái: {collaborator.status === 'suspended' ? 'Đã khóa' : 'Hoạt động'}
          </p>
        </div>
        <div className="btn-row">
          {collaborator.pipeline_id ? (
            <Button size="sm" onClick={() => navigate(`/analytics/pipeline/${collaborator.pipeline_id}`)}>
              Về Workspace
            </Button>
          ) : null}
        </div>
      </div>

      <div className="filter-bar">
        <Select label="Khoảng thời gian" name="analytics_days" value={days} onChange={(event) => setDays(event.target.value)}>
          {RANGES.map((range) => (
            <option key={range.id} value={range.id}>
              {range.label}
            </option>
          ))}
        </Select>
      </div>

      <KpiRow kpis={kpis} />

      <TrendChart points={report.timeseries} />

      <Card title="Bài của CTV" description="Tương tác chỉ hiện khi đã đo được.">
        {report.top_posts.length === 0 ? (
          <EmptyState title="Chưa có bài" description="CTV này chưa tạo bài nào trong khoảng thời gian đã chọn." />
        ) : (
          <DataTable head={['Bài', 'Group / Reel', 'Ngày', 'Thích', 'Bình luận', 'Chia sẻ', 'Lượt xem', 'Trạng thái']}>
            {report.top_posts.map((post) => (
              <tr key={`${post.content_type}-${post.post_id}`}>
                <td>
                  <div className="cell-title truncate" style={{ maxWidth: '280px' }}>
                    {post.title}
                  </div>
                </td>
                <td>{post.group_label ?? 'Reel'}</td>
                <td>{post.published_at ? new Date(post.published_at).toLocaleDateString('vi-VN') : '—'}</td>
                <td>{measured(post.reactions).text}</td>
                <td>{measured(post.comments).text}</td>
                <td>{measured(post.shares).text}</td>
                <td>{measured(post.views).text}</td>
                <td>
                  {post.removed ? (
                    <Badge tone="bad">Đã bị gỡ</Badge>
                  ) : post.measured ? (
                    <Badge tone="ok">Đã đo</Badge>
                  ) : (
                    <Badge tone="neutral">Chưa có dữ liệu</Badge>
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <Kpi label="Tương tác đo được" value={kpis.engagement} hint={`${formatNumber(kpis.measured_posts)} bài đã đo`} />
      </div>
    </AnalyticsShell>
  );
}
