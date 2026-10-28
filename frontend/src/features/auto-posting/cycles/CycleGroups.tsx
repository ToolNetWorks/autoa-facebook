import { useState } from 'react';
import { api } from '@/services/apiClient';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Alert, Badge, Card } from '@/components/ui/Surfaces';
import type { FacebookAccount } from '@/types/api';
import type { GroupState } from './types';

const STATES: Record<string,string> = { ACTIVE:'Đã xác minh',DISABLED:'Group Bị Vô Hiệu Hoá',CHECK_PENDING:'Chờ kiểm tra',UNKNOWN:'Chưa xác minh',ACCESS_ERROR:'Lỗi truy cập' };
const PAIRS: Record<string,string> = { ...STATES,NOT_JOINED:'Chưa tham gia',SESSION_INVALID:'Cần kiểm tra phiên Facebook',RESTRICTED:'Tài khoản bị hạn chế',IDENTITY_UNAVAILABLE:'Danh tính đăng chưa khả dụng' };
export function CycleGroups({ pipelineId, groups, accounts, isAdmin, onChanged }: {
  pipelineId: number; groups: GroupState[]; accounts: FacebookAccount[]; isAdmin: boolean; onChanged: () => void;
}) {
  const [search,setSearch] = useState(''); const [account,setAccount] = useState(accounts[0]?.id ?? 0);
  const [editing,setEditing] = useState<number | null>(null); const [evidence,setEvidence] = useState('');
  const [busy,setBusy] = useState<number | null>(null); const [error,setError] = useState<string | null>(null);
  async function act(group: GroupState, check: boolean) {
    setBusy(group.id);setError(null);
    try {
      if (check) await api.post(`/api/pipelines/${pipelineId}/auto-post/groups/${group.id}/check`,{ account_ids: [account] });
      else await api.put(`/api/pipelines/${pipelineId}/auto-post/groups/${group.id}/global-status`,{ state: group.global_state === 'DISABLED' ? 'ACTIVE' : 'DISABLED', evidence });
      setEditing(null);setEvidence('');onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : 'Chưa cập nhật được Group.'); }
    finally { setBusy(null); }
  }
  return <div className="stack">
    <Input label="Tìm Group" value={search} onChange={e => setSearch(e.target.value)} />
    <Select label="Tài khoản dùng để kiểm tra quyền" value={account} onChange={e => setAccount(Number(e.target.value))}>{accounts.map(a => <option key={a.id} value={a.id}>{a.fb_name || a.label}</option>)}</Select>
    {error ? <Alert tone="bad">{error}</Alert> : null}
    {groups.filter(g => g.label.toLowerCase().includes(search.toLowerCase())).map(g => <Card key={g.id} title={g.label}><div className="stack">
      <Badge>{STATES[g.global_state] ?? 'Chưa xác minh'}</Badge><a href={g.url} target="_blank" rel="noreferrer">Mở Group</a>
      <p>{g.accounts.filter(a => a.state === 'ACTIVE').length} tài khoản có bằng chứng quyền đăng phù hợp.</p>
      <details><summary>Quyền của từng tài khoản</summary>{g.accounts.map(pair => <p key={pair.account_id}>{accounts.find(a => a.id === pair.account_id)?.label ?? 'Facebook'} · {PAIRS[pair.state] ?? 'Chưa xác minh'}</p>)}</details>
      <div className="cycle-actions"><Button disabled={busy !== null || !account} loading={busy === g.id} onClick={() => void act(g,true)}>Kiểm tra lại quyền</Button>{isAdmin ? <Button disabled={busy !== null} onClick={() => {setEditing(g.id);setEvidence('');}}>Xác nhận trạng thái Group</Button> : null}</div>
      {editing === g.id && isAdmin ? <div className="stack"><Textarea label="Bằng chứng xác nhận của Admin" value={evidence} onChange={e => setEvidence(e.target.value)} /><p className="hint">Lỗi truy cập của một tài khoản không đủ để kết luận Group bị vô hiệu hóa.</p><Button disabled={evidence.trim().length < 10 || busy !== null} onClick={() => void act(g,false)}>{g.global_state === 'DISABLED' ? 'Xác nhận Group hoạt động trở lại' : 'Xác nhận Group Bị Vô Hiệu Hoá'}</Button></div> : null}
    </div></Card>)}
  </div>;
}
