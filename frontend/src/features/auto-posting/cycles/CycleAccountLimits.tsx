import { useState } from 'react';
import { api } from '@/services/apiClient';
import { useAsync } from '@/hooks/useAsync';
import { Button } from '@/components/ui/Button';
import { Input, Select } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import { Alert, Card, ErrorState, Skeleton } from '@/components/ui/Surfaces';

interface Limit { account_id: number; label: string; daily_limit: number | null; min_gap_minutes: number | null; group_daily_limit: number | null; paused: boolean }
export function CycleAccountLimits({ pipelineId, onChanged }: { pipelineId: number; onChanged: () => void }) {
  const data=useAsync(signal => api.get<{limits: Limit[]}>(`/api/pipelines/${pipelineId}/auto-post/limits`,signal),[pipelineId]);
  const [draft,setDraft]=useState<Limit | null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState<string | null>(null);
  async function save() {
    if (!draft) return;
    setBusy(true);setError(null);
    try { await api.put(`/api/pipelines/${pipelineId}/auto-post/limits/${draft.account_id}`,{daily_limit:draft.daily_limit,min_gap_minutes:draft.min_gap_minutes,group_daily_limit:draft.group_daily_limit,paused:draft.paused});data.reload();onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : 'Chưa lưu được hạn mức.'); }
    finally { setBusy(false); }
  }
  if (data.loading && !data.data) return <Skeleton variant="block" />;
  if (data.error) return <ErrorState title="Chưa tải được hạn mức Admin" description={data.error} />;
  return <Card title="Hạn mức do Admin quản lý"><div className="stack">
    <Select label="Facebook Account" value={draft?.account_id ?? ''} onChange={e => setDraft(data.data?.limits.find(a => a.account_id===Number(e.target.value)) ?? null)}><option value="">Chọn tài khoản</option>{data.data?.limits.map(a => <option key={a.account_id} value={a.account_id}>{a.label}</option>)}</Select>
    {error ? <Alert tone="bad">{error}</Alert> : null}
    {draft ? <div className="stack"><div className="cycle-form-grid">
      <Input label="Giới hạn bài/ngày" type="number" min={1} value={draft.daily_limit ?? ''} onChange={e => setDraft({...draft,daily_limit:Number(e.target.value)||null})} />
      <Input label="Khoảng cách tối thiểu (phút)" type="number" min={1} value={draft.min_gap_minutes ?? ''} onChange={e => setDraft({...draft,min_gap_minutes:Number(e.target.value)||null})} />
      <Input label="Giới hạn Group/ngày" type="number" min={1} value={draft.group_daily_limit ?? ''} onChange={e => setDraft({...draft,group_daily_limit:Number(e.target.value)||null})} />
    </div><Switch label="Tạm dừng tài khoản này" checked={draft.paused} onChange={paused => setDraft({...draft,paused})} /><Button loading={busy} disabled={busy} onClick={() => void save()}>Lưu hạn mức Admin</Button></div> : null}
  </div></Card>;
}
