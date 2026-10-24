import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '@/services/apiClient';
import { useAsync } from '@/hooks/useAsync';
import { KpiRow } from '@/features/analytics/AnalyticsWidgets';
import { measured } from '@/features/analytics/AnalyticsWidgets';
import type { AnalyticsKpis, AnalyticsPipelineRow } from '@/types/api';
import { useAuth } from '@/app/AuthProvider';
import { useToast } from '@/app/ToastProvider';
import { AppShell } from '@/app/layout/AppShell';
import { ADMIN_NAV } from '@/app/navigation';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Badge, EmptyState, ErrorState, Skeleton, Stat } from '@/components/ui/Surfaces';
import type { PipelineAdminRow } from '@/types/api';
import { PasswordChangeModal } from '@/features/admin-dashboard/PasswordChangeModal';

function PipelineCard({
  row,
  analytics,
  onOpen,
  onCollaborators,
  onKeys,
  onReport,
}: {
  row: PipelineAdminRow;
  analytics?: AnalyticsPipelineRow;
  onOpen: () => void;
  onCollaborators: () => void;
  onKeys: () => void;
  onReport: () => void;
}) {
  return (
    <article className="card tight workspace-card">
      <div className="row-between" style={{ marginBottom: 'var(--sp-3)' }}>
        <div style={{ minWidth: 0 }}>
          <h3 style={{ fontSize: 'var(--fs-16)' }}>
            <Link className="link" to={`/analytics/pipeline/${row.id}`}>
              {row.name}
            </Link>
          </h3>
          <p className="hint" style={{ marginTop: 2 }}>
            {row.description || 'Chưa có mô tả'}
          </p>
        </div>
        <Badge tone={row.enabled ? 'ok' : 'neutral'}>{row.enabled ? 'Hoạt động' : 'Tạm dừng'}</Badge>
      </div>

      <div className="stat-row" style={{ marginBottom: 'var(--sp-3)' }}>
        <Stat label="Bài đã đăng" value={analytics?.posts_published ?? row.posted_count} />
        <Stat
          label="Tương tác"
          value={analytics ? measured(analytics.engagement).text : '—'}
          muted={analytics?.engagement === null}
        />
        <Stat
          label="Lượt xem reel"
          value={analytics ? measured(analytics.views).text : '—'}
          muted={analytics?.views === null}
        />
        <Stat label="Chờ duyệt" value={analytics?.posts_waiting ?? row.pending_count} />
        <Stat label="CTV" value={analytics?.collaborators ?? row.ctv_count} />
      </div>

      <div className="btn-row">
        <Button variant="primary" size="sm" onClick={onOpen}>
          Mở Workspace
        </Button>
        <Button size="sm" onClick={onCollaborators}>
          Quản lý CTV
        </Button>
        <Button size="sm" onClick={onKeys}>
          Pipeline Key
        </Button>
        <Button size="sm" onClick={onReport}>
          Báo cáo
        </Button>
      </div>
    </article>
  );
}

const emptyKpis: AnalyticsKpis = {
  total: 0, posts_published: 0, posts_waiting: 0, posts_refused: 0, posts_removed: 0,
  submitted_unverified: 0, group_posts: 0, group_posts_posted: 0, reels: 0, reels_posted: 0,
  engagement: null, reactions: null, comments: null, shares: null, views: null, reach: null,
  measured_posts: 0, unmeasured_posts: 0, engagement_available: false, views_available: false,
};

