import type { AnalyticsKpis, AnalyticsPoint } from '@/types/api';
import { Badge, Card } from '@/components/ui/Surfaces';

/** A number that may not have been measured yet. */
export function measured(value: number | null | undefined, suffix = ''): { text: string; known: boolean } {
  if (value === null || value === undefined) return { text: 'Chưa có dữ liệu', known: false };
  return { text: formatNumber(value) + suffix, known: true };
}

export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return '0';
  if (Math.abs(value) >= 1000000) return (value / 1000000).toFixed(1).replace('.0', '') + 'M';
  if (Math.abs(value) >= 1000) return (value / 1000).toFixed(1).replace('.0', '') + 'K';
  return String(value);
}

interface KpiProps {
  label: string;
  value: number | null | undefined;
  hint?: string;
  tone?: 'plain' | 'good' | 'warn' | 'bad';
}

/** One small number of a report. */
export function Kpi({ label, value, hint, tone = 'plain' }: KpiProps) {
  const number = measured(value);
  return (
    <div className={`kpi kpi-${tone}`}>
      <span className="kpi-label">{label}</span>
      <span className={`kpi-value${number.known ? '' : ' kpi-unknown'}`}>{number.text}</span>
      {hint ? <span className="kpi-hint">{hint}</span> : null}
    </div>
  );
}

/** The row of numbers at the top of a report. */
export function KpiRow({ kpis }: { kpis: AnalyticsKpis }) {
  return (
    <div className="kpi-row">
      <Kpi label="Bài đã đăng" value={kpis.posts_published} hint={`${kpis.group_posts_posted} group · ${kpis.reels_posted} reel`} />
      <Kpi label="Tương tác đo được" value={kpis.engagement} hint={kpis.engagement_available ? `${kpis.measured_posts} bài đã đo` : 'chưa đo được chỉ số nào'} />
      <Kpi label="Lượt xem reel" value={kpis.views} hint={kpis.views_available ? 'lượt xem đã đo' : 'chưa có lượt xem'} />
      <Kpi label="Chờ duyệt" value={kpis.posts_waiting} tone={kpis.posts_waiting > 0 ? 'warn' : 'plain'} />
      <Kpi label="Bị từ chối / gỡ" value={kpis.posts_refused + kpis.posts_removed} tone={kpis.posts_refused + kpis.posts_removed > 0 ? 'bad' : 'plain'} />
    </div>
  );
}

/** The trend of the published posts and the measured interaction. */
export function TrendChart({ points, height = 190 }: { points: AnalyticsPoint[]; height?: number }) {
  if (!points.length) {
    return (
      <Card title="Xu hướng">
        <p className="hint">Chưa có dữ liệu theo ngày để vẽ biểu đồ.</p>
      </Card>
    );
  }
  const width = 720;
  const padding = { top: 16, right: 16, bottom: 26, left: 38 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const maxEngagement = Math.max(1, ...points.map((point) => point.engagement));
  const maxPosts = Math.max(1, ...points.map((point) => point.posts + point.reels));
  const step = points.length > 1 ? innerWidth / (points.length - 1) : 0;
  const x = (index: number) => padding.left + index * step;
  const yEngagement = (value: number) => padding.top + innerHeight - (value / maxEngagement) * innerHeight;
  const yPosts = (value: number) => padding.top + innerHeight - (value / maxPosts) * innerHeight * 0.42;

  const line = points.map((point, index) => `${index === 0 ? 'M' : 'L'}${x(index)},${yEngagement(point.engagement)}`).join(' ');
  const area = `${line} L${x(points.length - 1)},${padding.top + innerHeight} L${x(0)},${padding.top + innerHeight} Z`;
  const labelEvery = Math.max(1, Math.ceil(points.length / 6));

  return (
    <Card title="Xu hướng" description="Số bài đăng và tương tác đo được theo ngày.">
      <svg viewBox={`0 0 ${width} ${height}`} className="chart" role="img" aria-label="Biểu đồ xu hướng">
        <defs>
          <linearGradient id="analyticsFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 0.5, 1].map((ratio) => (
          <line
            key={ratio}
            x1={padding.left}
            x2={width - padding.right}
            y1={padding.top + innerHeight * ratio}
            y2={padding.top + innerHeight * ratio}
            className="chart-grid"
          />
        ))}
        {points.map((point, index) => (
          <rect
            key={`bar-${point.day}`}
            x={x(index) - Math.min(6, step / 3)}
            y={yPosts(point.posts + point.reels)}
            width={Math.max(3, Math.min(12, step / 2))}
            height={padding.top + innerHeight - yPosts(point.posts + point.reels)}
            className="chart-bar"
          />
        ))}
        <path d={area} fill="url(#analyticsFill)" />
        <path d={line} className="chart-line" />
        {points.map((point, index) =>
          index % labelEvery === 0 || index === points.length - 1 ? (
            <text key={`label-${point.day}`} x={x(index)} y={height - 8} className="chart-label" textAnchor="middle">
              {point.day.slice(5)}
            </text>
          ) : null,
        )}
      </svg>
      <div className="chart-legend">
        <span className="chart-legend-item">
          <span className="chart-swatch chart-swatch-line" /> Tương tác đo được
        </span>
        <span className="chart-legend-item">
          <span className="chart-swatch chart-swatch-bar" /> Bài đăng
        </span>
        <Badge tone="neutral">Đỉnh {formatNumber(maxEngagement)}</Badge>
      </div>
    </Card>
  );
}
