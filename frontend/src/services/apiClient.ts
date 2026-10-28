/**
 * A single place that talks to the backend.
 *
 * Every request carries the session cookie and, for anything that changes
 * data, the CSRF token that the server handed out with the session.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(friendlyMessage(status, detail));
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }
}

/**
 * The backend answers with a short technical sentence. The ones a customer
 * can meet are translated, the rest are replaced by a plain message.
 */
const TRANSLATED: Record<string, string> = {
  'Not found': 'Không tìm thấy dữ liệu.',
  'Evidence not found': 'Không tìm thấy bằng chứng.',
  'Profile link must use facebook.com HTTPS': 'Link trang cá nhân phải là đường dẫn facebook.com.',
  'Schedule cannot be in the past': 'Lịch đăng không được nằm trong quá khứ.',
  'This group is assigned to another Facebook account': 'Group này đang gán cho tài khoản Facebook khác.',
  'scheduled_at must be ISO 8601 with timezone': 'Thời điểm đăng phải có định dạng ISO 8601 kèm múi giờ.',
  'Upload exactly one video for Reel/Video': 'Chỉ tải lên đúng một video cho Reel hoặc Video.',
};

/**
 * A message that can be shown to a customer.
 *
 * The validation report of the backend is technical; it is translated into a
 * short sentence instead of being shown as it is.
 */
function friendlyMessage(status: number, detail: unknown): string {
  if (status >= 500) return 'Máy chủ gặp sự cố, vui lòng thử lại.';
  if (typeof detail === 'object' && detail !== null && 'code' in detail) {
    const code = (detail as { code?: string }).code;
    if (code === 'PIPELINE_MEMBERSHIP_CONFLICT') return 'Workspace có phân công CTV xung đột. Vui lòng liên hệ quản trị viên.';
    if (code === 'POST_EVIDENCE_REQUIRED') return 'Chưa có bằng chứng xác minh bài đã đăng.';
    if (code === 'GROUP_RECHECK_REQUIRED') return 'Cần kiểm tra lại quyền đăng bằng Facebook trước khi bật Group.';
  }
  if (typeof detail === 'string'  && detail.trim()) {
    return TRANSLATED[detail] ?? detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { type?: string; loc?: (string | number)[] };
    const last = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : undefined;
    const field = typeof last === 'string' ? last : undefined;
    const type = first.type ?? '';
    if (type.startsWith('int_') || type.startsWith('float_')) {
      return field ? `Giá trị của "${field}" phải là số.` : 'Giá trị gửi lên phải là số.';
    }
    if (type === 'missing') {
      return field ? `Thiếu dữ liệu "${field}".` : 'Thiếu dữ liệu cần thiết.';
    }
    if (field) return `Dữ liệu "${field}" không hợp lệ.`;
    return 'Dữ liệu gửi lên không hợp lệ.';
  }
  if (status === 422) return 'Dữ liệu gửi lên không hợp lệ.';
  if (status === 403) return 'Bạn không có quyền thực hiện thao tác này.';
  if (status === 404) return 'Không tìm thấy dữ liệu.';
  if (status >= 500) return 'Máy chủ gặp sự cố, vui lòng thử lại.';
  return 'Yêu cầu thất bại.';
}

/** A path with a missing value never reaches the backend. */
const MISSING_VALUE = /\/(undefined|null|NaN)(\/|\?|$)/;

function assertPath(path: string): void {
  if (MISSING_VALUE.test(path)) {
    throw new ApiError(0, 'Workspace chưa được chọn. Hãy tải lại trang và thử lại.');
  }
}

let csrfToken = '';

export function setCsrfToken(token: string): void {
  csrfToken = token ?? '';
}

export function getCsrfToken(): string {
  return csrfToken;
}

type Body = unknown;

async function request<T>(method: string, path: string, body?: Body, signal?: AbortSignal): Promise<T> {
  assertPath(path);
  const headers: Record<string, string> = {};
  const options: RequestInit = { method, credentials: 'same-origin', headers, signal };

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  if (method !== 'GET' && method !== 'HEAD' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  const response = await fetch(path, options);
  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in (payload as Record<string, unknown>)
        ? (payload as Record<string, unknown>).detail
        : payload;
    throw new ApiError(response.status, detail);
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>('GET', path, undefined, signal),
  post: <T>(path: string, body?: Body) => request<T>('POST', path, body ?? {}),
  patch: <T>(path: string, body?: Body) => request<T>('PATCH', path, body ?? {}),
  put: <T>(path: string, body?: Body) => request<T>('PUT', path, body ?? {}),
  delete: <T>(path: string) => request<T>('DELETE', path),
  upload: async <T>(path: string, form: FormData, signal?: AbortSignal): Promise<T> => {
    assertPath(path);
    const headers: Record<string, string> = {};
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
    const response = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers,
      body: form,
      signal,
    });
    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    if (!response.ok) {
      const detail =
        payload && typeof payload === 'object' && 'detail' in (payload as Record<string, unknown>)
          ? (payload as Record<string, unknown>).detail
          : payload;
      throw new ApiError(response.status, detail);
    }
    return payload as T;
  },
};