export function DashboardPage() {
  const navigate = useNavigate();
  const { signOut, session, refresh } = useAuth();
  const { push } = useToast();
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);
  const state = useAsync<PipelineAdminRow[]>((signal) => api.get('/api/admin/pipelines', signal), []);
  const analytics = useAsync<{ kpis: AnalyticsKpis }>(
    (signal) => api.get('/api/admin/analytics/overview', signal),
    [],
  );
  const performance = useAsync<{ pipelines: AnalyticsPipelineRow[] }>(
    (signal) => api.get('/api/admin/analytics/pipelines?days=30', signal),
    [],
  );
  const performanceByPipeline = new Map(
    (performance.data?.pipelines ?? []).map((item) => [item.pipeline_id, item]),
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  async function createPipeline() {
    setBusy(true);
    try {
      const created = await api.post<{ id: number }>('/api/pipelines', { name: name.trim(), description: description.trim() });
      push(`Đã tạo pipeline ${name.trim()}`, 'success');
      setCreateOpen(false);
      setName('');
      setDescription('');
      state.reload();
      navigate(`/workspace/${created.id}/overview`);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tạo được pipeline', 'error');
    } finally {
      setBusy(false);
    }
  }

  const rows = state.data ?? [];
  const forcePasswordChange = session?.must_change_password ?? false;

  useEffect(() => {
    if (forcePasswordChange) {
      setChangePasswordOpen(true);
    }
  }, [forcePasswordChange]);

  async function handlePasswordChanged() {
    await refresh();
  }

  function openChangePassword() {
    setChangePasswordOpen(true);
  }

  return (
    <AppShell
      nav={[{ heading: 'Quản trị', items: ADMIN_NAV }]}
      activeId="dashboard"
      onNavigate={(id) => {
        if (id === 'dashboard') navigate('/');
        else navigate(`/workspace/${rows[0]?.id ?? 1}/${id}`);
      }}
      title="Dashboard"
      actions={
        <>
          <Button size="sm" onClick={() => state.reload()}>
            Làm mới
          </Button>
          <Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>
            Tạo pipeline
          </Button>
          <Button size="sm" variant="ghost" onClick={openChangePassword}>
            Đổi mật khẩu
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void signOut()}>
            Đăng xuất
          </Button>
        </>
      }
    >
      {state.error ? <ErrorState title="Không tải được danh sách pipeline" description={state.error} /> : null}

      {state.loading ? (
        <div className="card-grid wide">
          {[0, 1, 2].map((index) => (
            <div className="card tight" key={index}>
              <Skeleton variant="title" />
              <Skeleton width="80%" />
              <Skeleton variant="block" />
            </div>
          ))}
        </div>
      ) : null}

      {!state.loading && !state.error && rows.length === 0 ? (
        <EmptyState
          title="Chưa có pipeline nào"
          description="Tạo pipeline đầu tiên để bắt đầu quản lý Facebook."
          action={
            <Button variant="primary" onClick={() => setCreateOpen(true)}>
              Tạo pipeline
            </Button>
          }
        />
      ) : null}

      {!state.loading && rows.length > 0 ? (
        <>
          <KpiRow kpis={analytics.data?.kpis ?? emptyKpis} />

          <div className="stat-row">
            <Stat label="Pipeline" value={rows.length} />
            <Stat label="Tài khoản Facebook" value={rows.reduce((sum, row) => sum + row.accounts_count, 0)} />
            <Stat label="Nhóm" value={rows.reduce((sum, row) => sum + row.groups_count, 0)} />
            <Stat label="CTV" value={rows.reduce((sum, row) => sum + row.ctv_count, 0)} />
            <Stat label="Bài chờ đăng" value={rows.reduce((sum, row) => sum + row.pending_count, 0)} />
            <Stat label="Công việc lỗi" value={rows.reduce((sum, row) => sum + row.failed_count, 0)} />
          </div>

          <div className="card-grid wide">
            {rows.map((row) => (
              <PipelineCard
                key={row.id}
                row={row}
                analytics={performanceByPipeline.get(row.id)}
                onOpen={() => navigate(`/workspace/${row.id}/overview`)}
                onCollaborators={() => navigate(`/workspace/${row.id}/collaborators`)}
                onKeys={() => navigate(`/workspace/${row.id}/access-keys`)}
                onReport={() => navigate(`/analytics/pipeline/${row.id}`)}
              />
            ))}
          </div>
        </>
      ) : null}

      <Modal
        open={createOpen}
        title="Tạo pipeline"
        description="Mỗi pipeline là một Workspace riêng với tài khoản, nhóm và lịch đăng của riêng nó."
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <Button onClick={() => setCreateOpen(false)} disabled={busy}>
              Hủy
            </Button>
            <Button variant="primary" onClick={() => void createPipeline()} loading={busy} disabled={name.trim().length < 2}>
              Tạo pipeline
            </Button>
          </>
        }
      >
        <Input
          label="Tên pipeline"
          name="pipeline_name"
          placeholder="@hiu9h"
          value={name}
          onChange={(event) => setName(event.target.value)}
          hint="Bắt đầu bằng @ và dùng chữ, số, dấu gạch dưới hoặc dấu chấm."
        />
        <Textarea
          label="Mô tả"
          name="pipeline_description"
          rows={3}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
       </Modal>
      <PasswordChangeModal
        open={changePasswordOpen}
        onClose={() => setChangePasswordOpen(false)}
        onPasswordChanged={handlePasswordChanged}
        force={forcePasswordChange}
      />
    </AppShell>
  );
}
