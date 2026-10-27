export interface AutoPostJobItem {
  id: number;
  post_id?: number | null;
  status: string;
  media_id?: number | null;
  media_name?: string | null;
  account_label?: string | null;
  account_id?: number | null;
  group_label?: string | null;
  group_id?: number | null;
  group_url?: string | null;
  caption: string;
  posting_mode?: string;
  nickname?: string;
  scheduled_for?: string | null;
  created_at?: string;
  updated_at?: string;
  error?: string;
  defer_reason?: string;
  evidence?: string;
  permalink?: string;
  attempts?: number;
}

export function getPostLink(job?: AutoPostJobItem | null): string {
  if (!job?.permalink) return '';
  try {
    const url = new URL(job.permalink);
    if (url.protocol !== 'https:' || !['facebook.com','www.facebook.com','web.facebook.com','m.facebook.com'].includes(url.hostname)) return '';
    if (!/^\/groups\/[^/]+\/(posts|permalink|pending_posts)\/[^/]+\/?$/.test(url.pathname)) return '';
    return url.href;
  } catch { return ''; }
}

export type FilterCategory = 'all' | 'waiting' | 'processing' | 'needs_review' | 'posted' | 'failed';

export function mapJobStatus(status: string): {
  label: string;
  tone: 'neutral' | 'ok' | 'warn' | 'bad' | 'info';
  description: string;
} {
  switch (status) {
    case 'pending':
      return { label: 'Đang chờ', tone: 'neutral', description: 'Đã lưu lịch, chờ đến giờ thực thi' };
    case 'approved':
      return { label: 'Đã duyệt', tone: 'info', description: 'Đã phê duyệt, chờ xuất bản' };
    case 'queued':
      return { label: 'Đang xử lý (hàng đợi)', tone: 'warn', description: 'Đã đưa vào hàng đợi chuẩn bị đăng' };
    case 'running':
      return { label: 'Đang xử lý', tone: 'warn', description: 'Worker đang tương tác với Facebook' };
    case 'needs_review':
      return { label: 'Cần xem lại', tone: 'warn', description: 'Chưa thể xác nhận hoặc cần kiểm tra thủ công' };
    case 'pending_approval':
      return { label: 'Chờ duyệt', tone: 'warn', description: 'Bài đã gửi, đang chờ quản trị viên group duyệt' };
    case 'submitted_unverified':
      return { label: 'Chưa xác nhận', tone: 'warn', description: 'Đã thử gửi bài, chưa xác minh được kết quả; cần kiểm tra trước khi gửi lại' };
    case 'posted':
    case 'published':
      return { label: 'Đã đăng', tone: 'ok', description: 'Có bằng chứng bài viết đã đăng thành công trên Facebook' };
    case 'failed':
      return { label: 'Thất bại', tone: 'bad', description: 'Có lỗi xác định khi đăng' };
    case 'blocked':
      return { label: 'Bị chặn', tone: 'bad', description: 'Bị chặn bởi bộ lọc an toàn hoặc giới hạn' };
    case 'cancelled':
      return { label: 'Đã hủy', tone: 'neutral', description: 'Đã hủy trước khi thực thi' };
    default:
      return { label: status, tone: 'neutral', description: status };
  }
}

export function matchesFilter(job: AutoPostJobItem, filter: FilterCategory): boolean {
  if (filter === 'all') return true;
  if (filter === 'waiting') return ['pending', 'approved'].includes(job.status);
  if (filter === 'processing') return ['queued', 'running'].includes(job.status);
  if (filter === 'needs_review') return ['needs_review', 'pending_approval', 'submitted_unverified'].includes(job.status);
  if (filter === 'posted') return ['posted', 'published'].includes(job.status);
  if (filter === 'failed') return ['failed', 'blocked', 'cancelled'].includes(job.status);
  return true;
}
