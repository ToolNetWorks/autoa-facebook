/**
 * Centralized customer-facing labels for internal status enums.
 *
 * This module is the single source of truth for translating internal backend
 * status strings into natural Vietnamese messages that are safe to display.
 * No technical enum values are ever rendered raw.
 */

export const JOB_STATE_LABEL: Record<string, string> = {
  queued: 'Chờ đăng',
  running: 'Đang xử lý',
  pending_approval: 'Chờ duyệt',
  submitted_unverified: 'Đã gửi, chưa xác minh',
  posted: 'Đã đăng',
  failed: 'Thất bại',
  cancelled: 'Đã hủy',
  needs_review: 'Cần xem lại',
  scheduled: 'Chờ đăng',
  processing: 'Đang xử lý',
  completed: 'Hoàn tất',
  pending: 'Chờ',
  ready: 'Sẵn sàng',
  posting: 'Đang đăng',
  duplicate_limit: 'Trùng lặp',
};

export const JOB_STATE_TONE: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
  queued: 'neutral',
  running: 'neutral',
  pending_approval: 'warn',
  submitted_unverified: 'warn',
  posted: 'ok',
  failed: 'bad',
  cancelled: 'neutral',
  needs_review: 'warn',
  scheduled: 'neutral',
  processing: 'neutral',
  completed: 'ok',
  pending: 'neutral',
  ready: 'ok',
  posting: 'neutral',
  duplicate_limit: 'warn',
};

export const TASK_STATUS_LABEL: Record<string, string> = {
  pending: 'Chờ',
  ready: 'Sẵn sàng',
  posting: 'Đang đăng',
  posted: 'Đã đăng',
  failed: 'Thất bại',
  submitted_unverified: 'Chờ xác minh',
  cancelled: 'Đã hủy',
  completed: 'Hoàn tất',
  scheduled: 'Chờ đăng',
  processing: 'Đang xử lý',
};

export const TASK_STATUS_TONE: Record<string, 'ok' | 'warn' | 'bad' | 'neutral'> = {
  pending: 'neutral',
  ready: 'ok',
  posting: 'neutral',
  posted: 'ok',
  failed: 'bad',
  submitted_unverified: 'warn',
  cancelled: 'neutral',
  completed: 'ok',
  scheduled: 'neutral',
  processing: 'neutral',
};

export function jobStateLabel(state: string): string {
  return JOB_STATE_LABEL[state] ?? state;
}

export function jobStateTone(state: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  return JOB_STATE_TONE[state] ?? 'neutral';
}

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABEL[status] ?? status;
}

export function taskStatusTone(status: string): 'ok' | 'warn' | 'bad' | 'neutral' {
  return TASK_STATUS_TONE[status] ?? 'neutral';
}
