import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '@/app/AuthProvider';
import { LoginPage } from '@/features/authentication/LoginPage';
import { DashboardPage } from '@/features/admin-dashboard/DashboardPage';
import { WorkspacePage } from '@/features/workspace/WorkspacePage';
import { CollaboratorReportPage, PipelineReportPage } from '@/features/analytics/AnalyticsReports';
import { useEffect, type ReactNode } from 'react';

function FullScreenLoader({ label }: { label: string }) {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        color: 'var(--text-muted)',
        fontSize: 'var(--fs-13)',
      }}
    >
      {label}
    </div>
  );
}

function LoginRoute() {
  const { session, loading, signIn } = useAuth();

  if (loading) return <FullScreenLoader label="Đang kiểm tra phiên đăng nhập..." />;
  if (session) return <Navigate to={session.role === 'admin' ? '/' : '/workspace'} replace />;
  return <LoginPage onSignIn={signIn} />;
}

function DashboardRoute() {
  const { session, loading } = useAuth();
  if (loading) return <FullScreenLoader label="Đang tải..." />;
  if (!session) return <Navigate to="/login" replace />;
  if (session.role !== 'admin') return <Navigate to="/workspace" replace />;
  return <DashboardPage />;
}

function WorkspaceRoute() {
  const { session, loading } = useAuth();
  const { pipelineId, section } = useParams();
  const navigate = useNavigate();

  useEffect(() => {
    if (!loading && !session) navigate('/login', { replace: true });
  }, [loading, session, navigate]);

  if (loading) return <FullScreenLoader label="Đang tải..." />;
  if (!session) return <Navigate to="/login" replace />;
  return <WorkspacePage pipelineId={pipelineId} section={section ?? 'overview'} />;
}

function AnalyticsRoute({ children }: { children: ReactNode }) {
  const { session, loading } = useAuth();
  if (loading) return <FullScreenLoader label="Đang tải báo cáo..." />;
  if (!session) return <Navigate to="/login" replace />;
  if (session.role !== 'admin') return <Navigate to="/workspace" replace />;
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginRoute />} />
      <Route path="/" element={<DashboardRoute />} />
      <Route path="/workspace" element={<WorkspaceRoute />} />
      <Route path="/workspace/:pipelineId/:section" element={<WorkspaceRoute />} />
      <Route path="/analytics/pipeline/:pipelineId" element={<AnalyticsRoute><PipelineReportPage /></AnalyticsRoute>} />
      <Route path="/analytics/collaborator/:userId" element={<AnalyticsRoute><CollaboratorReportPage /></AnalyticsRoute>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
