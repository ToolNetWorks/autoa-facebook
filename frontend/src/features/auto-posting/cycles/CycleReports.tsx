import { useState } from 'react';
import { Badge, Card, EmptyState } from '@/components/ui/Surfaces';
import { Input, Select } from '@/components/ui/Field';
import type { CycleReport } from './types';
import { REASON, STATUS } from './types';

function when(value: string | null, timezone?: string) { return value ? new Date(value).toLocaleString('vi-VN',{timeZone:timezone}) : 'Chưa có'; }
export function CycleReports({ cycles }: { cycles: CycleReport[] }) {
  const [search,setSearch] = useState('');
  const [selected,setSelected] = useState<number | null>(null);
  if (!cycles.length) return <EmptyState title="Chưa có chu kỳ" description="Lưu cấu hình và xếp lịch để bắt đầu." />;
  return <div className="stack">
    <Input label="Tìm Group trong báo cáo" value={search} onChange={e => setSearch(e.target.value)} />
    <Select label="Chọn chu kỳ" value={selected ?? cycles[0].id} onChange={e => setSelected(Number(e.target.value))}>{cycles.map(c => <option key={c.id} value={c.id}>Chu kỳ #{c.id} · {when(c.due_at)}</option>)}</Select>
    {cycles.filter(c => c.id === (selected ?? cycles[0].id)).map(c => <Card key={c.id} title={`Chu kỳ #${c.id} · ${when(c.due_at,c.timezone)}`}><div className="stack">
      <dl className="cycle-summary">
        {([['Tổng Group',c.summary.total_groups],['Đủ điều kiện',c.summary.eligible_groups],['Đã xác minh đăng',c.summary.posted],['Chờ duyệt',c.summary.pending_approval],['Chưa xác minh',c.summary.submitted_unverified],['Cần đối chiếu',c.summary.needs_review],['Group Bị Vô Hiệu Hoá',c.summary.disabled_groups],['Bỏ qua',c.summary.skipped],['Thất bại',c.summary.failed],['Không có account phù hợp',c.summary.no_eligible_account]] as [string,number][]).map(([label,n]) => <div key={label}><dt>{label}</dt><dd>{n}</dd></div>)}
      </dl>
      <p className="hint">Lượt tiếp theo: {when(c.next_run_at,c.timezone)} · {c.timezone}</p>
      {c.excluded_groups.filter(g => `${g.group_name || g.label}`.toLowerCase().includes(search.toLowerCase())).map(g => <details key={`excluded-${g.id}`} className="cycle-report-row">
        <summary><span>{g.group_name || g.label}</span><Badge>Group Bị Vô Hiệu Hoá</Badge></summary>
        <p>Không đưa vào chu kỳ; chưa tạo job. <a href={g.url} target="_blank" rel="noreferrer">Mở Group</a></p>
      </details>)}
      {c.items.filter(i => `${i.group_name || i.group_label}`.toLowerCase().includes(search.toLowerCase())).map(i => <details className="cycle-report-row" key={i.id}>
        <summary><span>{i.group_name || i.group_label}</span><Badge>{STATUS[i.status] ?? 'Cần kiểm tra'}</Badge></summary>
        <div className="stack">
          <a href={i.group_url} target="_blank" rel="noreferrer">Mở Group</a>
          <p>{i.account_name || i.account_label || 'Chưa phân công tài khoản'} · {when(i.started_at || i.scheduled_for,c.timezone)}</p>
          <p className="cycle-caption">{i.caption || 'Chưa chuẩn bị nội dung'}</p>
          {i.media_id ? <figure><img className="cycle-report-image" loading="lazy" src={`/api/pipelines/${c.pipeline_id ?? ''}/media/${i.media_id}/thumbnail`} alt={i.media_name} /><figcaption>{i.media_name}</figcaption></figure> : null}
          <p>Auto Job {i.auto_job_id ?? 'chưa tạo'} · Job {i.job_id ?? 'chưa tạo'} · Post {i.post_id ?? 'chưa tạo'}</p>
          {i.reason_code ? <p>{REASON[i.reason_code] ?? 'Cần kiểm tra lại tài khoản, Group hoặc nội dung.'} <small>Mã: {i.reason_code}</small></p> : null}
          {i.job_id && i.evidence ? <a href={`/api/evidence/${i.job_id}`} target="_blank" rel="noreferrer">Ảnh bằng chứng</a> : null}
          {i.status === 'posted' && i.permalink ? <a href={i.permalink} target="_blank" rel="noreferrer">Xem bài đã xác minh</a> : null}
        </div>
      </details>)}
    </div></Card>)}
  </div>;
}
