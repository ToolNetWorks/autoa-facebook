import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/app/AuthProvider';
import { visibleEntries, WORKSPACE_NAV } from '@/app/navigation';
import { AppShell } from '@/app/layout/AppShell';
import { useWorkspace } from '@/features/workspace/useWorkspace';
import { OverviewSection } from '@/features/workspace/sections/OverviewSection';
import { AccountsSection } from '@/features/facebook-accounts/AccountsSection';
import { AlbumSection } from '@/features/albums/AlbumSection';
import { GroupsSection } from '@/features/groups/GroupsSection';
import { ScreeningSection } from '@/features/group-screening/ScreeningSection';
import { AutoPostingSection } from '@/features/auto-posting/AutoPostingSection';
import { GroupPostSection } from '@/features/group-post/GroupPostSection';
import { ReelsSection } from '@/features/reels/ReelsSection';
import { AiCaptionSection } from '@/features/ai-caption/AiCaptionSection';
import { VideoPrecheckSection } from '@/features/video-precheck/VideoPrecheckSection';
import { SafetySection } from '@/features/safety/SafetySection';
import { ScheduleSection } from '@/features/scheduler/ScheduleSection';
import { CommentsSection } from '@/features/comments/CommentsSection';
import { HistorySection } from '@/features/history/HistorySection';
import { CollaboratorsSection } from '@/features/collaborators/CollaboratorsSection';
import { AccessKeysSection } from '@/features/access-keys/AccessKeysSection';
import { SettingsSection } from '@/features/settings/SettingsSection';
import type { Permission } from '@/types/api';

const SECTION_TITLES: Record<string, string> = {
  overview: 'Tổng quan',
  accounts: 'Tài khoản Facebook',
  album: 'Album ảnh',
  groups: 'Nhóm Facebook',
  screening: 'Group Screening',
  'auto-posting': 'Tự động đăng group',
  'group-post': 'Tạo bài group',
  reels: 'Trang cá nhân · Reels',
  'ai-caption': 'AI Caption',
  'video-precheck': 'Video Pre-Check',
  safety: 'Safety Center',
  schedule: 'Lịch đăng & hàng đợi',
  comments: 'Bình luận',
  history: 'Lịch sử',
  collaborators: 'CTV & phân quyền',
  'access-keys': 'Pipeline Key',
  settings: 'Cài đặt Workspace',
};

export function WorkspacePage({ pipelineId, section }: { pipelineId?: string; section: string }) {
  const navigate = useNavigate();
  const { session, isAdmin, can } = useAuth();
  const id = Number(pipelineId ?? session?.pipeline_id ?? 0) || null;
  const workspace = useWorkspace(id);

  const nav = visibleEntries(WORKSPACE_NAV, { isAdmin, can: can as (permission: Permission) => boolean });
  const allowed = nav.some((entry) => entry.items.some((item) => item.id === section));
  const active = allowed ? section : 'overview';

  function go(next: string) {
    navigate(`/workspace/${id ?? ''}/${next}`);
  }

  const pipelineName = workspace.pipeline?.name ?? 'Workspace';

  const body = (() => {
    switch (active) {
      case 'accounts':
        return <AccountsSection workspace={workspace} />;
      case 'album':
        return <AlbumSection workspace={workspace} />;
      case 'groups':
        return <GroupsSection workspace={workspace} />;
      case 'screening':
        return <ScreeningSection workspace={workspace} />;
      case 'auto-posting':
        return <AutoPostingSection workspace={workspace} />;
      case 'group-post':
        return <GroupPostSection workspace={workspace} />;
      case 'reels':
        return <ReelsSection workspace={workspace} />;
      case 'ai-caption':
        return <AiCaptionSection workspace={workspace} />;
      case 'video-precheck':
        return <VideoPrecheckSection workspace={workspace} />;
      case 'safety':
        return <SafetySection workspace={workspace} />;
      case 'schedule':
        return <ScheduleSection workspace={workspace} />;
      case 'comments':
        return <CommentsSection workspace={workspace} />;
      case 'history':
        return <HistorySection workspace={workspace} />;
      case 'collaborators':
        return <CollaboratorsSection workspace={workspace} />;
      case 'access-keys':
        return <AccessKeysSection workspace={workspace} />;
      case 'settings':
        return <SettingsSection workspace={workspace} />;
      default:
        return <OverviewSection workspace={workspace} />;
    }
  })();

  return (
    <AppShell
      nav={nav}
      activeId={active}
      onNavigate={go}
      title={SECTION_TITLES[active] ?? 'Workspace'}
      breadcrumb={<span>{pipelineName}</span>}
      backTo={isAdmin ? { label: 'Dashboard', to: '/' } : undefined}
    >
      {body}
    </AppShell>
  );
}
