import { useAuth } from '@/app/AuthProvider';
import { CtvAutoPostPanel } from '@/features/auto-posting/CtvAutoPostPanel';
import { CtvConfigList } from '@/features/auto-posting/CtvConfigList';
import { SharedAutoPostPanel } from '@/features/auto-posting/SharedAutoPostPanel';
import { ErrorState } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';
import { useAsync } from '@/hooks/useAsync';
import { api, ApiError } from '@/services/apiClient';
import { Skeleton } from '@/components/ui/Surfaces';
import { CycleWorkspace } from './cycles/CycleWorkspace';
import type { CycleConfig } from './cycles/types';

/**
 * The automatic posting section.
 *
 * A collaborator configures their own posting, the administrator keeps the
 * shared policy of the workspace and watches the configuration of everybody.
 */
export function AutoPostingSection({ workspace }: { workspace: WorkspaceData }) {
  const { isAdmin } = useAuth();
  const pipelineId = workspace.pipelineId;
  const cycles = useAsync(async signal => {
    if (!pipelineId) return null;
    try { return await api.get<CycleConfig>(`/api/pipelines/${pipelineId}/auto-post/cycle-config`,signal); }
    catch (error) {
      if (error instanceof ApiError && error.status === 503 && (error.detail as {code?: string})?.code === 'CYCLE_SCHEMA_REQUIRED') return null;
      throw error;
    }
  },[pipelineId]);

  if (!pipelineId) {
    return (
      <ErrorState
        title="Chưa chọn Workspace"
        description="Hãy mở một Workspace trước khi cấu hình tự động đăng group."
      />
    );
  }
  if (cycles.loading && !cycles.data) return <Skeleton variant="block" />;
  if (cycles.error) return <ErrorState title="Chưa tải được Auto Posting" description={cycles.error} />;
  if (cycles.data && cycles.data.pipeline_id !== pipelineId) return <Skeleton variant="block" />;
  if (cycles.data) return <CycleWorkspace key={pipelineId} pipelineId={pipelineId} accounts={workspace.accounts} config={cycles.data} onChanged={cycles.reload} />;

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Tự động đăng Group</h2>
          <p className="hint">
            Workspace {workspace.pipeline?.name ?? ''} ·{' '}
            {isAdmin
              ? 'chính sách chung và cấu hình của từng CTV.'
              : 'cấu hình riêng của bạn, không ảnh hưởng CTV khác.'}
          </p>
        </div>
      </div>

      {isAdmin ? (
        <>
          <SharedAutoPostPanel pipelineId={pipelineId} groups={workspace.groups ?? []} />
          <CtvConfigList pipelineId={pipelineId} />
        </>
      ) : (
        <CtvAutoPostPanel
          pipelineId={pipelineId}
          accounts={workspace.accounts ?? []}
          groups={workspace.groups ?? []}
        />
      )}
    </div>
  );
}
