/** The shapes the backend returns. One place to keep them honest. */

export type Role = 'admin' | 'ctv';

export type Permission =
  | 'facebook.view'
  | 'facebook.add'
  | 'facebook.update_session'
  | 'groups.view'
  | 'groups.manage'
  | 'reels.create'
  | 'reels.publish'
  | 'group_posts.create'
  | 'group_posts.publish'
  | 'comments.manage'
  | 'ai_caption.use'
  | 'schedule.manage'
  | 'history.view'
  | 'media.view'
  | 'media.upload'
  | 'media.edit'
  | 'media.delete'
  | 'media.approve'
  | 'auto_post.configure'
  | 'auto_post.create'
  | 'auto_post.approve'
  | 'auto_post.publish'
  | 'auto_post.self_configure'
  | 'analytics.view';

export interface Session {
  role: Role;
  username: string;
  user_id: number;
  pipeline_id: number | null;
  permissions: Permission[];
  csrf_token: string;
  must_change_password: boolean;
  pipeline_name?: string | null;
}

export interface Pipeline {
  id: number;
  name: string;
  description: string;
  enabled: number;
  created_at: string;
  accounts_count?: number;
  groups_count?: number;
  queued_count?: number;
  posted_count?: number;
  pending_count?: number;
  failed_count?: number;
  ctv_count?: number;
  reels_count?: number;
  active_keys?: number;
}

export interface PipelineAdminRow extends Pipeline {
  accounts_count: number;
  groups_count: number;
  ctv_count: number;
  posted_count: number;
  pending_count: number;
  failed_count: number;
  reels_count: number;
  active_keys: number;
}

export interface FacebookAccount {
  id: number;
  pipeline_id: number;
  label: string;
  profile_url: string;
  enabled: number;
  created_at: string;
  auth_method: 'cookie_file' | 'browser';
  cookie_updated_at: string | null;
  session_status: string;
  session_checked_at: string | null;
  session_details: string;
  fb_name: string;
  fb_uid: string;
  fb_avatar: string;
  login_open: boolean;
  profile_busy: boolean;
  pipeline_name: string;
  owner_user_id: number | null;
  owner_username: string | null;
}

export interface GroupRow {
  id: number;
  pipeline_id: number;
  account_id: number | null;
  account_label: string | null;
  label: string;
  url: string;
  group_key: string;
  enabled: number;
  approved_target: number;
  created_at: string;
  group_name: string;
  posting_mode: 'anonymous' | 'nickname';
  nickname: string;
  status: 'active' | 'paused';
  screening_status: string;
  screening_source: string;
  screening_confidence: number;
  screening_checked_at: string | null;
  check_status: string;
  last_checked_at: string | null;
  /** Phân loại học được từ kết quả đăng bài thật. */
  classification: 'no_approval' | 'needs_approval' | 'unclassified';
  classification_label: string;
  classification_tone: 'ok' | 'warn' | 'neutral';
  priority: 'high' | 'low' | 'none';
  priority_rank: number;
  /** Khả năng đăng mà group thực sự hỗ trợ. */
  supports_anonymous: boolean;
  supports_nickname: boolean;
  capability_state: 'known' | 'unknown';
  /** Chế độ CTV đã chọn, khác với khả năng đăng. */
  selected_posting_mode: 'anonymous' | 'nickname';
  last_verified_at: string | null;
  last_post_status: string;
  posted_count: number;
  pending_count: number;
  failed_count: number;
  accounts: { id: number; account_id: number; posting_mode: string; nickname: string; account_label: string }[];
  owner_user_id: number | null;
  owner_username: string | null;
  shared: boolean;
}

/** The six states of a picture in the album. They are never merged. */
export type MediaState =
  | 'uploaded'
  | 'analyzing'
  | 'ready'
  | 'needs_review'
  | 'blocked'
  | 'check_failed';

export interface MediaAsset {
  id: number;
  pipeline_id: number;
  storage_key: string;
  original_name: string;
  mime_type: string;
  width: number;
  height: number;
  size_bytes: number;
  description: string;
  safety_status: 'pass' | 'needs_review' | 'blocked' | 'unknown';
  analysis_status: string;
  usable: boolean;
  locked: boolean;
  saved: boolean;
  usage_count: number;
  last_used_at: string | null;
  created_at: string;
  tags: string[];
  usage_history_count?: number;
  /** The state of the picture, worked out by the backend. */
  state: MediaState;
  state_label: string;
  state_tone: 'ok' | 'warn' | 'bad' | 'neutral' | 'info';
  /** A short sentence the collaborator can act on. */
  reason: string;
  can_retry: boolean;
  can_be_usable: boolean;
}

