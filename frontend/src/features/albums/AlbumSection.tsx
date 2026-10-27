import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, EmptyState, ErrorState, Skeleton, Tabs } from '@/components/ui/Surfaces';
import { usePermissions } from '@/hooks/usePermissions';
import type { MediaAlbum, MediaAsset } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'usable', label: 'Khả dụng' },
  { id: 'review', label: 'Cần xem xét' },
  { id: 'failed', label: 'Kiểm tra lỗi' },
  { id: 'saved', label: 'Đã lưu' },
];

/** How long the page keeps an eye on the analysis before giving up. */
const POLL_LIMIT_MS = 5 * 60 * 1000;
const POLL_INTERVAL_MS = 4000;

export function AlbumSection({ workspace }: { workspace: WorkspaceData }) {
  const { can } = usePermissions();
  const { push } = useToast();
  const [album, setAlbum] = useState<MediaAlbum | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadCount, setUploadCount] = useState(0);
  const [detail, setDetail] = useState<MediaAsset | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const pollUntil = useRef(0);

  const load = useCallback(async (quiet = false) => {
    if (!workspace.pipelineId) {
      setLoading(false);
      return null;
    }
    if (!quiet) setLoading(true);
    try {
      const result = await api.get<MediaAlbum>(`/api/pipelines/${workspace.pipelineId}/media`);
      setAlbum(result);
      setLoadError(null);
      return result;
    } catch (failure) {
      if (!quiet) {
        setLoadError(failure instanceof Error ? failure.message : 'Không tải được Album');
      }
      return null;
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [workspace.pipelineId]);

  useEffect(() => {
    void load();
  }, [load]);

  const items = album?.media ?? [];
  const waiting = items.some((item) => item.state === 'uploaded' || item.state === 'analyzing');

  // While a picture is still being checked the page keeps itself up to date,
  // so the collaborator sees the result without reloading.
  useEffect(() => {
    if (!waiting || Date.now() > pollUntil.current) return;
    const timer = window.setTimeout(() => {
      void load(true);
    }, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, items, load]);

  async function upload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadCount(files.length);
    try {
      const form = new FormData();
      Array.from(files).forEach((file) => form.append('files', file));
      const result = await api.upload<{ count: number }>(
        `/api/pipelines/${workspace.pipelineId}/media/upload`,
        form,
      );
      pollUntil.current = Date.now() + POLL_LIMIT_MS;
      push(`Đã tải ${result.count} ảnh, hệ thống đang kiểm tra`, 'success');
      await load(true);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được ảnh', 'error');
    } finally {
      setUploading(false);
      setUploadCount(0);
      if (fileInput.current) fileInput.current.value = '';
    }
  }

  async function patchMedia(media: MediaAsset, body: Record<string, unknown>) {
    setBusy(media.id);
    try {
      await api.patch(`/api/pipelines/${workspace.pipelineId}/media/${media.id}`, body);
      await load(true);
      setDetail(null);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được ảnh', 'error');
    } finally {
      setBusy(null);
    }
  }

  async function removeMedia(media: MediaAsset) {
    setBusy(media.id);
    try {
      await api.delete(`/api/pipelines/${workspace.pipelineId}/media/${media.id}`);
      push('Đã xóa ảnh', 'success');
      setDetail(null);
      await load(true);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được ảnh', 'error');
    } finally {
      setBusy(null);
    }
  }

  async function retryAnalysis(media: MediaAsset) {
    setBusy(media.id);
    try {
      await api.post(`/api/pipelines/${workspace.pipelineId}/media/${media.id}/analyze`);
      pollUntil.current = Date.now() + POLL_LIMIT_MS;
      push('Đã yêu cầu kiểm tra lại, chờ kết quả', 'info');
      await load(true);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra lại được ảnh', 'error');
    } finally {
      setBusy(null);
    }
  }

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((item) => {
      if (filter === 'usable' && item.state !== 'ready') return false;
      if (filter === 'review' && item.state !== 'needs_review') return false;
      if (filter === 'failed' && item.state !== 'check_failed') return false;
      if (filter === 'saved' && !item.saved) return false;
      if (!needle) return true;
      return (
        item.original_name.toLowerCase().includes(needle) ||
        item.description.toLowerCase().includes(needle) ||
        item.tags.some((tag) => tag.toLowerCase().includes(needle))
      );
    });
  }, [items, filter, search]);

  const counts = album?.counts;

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Album ảnh</h2>
          <p className="hint">
            Kho ảnh của Workspace này. Ảnh kiểm tra đạt thì dùng được ngay cho lịch đăng.
          </p>
        </div>
        <div className="btn-row">
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            className="visually-hidden"
            onChange={(event) => void upload(event.target.files)}
          />
          <Button size="sm" loading={uploading} variant="primary" onClick={() => fileInput.current?.click()}>
            Tải ảnh lên
          </Button>
        </div>
      </div>

      {uploading ? (
        <Alert tone="info">Đang tải {uploadCount} ảnh lên, chờ hệ thống kiểm tra...</Alert>
      ) : null}

      {!uploading && waiting ? (
        <Alert tone="info">Hệ thống đang kiểm tra ảnh mới. Trang tự cập nhật khi có kết quả.</Alert>
      ) : null}

      {loadError ? (
        <ErrorState
          title="Không thể tải Album. Vui lòng thử lại."
          action={
            <Button size="sm" onClick={() => void load()}>
              Thử lại
            </Button>
          }
        />
      ) : null}

      <div className="row" style={{ gap: 'var(--sp-3)' }}>
        <Input
          name="album_search"
          placeholder="Tìm theo tên, mô tả hoặc thẻ"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="grow"
        />
      </div>

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? (counts?.total ?? 0)
              : entry.id === 'usable'
                ? (counts?.usable ?? 0)
                : entry.id === 'review'
                  ? (counts?.needs_review ?? 0)
                  : entry.id === 'failed'
                    ? (counts?.failed ?? 0)
                    : (counts?.saved ?? 0),
        }))}
        active={filter}
        onChange={setFilter}
      />

      {loading ? (
        <div className="media-grid">
          {[0, 1, 2, 3].map((index) => (
            <Skeleton variant="block" key={index} />
          ))}
        </div>
      ) : null}

      {!loading && !loadError && visible.length === 0 ? (
        <EmptyState
          title={items.length === 0 ? 'Album còn trống' : 'Không có ảnh phù hợp'}
          description={
            items.length === 0
              ? 'Tải ảnh của bạn lên để chuẩn bị bài đăng tự động.'
              : 'Thử một từ khóa khác hoặc đổi bộ lọc.'
          }
        />
      ) : null}

      {!loading && visible.length > 0 ? (
        <div className="media-grid">
          {visible.map((item) => (
            <button type="button" className="media-tile" key={item.id} onClick={() => setDetail(item)}>
              <img
                src={`/api/pipelines/${workspace.pipelineId}/media/${item.id}/thumbnail`}
                alt={item.original_name}
                loading="lazy"
                onError={(e) => {
                  const target = e.currentTarget;
                  target.src = '/placeholder-image.png';
                }}
              />
              <span className="meta">
                <span className="cell-title truncate">{item.original_name}</span>
                <span className="cell-sub">
                  {item.width}×{item.height} · {Math.round(item.size_bytes / 1024)} KB
                </span>
                <span className="badge-row">
                  <Badge tone={item.state_tone}>{item.state_label}</Badge>
                  {item.saved ? <Badge tone="info">Đã lưu</Badge> : null}
                  {item.locked ? <Badge tone="neutral">Đã khóa</Badge> : null}
                </span>
                {item.state === 'blocked' ? (
                  <span className="cell-sub">Không đạt ({item.reason})</span>
                ) : item.state === 'check_failed' ? (
                  <span className="cell-sub">{item.reason}</span>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      <Modal
        open={detail !== null}
        title={detail?.original_name ?? ''}
        description={detail?.state_label ?? ''}
        onClose={() => setDetail(null)}
        footer={
          <>
            <Button onClick={() => setDetail(null)}>Đóng</Button>
            {detail && detail.state === 'check_failed' && detail.can_retry ? (
              <Button
                variant="primary"
                size="sm"
                loading={busy === detail.id}
                onClick={() => void retryAnalysis(detail)}
              >
                Kiểm tra lại
              </Button>
            ) : null}
            {detail && can('media.delete') ? (
              <Button variant="danger" onClick={() => void removeMedia(detail)} loading={busy === detail.id}>
                Xóa ảnh
              </Button>
            ) : null}
          </>
        }
      >
        {detail ? (
          <div className="stack">
            <img
              src={`/api/pipelines/${workspace.pipelineId}/media/${detail.id}/file`}
              alt={detail.original_name}
              style={{ width: '100%', borderRadius: 'var(--r-md)', maxHeight: '46vh', objectFit: 'contain' }}
              onError={(e) => {
                const target = e.currentTarget;
                target.src = '/placeholder-image.svg';
              }}
            />
            <div className="badge-row">
              <Badge tone={detail.state_tone}>{detail.state_label}</Badge>
              {detail.saved ? <Badge tone="info">Đã lưu</Badge> : null}
              {detail.locked ? <Badge tone="neutral">Đã khóa</Badge> : null}
            </div>
            <p className="hint">
              {detail.state === 'blocked'
                ? `Không đạt (${detail.reason})`
                : detail.reason}
            </p>
            {detail.state === 'ready' && detail.description ? (
              <p className="hint">{detail.description}</p>
            ) : null}
            {detail.tags.length > 0 ? (
              <div className="tag-list">
                {detail.tags.map((tag) => (
                  <Badge key={tag} tone="info">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
            {can('media.edit') ? (
              <div className="btn-row">
                {detail.can_be_usable ? (
                  <Button
                    size="sm"
                    variant={detail.usable ? 'default' : 'primary'}
                    loading={busy === detail.id}
                    onClick={() => void patchMedia(detail, { usable: !detail.usable })}
                  >
                    {detail.usable ? 'Bỏ cho phép dùng' : 'Cho phép dùng'}
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  loading={busy === detail.id}
                  onClick={() => void patchMedia(detail, { locked: !detail.locked })}
                >
                  {detail.locked ? 'Mở khóa' : 'Tạm khóa'}
                </Button>
                <Button
                  size="sm"
                  loading={busy === detail.id}
                  onClick={() => void patchMedia(detail, { saved: !detail.saved })}
                >
                  {detail.saved ? 'Bỏ lưu' : 'Lưu cho lịch sau'}
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
