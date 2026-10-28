import { useRef, useState } from 'react';
import { useAuth } from '@/app/AuthProvider';
import { api } from '@/services/apiClient';
import { useAsync } from '@/hooks/useAsync';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Alert, Card, ErrorState, Skeleton, Tabs } from '@/components/ui/Surfaces';
import type { FacebookAccount } from '@/types/api';
import type { CycleConfig, CycleLibrary, CycleReport, CycleSettings } from './types';
import { CycleSettingsForm } from './CycleSettingsForm';
import { CycleLibraryPanel } from './CycleLibraryPanel';
import { CycleReports } from './CycleReports';
import { CycleGroups } from './CycleGroups';
import { CycleAccountLimits } from './CycleAccountLimits';
import './cycles.css';

export function CycleWorkspace({ pipelineId, accounts, config, onChanged }: {
  pipelineId: number; accounts: FacebookAccount[]; config: CycleConfig; onChanged: () => void;
}) {
  const { isAdmin } = useAuth(); const { push } = useToast();
  const [settings,setSettings] = useState(config.settings);
  const [dirty,setDirty] = useState(false); const [tab,setTab] = useState('config');
  const [busy,setBusy] = useState<'save' | 'schedule' | null>(null); const [error,setError] = useState<string | null>(null);
  const requestKey = useRef<string | null>(null);
  const data = useAsync(async signal => {
    const [library,reports] = await Promise.all([
      api.get<CycleLibrary>(`/api/pipelines/${pipelineId}/auto-post/library`,signal),
      api.get<{cycles: CycleReport[]}>(`/api/pipelines/${pipelineId}/auto-post/cycles`,signal),
    ]);
    return { library, cycles: reports.cycles };
  },[pipelineId]);
  function changed(patch: Partial<CycleSettings>) { setSettings(v => ({ ...v,...patch }));setDirty(true); }
  function reload() { data.reload();onChanged(); }
  async function save() {
    setBusy('save');setError(null);
    try {
      await api.put(`/api/pipelines/${pipelineId}/auto-post/cycle-config`,settings);
      setDirty(false);push('Đã lưu cấu hình chung.','success');reload();
    } catch (e) { setError(e instanceof Error ? e.message : 'Chưa lưu được cấu hình.'); }
    finally { setBusy(null); }
  }
  async function schedule() {
    setBusy('schedule');setError(null);
    requestKey.current ??= crypto.randomUUID();
    try {
      const result = await api.post<{cycle_id: number}>(`/api/pipelines/${pipelineId}/auto-post/cycles`,{ idempotency_key: requestKey.current });
      push(`Đã lưu chu kỳ #${result.cycle_id}; chưa xuất bản bài.`, 'success');requestKey.current = null;reload();setTab('reports');
    } catch (e) { setError(e instanceof Error ? e.message : 'Chưa xếp lịch được.'); }
    finally { setBusy(null); }
  }
  if (data.loading && !data.data) return <Skeleton variant="block" />;
  if (data.error || !data.data) return <ErrorState title="Chưa tải được cấu hình và báo cáo" description={data.error} action={<Button onClick={data.reload}>Thử lại</Button>} />;
  return <div className="stack cycle-workspace">
    <Card title="Một cấu hình cho toàn Workspace"><div className="stack">
      <p className="hint">Tự chọn tài khoản đủ quyền cho mỗi Group. Giới hạn của Admin luôn được áp dụng.</p>
      <div className="cycle-overview" aria-label="Dự kiến theo cấu hình đã lưu">
        <span>{config.overview.selected_accounts} tài khoản đã chọn</span><span>Group đủ điều kiện: {config.overview.eligible_groups ?? 'Chưa xác minh'}</span>
        <span>{config.overview.disabled_groups} Group bị vô hiệu hóa</span><span>Job dự kiến (tối đa): {config.overview.expected_jobs ?? 'Chưa lập kế hoạch'}</span>
      </div>
      {dirty ? <Alert tone="warn">Có thay đổi chưa lưu. Lưu cấu hình để cập nhật dự kiến và xếp lịch.</Alert> : null}
      {error ? <Alert tone="bad">{error}</Alert> : null}
      <div className="cycle-actions"><Button loading={busy === 'save'} disabled={busy !== null} onClick={() => void save()}>Lưu cấu hình</Button><Button variant="primary" loading={busy === 'schedule'} disabled={busy !== null || dirty || !settings.enabled || !config.configured} onClick={() => void schedule()}>Xếp lịch ngay</Button><Button disabled={busy !== null} onClick={reload}>Làm mới</Button></div>
    </div></Card>
    <Tabs tabs={[{id:'config',label:'Cấu hình'},{id:'groups',label:'Facebook Groups'},{id:'library',label:'Albums và Caption'},{id:'reports',label:'Lịch và báo cáo'}]} active={tab} onChange={setTab} />
    {tab === 'config' ? <div className="stack"><Card title="Auto Posting"><CycleSettingsForm settings={settings} onChange={changed} accounts={accounts} config={config} library={data.data.library} /></Card>{isAdmin ? <details><summary>Hạn mức Admin</summary><CycleAccountLimits pipelineId={pipelineId} onChanged={reload} /></details> : null}</div> : null}
    {tab === 'groups' ? <CycleGroups pipelineId={pipelineId} groups={config.overview.groups} accounts={accounts} isAdmin={isAdmin} onChanged={reload} /> : null}
    {tab === 'library' ? <CycleLibraryPanel pipelineId={pipelineId} library={data.data.library} onChanged={data.reload} /> : null}
    {tab === 'reports' ? <CycleReports cycles={data.data.cycles} /> : null}
  </div>;
}