export interface MediaAlbum {
  media: MediaAsset[];
  counts: {
    total: number;
    usable: number;
    needs_review: number;
    blocked: number;
    failed: number;
    waiting: number;
    locked: number;
    saved: number;
  };
}

export interface JobRow {
  id: number;
  state: string;
  posting_status: string;
  requested_posting_mode: string;
  requested_nickname: string;
  actual_posting_identity: string;
  verification_status: string;
  details: string;
  evidence: string;
  scheduled_at: string;
  updated_at: string;
  group_name: string;
  group_url: string;
  account_name: string;
  pipeline_name: string;
  body: string;
  media_json: string;
  anonymous_only: number;
  failure_reason: string;
}

export interface ScheduledPostRow {
  id: number;
  pipeline_id: number;
  body: string;
  media_json: string;
  created_at: string;
  anonymous_only: number;
  ai_record_id?: number | null;
  job_count: number;
  pending_count: number;
  posted_count: number;
  failed_count: number;
  first_scheduled: string | null;
  last_scheduled: string | null;
}

export interface PostJobRow {
  id: number;
  state: string;
  posting_status: string;
  requested_posting_mode: string;
  requested_nickname: string;
  actual_posting_identity: string;
  verification_status: string;
  details: string;
  evidence: string;
  scheduled_at: string;
  updated_at: string;
  group_name: string;
  group_url: string;
  group_mode: string;
  group_nickname: string;
  account_name: string;
  fb_name: string;
  failure_reason: string;
  permalink: string;
  published_at: string | null;
}

export interface ManualTask {
  id: number;
  task_type: string;
  body: string;
  status: string;
  auto_publish: number;
  details: string;
  evidence: string;
  scheduled_at: string;
  account_label: string;
  group_label: string | null;
  pipeline_name: string;
  created_at: string;
  target_url: string;
}

export interface Collaborator {
  id: number;
  username: string;
  role: Role;
  status: 'active' | 'suspended';
  must_change_password: number;
  created_at: string;
  pipeline_id: number | null;
  pipeline_name: string | null;
  permissions: Permission[];
  membership_status: string | null;
}

