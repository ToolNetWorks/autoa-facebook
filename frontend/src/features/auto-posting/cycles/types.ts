export interface CycleSettings {
  enabled: boolean; account_ids: number[]; group_ids: number[];
  album_id: number | null; media_ids: number[]; repeat_media: boolean;
  caption_mode: 'template' | 'ai'; template_ids: number[]; fallback_template_id: number | null;
  unique_captions: boolean; topic: string; posting_mode: 'anonymous' | 'nickname';
  timezone: string; days: number[]; start_time: string; end_time: string;
  cycle_interval_minutes: number; post_interval_minutes: number;
  groups_per_cycle: number; per_account_daily_limit: number; require_approval: boolean;
}
export interface GroupState {
  id: number; label: string; url: string; global_state: string; label_state: string;
  accounts: {account_id: number; state: string}[];
}
export interface CycleConfig {
  pipeline_id: number; configured: boolean; settings: CycleSettings;
  overview: {groups: GroupState[]; selected_accounts: number; eligible_groups: number | null;
    disabled_groups: number; expected_jobs: number | null; estimate_kind: 'upper_bound'; next_run_at: string | null};
}
export interface CycleLibrary {
  albums: {id: number; name: string; media_ids: number[]}[];
  templates: {id: number; name: string; body: string; approved: boolean; revision: number}[];
  media: {id: number; name: string; eligible: boolean}[];
}
export interface CycleItem {
  id: number; group_id: number; group_name: string; group_label: string; group_url: string;
  account_id: number | null; account_label: string; account_name: string; media_id: number | null;
  media_name: string; caption: string; status: string; reason_code: string; failure_level: string;
  job_id: number | null; post_id: number | null; auto_job_id: number | null;
  scheduled_for: string; started_at: string | null; executed_at: string | null;
  evidence: string; permalink: string;
}
export interface CycleReport {
  id: number; pipeline_id: number; timezone: string; state: string; due_at: string; items: CycleItem[]; next_run_at: string | null;
  excluded_groups: {id: number; group_name: string; label: string; url: string}[];
  summary: {total_groups: number; eligible_groups: number; posted: number; pending_approval: number;
    submitted_unverified: number; needs_review: number; disabled_groups: number; skipped: number;
    failed: number; no_eligible_account: number};
}
export const STATUS: Record<string,string> = {
  planned: 'Đã lập kế hoạch', preparing: 'Đang chuẩn bị', pending: 'Chờ duyệt nội dung',
  approved: 'Đã xếp lịch', queued: 'Đang chờ chạy', running: 'Đang xử lý', posted: 'Đã xác minh đăng',
  pending_approval: 'Chờ Admin nhóm duyệt', submitted_unverified: 'Đã gửi, chưa xác minh',
  needs_review: 'Cần đối chiếu thủ công', failed: 'Thất bại', skipped: 'Bỏ qua',
  no_eligible_account: 'Không có tài khoản đủ điều kiện', cancelled: 'Đã hủy',
};
export const REASON: Record<string,string> = {
  NO_ELIGIBLE_ACCOUNT: 'Chưa có tài khoản đáp ứng quyền đăng, phiên hoặc hạn mức.',
  SKIPPED_GROUP_DISABLED: 'Group Bị Vô Hiệu Hoá', MEDIA_EXHAUSTED: 'Album chưa còn ảnh đủ điều kiện.',
  DESTINATION_HAS_OPEN_WORK: 'Nhóm đã có bài đang chờ xử lý hoặc xác minh.',
  DUPLICATE_CONTENT: 'Nội dung và ảnh đã được dùng cho nhóm này.', DUPLICATE_CAPTION: 'Caption trùng với nội dung đã chuẩn bị.',
  AI_CAPTION_FAILED: 'Chưa tạo được caption; chưa có mẫu dự phòng được duyệt.',
  CAPTION_NEEDS_REVIEW: 'Caption cần được xem xét trước khi đăng.',
};
