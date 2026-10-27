import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '@/services/apiClient';
import { Badge, EmptyState, ErrorState, Skeleton } from '@/components/ui/Surfaces';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

import { getPostLink, mapJobStatus, matchesFilter } from './jobPresentation';
import type { AutoPostJobItem, FilterCategory } from './jobPresentation';
export { getPostLink, mapJobStatus, matchesFilter } from './jobPresentation';
export type { AutoPostJobItem, FilterCategory } from './jobPresentation';

function formatDate(iso?: string | null): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso.replace('T', ' ').slice(0, 16);
    const day = String(d.getDate()).padStart(2, '0');
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const year = d.getFullYear();
    const hours = String(d.getHours()).padStart(2, '0');
    const minutes = String(d.getMinutes()).padStart(2, '0');
    return `${day}/${month}/${year} ${hours}:${minutes}`;
  } catch {
    return iso.replace('T', ' ').slice(0, 16);
  }
}

export function AutoPostJobList({
  pipelineId,
  initialFilter = 'all',
  onCountsUpdate,
}: {
  pipelineId: number;
  initialFilter?: FilterCategory;
  onCountsUpdate?: (counts: Record<string, number>) => void;
}) {
  const [jobs, setJobs] = useState<AutoPostJobItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({
    all: 0,
    waiting: 0,
    processing: 0,
    needs_review: 0,
    posted: 0,
    failed: 0,
  });
  const [activeFilter, setActiveFilter] = useState<FilterCategory>(initialFilter);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedJob, setSelectedJob] = useState<AutoPostJobItem | null>(null);
  const [page, setPage] = useState(1);
  const pageSize = 15;
  const activeRequest = useRef<AbortController | null>(null);

  async function fetchJobs(isSilent = false) {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    if (!isSilent) setRefreshing(true);
    try {
      const resp = await api.get<{ jobs: AutoPostJobItem[]; counts: Record<string, number> }>(
        `/api/pipelines/${pipelineId}/auto-post/history?limit=300`, controller.signal,
      );
      if (controller.signal.aborted) return;
      if (!Array.isArray(resp.jobs) || !resp.counts) throw new Error('Invalid history response');
      setLoadError(false);
      setJobs((resp.jobs || []).map(job => ({ ...job,
        status: ['posted','published'].includes(job.status) && (!getPostLink(job) || getPostLink(job).includes('/pending_posts/')) ? 'submitted_unverified' : job.status,
      })));
      if (resp.counts) {
        setCounts(resp.counts);
        onCountsUpdate?.(resp.counts);
      }
    } catch {
      if (controller.signal.aborted) return;
      setLoadError(true);
    } finally {
      if (!controller.signal.aborted) { setLoading(false); setRefreshing(false); }
    }
  }

  // Sync initialFilter prop
  useEffect(() => {
    setActiveFilter(initialFilter);
    setPage(1);
  }, [initialFilter]);

  // Initial load
  useEffect(() => {
    setLoading(true);
    setJobs([]);
    setLoadError(false);
    void fetchJobs(false);
    return () => activeRequest.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  // Periodic polling every 15 seconds
  useEffect(() => {
    const timer = setInterval(() => {
      void fetchJobs(true);
    }, 15000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  // Filtered jobs
  const filteredJobs = useMemo(() => {
    return jobs.filter((j) => matchesFilter(j, activeFilter));
  }, [jobs, activeFilter]);

  // Pagination
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const paginatedJobs = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredJobs.slice(start, start + pageSize);
  }, [filteredJobs, currentPage]);

  const filterTabs: { id: FilterCategory; label: string; count: number }[] = [
    { id: 'all', label: 'Tất cả', count: counts.all ?? jobs.length },
    { id: 'waiting', label: 'Đang chờ', count: counts.waiting ?? 0 },
    { id: 'processing', label: 'Đang xử lý', count: counts.processing ?? 0 },
    { id: 'needs_review', label: 'Cần xem lại', count: counts.needs_review ?? 0 },
    { id: 'posted', label: 'Đã đăng', count: counts.posted ?? 0 },
    { id: 'failed', label: 'Thất bại', count: counts.failed ?? 0 },
  ];

  return (
    <div className="stack">
      {/* Filter and actions bar */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
        <div className="filter-bar" role="tablist">
          {filterTabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeFilter === tab.id}
              className={`filter-btn ${activeFilter === tab.id ? 'active' : ''}`}
              onClick={() => {
                setActiveFilter(tab.id);
                setPage(1);
              }}
            >
              <span>{tab.label}</span>
              <span className="filter-badge">{loading || loadError ? '—' : tab.count}</span>
            </button>
          ))}
        </div>

        <Button
          size="sm"
          variant="default"
          loading={refreshing}
          onClick={() => void fetchJobs(false)}
          title="Tự động cập nhật mỗi 15 giây"
        >
          Làm mới
        </Button>
      </div>

      {/* Loading Skeleton */}
      {loadError ? <ErrorState title="Chưa tải được lịch sử đăng bài" description="Vui lòng thử lại." action={<Button onClick={() => void fetchJobs()}>Thử lại</Button>} /> : null}
      {loading ? (
        <div className="stack" style={{ gap: '12px' }}>
          <Skeleton variant="block" />
          <Skeleton variant="block" />
          <Skeleton variant="block" />
        </div>
      ) : loadError ? null : paginatedJobs.length === 0 ? (
        <EmptyState
          title={
            activeFilter === 'all'
              ? 'Chưa có bài đăng nào'
              : `Không có bài đăng ở trạng thái "${filterTabs.find((t) => t.id === activeFilter)?.label}"`
          }
          description="Bấm “Xếp lịch ngay” để tự động chuẩn bị các bài đăng theo lịch của Workspace."
        />
      ) : (
        <>
          <div className="job-card-grid">
            {paginatedJobs.map((job) => {
              const st = mapJobStatus(job.status);
              const displayTime = formatDate(job.scheduled_for || job.created_at);
              const isPosted = job.status === 'posted' || job.status === 'published';
              const postLink = getPostLink(job);

              return (
                <div
                  key={job.id}
                  className="job-card-item"
                  onClick={() => setSelectedJob(job)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      setSelectedJob(job);
                    }
                  }}
                >
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'flex-start' }}>
                    {/* Thumbnail */}
                    <div className="job-thumb">
                      {job.media_id ? (
                        <img
                          src={`/api/pipelines/${pipelineId}/media/${job.media_id}/thumbnail`}
                          alt=""
                          loading="lazy"
                          onError={(e) => {
                            (e.currentTarget as HTMLElement).style.display = 'none';
                          }}
                        />
                      ) : (
                        <div className="job-thumb-fallback">IMG</div>
                      )}
                    </div>

                    {/* Main content */}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                          <strong style={{ fontSize: 'var(--fs-13)' }}>{job.account_label || `Facebook #${job.account_id || ''}`}</strong>
                          <span style={{ color: 'var(--text-subtle)', fontSize: 'var(--fs-12)' }}>→</span>
                          <span style={{ fontSize: 'var(--fs-13)', color: 'var(--text-muted)' }}>{job.group_label || `Group #${job.group_id || ''}`}</span>
                        </div>
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </div>

                      <p className="hint">Auto Job #{job.id} · {job.post_id ? `Post #${job.post_id}` : 'Chưa tạo Post'}</p>
                      {/* Shortened Caption */}
                      <p className="job-caption-clamp" style={{ margin: '0 0 6px 0' }}>
                        {job.caption || '—'}
                      </p>

                      {/* Meta line */}
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', flexWrap: 'wrap', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', marginTop: '4px' }}>
                        <span>
                          {job.scheduled_for ? 'Lịch dự kiến: ' : 'Thời điểm tạo: '}
                          <strong>{displayTime}</strong>
                        </span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {isPosted || postLink ? (
                            <a
                              href={postLink || '#'}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{
                                padding: '3px 10px',
                                fontSize: 'var(--fs-11)',
                                background: '#16a34a',
                                color: '#fff',
                                textDecoration: 'none',
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '4px',
                                borderRadius: 'var(--r-sm, 6px)',
                                fontWeight: 600,
                              }}
                              onClick={(e) => {
                                e.stopPropagation();
                              }}
                              title="Mở link bài đăng Facebook"
                            >
                              Xem link bài ↗
                            </a>
                          ) : null}
                          {job.error ? (
                            <span style={{ color: 'var(--critical, #e11d48)', fontWeight: 500 }}>
                              {job.error.slice(0, 30)}...
                            </span>
                          ) : (
                            <span style={{ color: 'var(--accent)', fontWeight: 500 }}>Chi tiết →</span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="pagination-bar">
              <span>
                Hiển thị {(currentPage - 1) * pageSize + 1} – {Math.min(currentPage * pageSize, filteredJobs.length)} trong tổng số <strong>{filteredJobs.length}</strong> bài
              </span>
              <div style={{ display: 'flex', gap: '6px' }}>
                <Button
                  size="sm"
                  variant="default"
                  disabled={currentPage <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  Trước
                </Button>
                <Button
                  size="sm"
                  variant="default"
                  disabled={currentPage >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                >
                  Sau
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Detail Modal */}
      {selectedJob && (() => {
        const modalPostLink = getPostLink(selectedJob);
        const modalIsPosted = selectedJob.status === 'posted' || selectedJob.status === 'published';

        return (
          <Modal
            open={true}
            title="Chi tiết bài đăng"
            description={`Facebook: ${selectedJob.account_label || '—'} · Group: ${selectedJob.group_label || '—'}`}
            onClose={() => setSelectedJob(null)}
            footer={
              <>
                {modalPostLink ? (
                  <a
                    href={modalPostLink}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn sm"
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '6px',
                      textDecoration: 'none',
                      background: '#16a34a',
                      borderColor: '#16a34a',
                      color: '#fff',
                      fontWeight: 600,
                    }}
                  >
                    Xem link bài ↗
                  </a>
                ) : null}
                <Button size="sm" onClick={() => setSelectedJob(null)}>
                  Đóng
                </Button>
              </>
            }
          >
            <div className="stack" style={{ gap: '16px' }}>
              <p>Auto Job #{selectedJob.id} · {selectedJob.post_id ? (
                <Link className="link" to={`/workspace/${pipelineId}/group-post?post_id=${selectedJob.post_id}`}>Post #{selectedJob.post_id}</Link>
              ) : 'Chưa tạo Post'}</p>
              {/* Post link if available */}
              {(modalIsPosted || modalPostLink) && (
                <div style={{ background: 'rgba(22, 163, 74, 0.08)', border: '1px solid rgba(22, 163, 74, 0.25)', padding: '12px', borderRadius: 'var(--r-md)' }}>
                  <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', textTransform: 'uppercase', marginBottom: '4px', fontWeight: 600 }}>
                    Link bài đăng Facebook
                  </span>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', flexWrap: 'wrap' }}>
                    <a
                      href={modalPostLink}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: '#16a34a', fontWeight: 600, fontSize: 'var(--fs-13)', wordBreak: 'break-all' }}
                    >
                      {modalPostLink} ↗
                    </a>
                    <a
                      href={modalPostLink}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn sm"
                      style={{ background: '#16a34a', color: '#fff', textDecoration: 'none', padding: '4px 10px', fontSize: 'var(--fs-12)', fontWeight: 600 }}
                    >
                      Mở link ↗
                    </a>
                  </div>
                </div>
              )}
            {/* Status and Mode */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', flexWrap: 'wrap' }}>
              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', textTransform: 'uppercase', marginBottom: '2px' }}>
                  Trạng thái
                </span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <Badge tone={mapJobStatus(selectedJob.status).tone}>
                    {mapJobStatus(selectedJob.status).label}
                  </Badge>
                  <span style={{ fontSize: 'var(--fs-12)', color: 'var(--text-muted)' }}>
                    {mapJobStatus(selectedJob.status).description}
                  </span>
                </div>
              </div>

              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', textTransform: 'uppercase', marginBottom: '2px' }}>
                  Chế độ đăng
                </span>
                <span style={{ fontSize: 'var(--fs-13)', fontWeight: 600 }}>
                  {selectedJob.posting_mode === 'anonymous' ? 'Đăng ẩn danh' : selectedJob.nickname ? `Biệt danh (${selectedJob.nickname})` : 'Tên thật'}
                </span>
              </div>
            </div>

            {/* Timestamps */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', background: 'var(--surface-muted, #f8fafc)', padding: '12px', borderRadius: 'var(--r-md)' }}>
              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)' }}>Thời gian xếp lịch</span>
                <strong style={{ fontSize: 'var(--fs-13)' }}>{formatDate(selectedJob.scheduled_for || selectedJob.created_at)}</strong>
              </div>
              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)' }}>Số lần thử đăng</span>
                <strong style={{ fontSize: 'var(--fs-13)' }}>{selectedJob.attempts ?? 0} lần</strong>
              </div>
            </div>

            {/* Media thumbnail and Caption */}
            <div className="job-detail-grid">
              <div style={{ width: '120px', height: '120px', borderRadius: 'var(--r-md)', overflow: 'hidden', background: 'var(--border)' }}>
                {selectedJob.media_id ? (
                  <img
                    src={`/api/pipelines/${pipelineId}/media/${selectedJob.media_id}/thumbnail`}
                    alt=""
                    style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                  />
                ) : (
                  <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)' }}>
                    Không có ảnh
                  </div>
                )}
              </div>

              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', textTransform: 'uppercase', marginBottom: '4px' }}>
                  Nội dung Caption
                </span>
                <div style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--fs-13)', lineHeight: 1.5, background: 'var(--surface)', padding: '10px', borderRadius: 'var(--r-sm)', border: '1px solid var(--border)', maxHeight: '180px', overflowY: 'auto' }}>
                  {selectedJob.caption || '—'}
                </div>
              </div>
            </div>

            {/* Error or Defer Reason if present */}
            {(selectedJob.error || selectedJob.defer_reason) && (
              <div style={{ background: 'rgba(225, 29, 72, 0.08)', border: '1px solid rgba(225, 29, 72, 0.2)', padding: '12px', borderRadius: 'var(--r-md)' }}>
                <strong style={{ display: 'block', color: 'var(--critical, #e11d48)', fontSize: 'var(--fs-12)', marginBottom: '4px' }}>
                  Lý do / Thông tin phản hồi:
                </strong>
                <p style={{ margin: 0, fontSize: 'var(--fs-13)', color: 'var(--text)' }}>
                  {selectedJob.error || selectedJob.defer_reason}
                </p>
              </div>
            )}

            {/* Evidence if present */}
            {selectedJob.evidence ? (
              <div>
                <span style={{ display: 'block', fontSize: 'var(--fs-11)', color: 'var(--text-subtle)', textTransform: 'uppercase', marginBottom: '4px' }}>
                  Bằng chứng xác minh từ Facebook
                </span>
                <div style={{ fontSize: 'var(--fs-12)', color: 'var(--text-muted)', background: 'var(--surface-muted, #f8fafc)', padding: '8px 12px', borderRadius: 'var(--r-sm)' }}>
                  {selectedJob.evidence}
                </div>
              </div>
            ) : null}
          </div>
        </Modal>
      ); })()}
    </div>
  );
}