export interface AccessKey {
  id: number;
  pipeline_id: number;
  key_prefix: string;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface AutoPostConfig {
  id: number;
  pipeline_id: number;
  account_id: number | null;
  group_ids: number[];
  topic: string;
  ai_caption: boolean;
  daily_limit: number;
  require_approval: boolean;
  enabled: boolean;
  tone: string;
}

export interface AutoPostJob {
  id: number;
  pipeline_id: number;
  media_id: number | null;
  media_name: string | null;
  account_id: number | null;
  group_id: number | null;
  group_label: string | null;
  caption: string;
  posting_mode: string;
  nickname: string;
  status: string;
  error: string;
  created_at: string;
}

/** The advanced rules of one collaborator's own automatic posting. */
export interface CtvAutoPostRules {
  days_enabled: number[];
  min_gap_minutes: number;
  group_daily_limit: number;
  image_selection_rule: 'rotate' | 'unused_first' | 'random';
}

/** The configuration of one collaborator, scoped to pipeline + user. */
export interface CtvAutoPostSettings {
  id?: number;
  pipeline_id: number | null;
  ctv_user_id: number | null;
  enabled: boolean;
  account_scope: 'all' | 'selected';
  selected_account_ids: number[];
  selected_group_ids: number[];
  topic: string;
  caption_template: string;
  max_posts_per_account_daily: number;
  start_time: string;
  end_time: string;
  timezone: string;
  schedule_rules: CtvAutoPostRules;
  ai_caption_enabled: boolean;
  approval_required: boolean;
  anti_duplicate: boolean;
  apply_to_new_accounts: boolean;
  created_at?: string;
  updated_at?: string;
  username?: string;
}

/** The limits of the pipeline that a collaborator may not go beyond. */
export interface CtvAutoPostPolicy {
  daily_limit_per_account: number;
  min_gap_minutes: number;
  pipeline_daily_limit: number;
}

export interface CtvAutoPostOwner {
  user_id: number;
  username: string;
  role: Role;
  status: string;
  membership_status: string | null;
  is_member: boolean;
}

/** The answer of the "my configuration" endpoint. */
export interface CtvAutoPostConfig {
  ok: boolean;
  configured: boolean;
  settings: CtvAutoPostSettings;
  owner?: CtvAutoPostOwner | null;
  policy?: CtvAutoPostPolicy;
}

export interface CtvAutoPostJob {
  id: number;
  pipeline_id: number;
  account_id: number | null;
  group_id: number | null;
  media_id: number | null;
  status: string;
  caption: string;
  scheduled_for: string | null;
  created_at: string;
  created_by_ctv_id: number | null;
  group_label: string | null;
  account_label: string | null;
}

export interface PrecheckIssue {
  policy_category: string;
  label: string;
  risk_level: 'high' | 'medium' | 'low';
  action: 'pass' | 'review' | 'block';
  reason: string;
  timestamp: number | null;
  evidence: string;
  remediation: string;
  confidence: 'high' | 'medium' | 'low';
  policy_ref: string;
}

export interface PrecheckCategory {
  status: 'pass' | 'needs_review' | 'blocked' | 'unknown' | 'check_failed' | 'not_provided';
  checked: boolean;
  reasons: string[];
  suggestions: string[];
  details?: Record<string, unknown>;
}

export interface Precheck {
  id: number;
  status: string;
  progress: string;
  model_version: string;
  vision_model: string;
  media_id: string;
  video_sha256: string;
  caption_checked: string;
  approved: boolean;
  approved_by: number | null;
  approved_at: string | null;
  approval_note: string;
  checked_at: string | null;
  created_at: string;
  warnings: string[];
  recommendations: string[];
  issues: PrecheckIssue[];
  policy_version: string;
  copyright_status: string;
  checked_scope: string;
  error: string;
  categories: Record<string, PrecheckCategory>;
}

export interface AuditEntry {
  id: number;
  actor_id: number | null;
  actor_username: string;
  pipeline_id: number | null;
  action: string;
  detail: string;
  created_at: string;
}

/* --- admin analytics -------------------------------------------------------- */

export interface AnalyticsKpis {
  total: number;
  posts_published: number;
  posts_waiting: number;
  posts_refused: number;
  posts_removed: number;
  submitted_unverified: number;
  group_posts: number;
  group_posts_posted: number;
  reels: number;
  reels_posted: number;
  engagement: number | null;
  reactions: number | null;
  comments: number | null;
  shares: number | null;
  views: number | null;
  reach: number | null;
  measured_posts: number;
  unmeasured_posts: number;
  engagement_available: boolean;
  views_available: boolean;
  pipelines_active?: number;
  collaborators_active?: number;
}

export interface AnalyticsPipelineRow {
  pipeline_id: number;
  name: string;
  description: string;
  enabled: boolean;
  collaborators: number;
  posts_published: number;
  posts_waiting: number;
  posts_refused: number;
  posts_removed: number;
  engagement: number | null;
  views: number | null;
  measured_posts: number;
  last_activity: string | null;
}

export interface AnalyticsCollaboratorRow {
  user_id: number;
  username: string;
  status: string;
  pipeline_id: number | null;
  pipeline_name: string | null;
  posts_created: number;
  posts_published: number;
  posts_waiting: number;
  posts_refused: number;
  posts_removed: number;
  engagement: number | null;
  views: number | null;
  measured_posts: number;
  success_rate: number | null;
  last_activity: string | null;
}

export interface AnalyticsPost {
  content_type: 'group_post' | 'reel';
  post_id: number;
  title: string;
  group_label: string | null;
  account_label: string | null;
  permalink: string;
  published_at: string | null;
  created_at: string | null;
  status: string | null;
  created_by: number | null;
  reactions: number | null;
  comments: number | null;
  shares: number | null;
  views: number | null;
  reach: number | null;
  engagement: number | null;
  measured: boolean;
  removed: boolean;
}

export interface AnalyticsPoint {
  day: string;
  posts: number;
  reels: number;
  engagement: number;
  measured: number;
  views: number;
}

export interface AnalyticsSources {
  graph_api: boolean;
  facebook_session: boolean;
}
