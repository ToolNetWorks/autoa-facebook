import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, EmptyState, Skeleton, Tabs } from '@/components/ui/Surfaces';
import type { MediaAlbum, MediaAsset } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'usable', label: 'Khả dụng' },
  { id: 'review', label: 'Cần duyệt' },
  { id: 'saved', label: 'Đã lưu' },
];

export function AlbumSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [album, setAlbum] = useState<MediaAlbum | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [uploading, setUploading] = useState(false);
  const [detail, setDetail] = useState<MediaAsset | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function load() {
    if (!workspace.pipelineId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const result = await api.get<MediaAlbum>(`/api/pipelines/${workspace.pipelineId}/media`);
      setAlbum(result);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được album', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.pipelineId]);

  async function upload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const form = new FormData();
      Array.from(files).forEach((file) => form.append('files', file));
      const result = await api.upload<{ count: number }>(`/api/pipelines/${workspace.pipelineId}/media/upload`, form);
      push(`Đã tải ${result.count} ảnh, hệ thống đang phân tích`, 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được ảnh', 'error');
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = '';
    }
  }

  async function patchMedia(media: MediaAsset, body: Record<string, unknown>) {
    try {
      await api.patch(`/api/pipelines/${workspace.pipelineId}/media/${media.id}`, body);
      await load();
      setDetail(null);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được ảnh', 'error');
    }
  }

  async function removeMedia(media: MediaAsset) {
    try {
      await api.delete(`/api/pipelines/${workspace.pipelineId}/media/${media.id}`);
      push('Đã xóa ảnh', 'success');
      setDetail(null);
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được ảnh', 'error');
    }
  }

  const items = album?.media ?? [];
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return items.filter((item) => {
      if (filter === 'usable' && !(item.usable && !item.locked)) return false;
      if (filter === 'review' && item.safety_status === 'pass') return false;
      if (filter === 'saved' && !item.saved) return false;
      if (!needle) return true;
      return (
        item.original_name.toLowerCase().includes(needle) ||
        item.description.toLowerCase().includes(needle) ||
        item.tags.some((tag) => tag.toLowerCase().includes(needle))
      );
    });
  }, [items, filter, search]);

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Album ảnh</h2>
          <p className="hint">Kho ảnh riêng của Workspace này. Ảnh được phân tích trước khi dùng cho lịch đăng.</p>
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

      {uploading ? <Alert tone="info">Đang tải ảnh lên và chờ phân tích...</Alert> : null}

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
              ? items.length
              : items.filter((item) =>
                  entry.id === 'usable'
                    ? item.usable && !item.locked
                    : entry.id === 'review'
                      ? item.safety_status !== 'pass'
                      : item.saved,
                ).length,
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

      {!loading && visible.length === 0 ? (
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
              />
              <span className="meta">
                <span className="cell-title truncate">{item.original_name}</span>
                <span className="cell-sub">
                  {item.width}×{item.height} · {Math.round(item.size_bytes / 1024)} KB
                </span>
                <span className="badge-row">
                  <Badge tone={item.safety_status === 'pass' ? 'ok' : item.safety_status === 'blocked' ? 'bad' : 'warn'}>
                    {item.safety_status === 'pass'
                      ? 'An toàn'
                      : item.safety_status === 'blocked'
                        ? 'Bị chặn'
                        : 'Cần duyệt'}
                  </Badge>
                  {item.saved ? <Badge tone="info">Đã lưu</Badge> : null}
                  {item.locked ? <Badge tone="neutral">Đã khóa</Badge> : null}
                </span>
              </span>
            </button>
          ))}
        </div>
      ) : null}

      <Modal
        open={detail !== null}
        title={detail?.original_name ?? ''}
        description={detail?.description || 'Ảnh chưa được mô tả.'}
        onClose={() => setDetail(null)}
        footer={
          <>
            <Button onClick={() => setDetail(null)}>Đóng</Button>
            {detail ? (
              <Button variant="danger" onClick={() => void removeMedia(detail)}>
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
            />
            <div className="badge-row">
              <Badge tone={detail.safety_status === 'pass' ? 'ok' : detail.safety_status === 'blocked' ? 'bad' : 'warn'}>
                An toàn: {detail.safety_status}
              </Badge>
              <Badge tone="neutral">Dùng {detail.usage_count} lần</Badge>
              {detail.last_used_at ? (
                <Badge tone="neutral">Dùng lần cuối {new Date(detail.last_used_at).toLocaleString('vi-VN')}</Badge>
              ) : null}
            </div>
            {detail.tags.length > 0 ? (
              <div className="tag-list">
                {detail.tags.map((tag) => (
                  <Badge key={tag} tone="info">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
            <div className="btn-row">
              <Button
                size="sm"
                variant={detail.usable ? 'default' : 'primary'}
                onClick={() => void patchMedia(detail, { usable: !detail.usable })}
              >
                {detail.usable ? 'Bỏ cho phép dùng' : 'Cho phép dùng'}
              </Button>
              <Button size="sm" onClick={() => void patchMedia(detail, { locked: !detail.locked })}>
                {detail.locked ? 'Mở khóa' : 'Tạm khóa'}
              </Button>
              <Button size="sm" onClick={() => void patchMedia(detail, { saved: !detail.saved })}>
                {detail.saved ? 'Bỏ lưu' : 'Lưu cho lịch sau'}
              </Button>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
