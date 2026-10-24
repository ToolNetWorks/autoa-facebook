import type { Permission } from '@/types/api';

export interface NavEntry {
  id: string;
  label: string;
  permission?: Permission;
  adminOnly?: boolean;
}

export interface NavSection {
  heading: string;
  items: NavEntry[];
}

/** The workspace menu, grouped so the sidebar stays short. */
export const WORKSPACE_NAV: NavSection[] = [
  {
    heading: 'Không gian',
    items: [
      { id: 'overview', label: 'Tổng quan' },
      { id: 'accounts', label: 'Tài khoản Facebook', permission: 'facebook.view' },
      { id: 'album', label: 'Album ảnh', permission: 'media.view' },
    ],
  },
  {
    heading: 'Đăng bài',
    items: [
      { id: 'groups', label: 'Nhóm Facebook', permission: 'groups.view' },
      { id: 'screening', label: 'Group Screening', permission: 'groups.view' },
      { id: 'auto-posting', label: 'Tự động đăng group', permission: 'auto_post.self_configure' },
      { id: 'group-post', label: 'Tạo bài group', permission: 'group_posts.create' },
      { id: 'reels', label: 'Trang cá nhân · Reels', permission: 'reels.create' },
    ],
  },
  {
    heading: 'AI và an toàn',
    items: [
      { id: 'ai-caption', label: 'AI Caption', permission: 'ai_caption.use' },
      { id: 'video-precheck', label: 'Video Pre-Check', permission: 'reels.create' },
      { id: 'safety', label: 'Safety Center', permission: 'history.view' },
    ],
  },
  {
    heading: 'Vận hành',
    items: [
      { id: 'schedule', label: 'Lịch đăng & hàng đợi', permission: 'history.view' },
      { id: 'comments', label: 'Bình luận', permission: 'comments.manage' },
      { id: 'history', label: 'Lịch sử', permission: 'history.view' },
    ],
  },
  {
    heading: 'Quản trị',
    items: [
      { id: 'collaborators', label: 'CTV & phân quyền', adminOnly: true },
      { id: 'access-keys', label: 'Pipeline Key', adminOnly: true },
      { id: 'settings', label: 'Cài đặt Workspace', adminOnly: true },
    ],
  },
];

export const ADMIN_NAV: NavEntry[] = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'collaborators', label: 'CTV & phân quyền' },
  { id: 'access-keys', label: 'Pipeline Key' },
  { id: 'security', label: 'Bảo mật' },
  { id: 'audit', label: 'Nhật ký' },
];

export function visibleEntries(
  sections: NavSection[],
  options: { isAdmin: boolean; can: (permission: Permission) => boolean },
): NavSection[] {
  return sections
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => {
        if (item.adminOnly && !options.isAdmin) return false;
        if (item.permission && !options.can(item.permission)) return false;
        return true;
      }),
    }))
    .filter((section) => section.items.length > 0);
}
