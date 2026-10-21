import { useAuth } from '@/app/AuthProvider';
import { CtvAutoPostPanel } from '@/features/auto-posting/CtvAutoPostPanel';
import { CtvConfigList } from '@/features/auto-posting/CtvConfigList';
import { SharedAutoPostPanel } from '@/features/auto-posting/SharedAutoPostPanel';
import { ErrorState } from '@/components/ui/Surfaces';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

/**
 * The automatic posting section.
 *
 * A collaborator configures their own posting, the administrator keeps the
 * shared policy of the workspace and watches the configuration of everybody.
 */
export function AutoPostingSection({ workspace }: { workspace: WorkspaceData }) {
  const { isAdmin } = useAuth();
  const pipelineId = workspace.pipelineId;

  if (!pipelineId) {
    return (
      <ErrorState
        title="Chưa chọn Workspace"
        description="Hãy mở một Workspace trước khi cấu hình tự động đăng group."
      />
    );
  }

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
