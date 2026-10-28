import { useState } from 'react';
import { api } from '@/services/apiClient';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Field';
import { Alert, Card } from '@/components/ui/Surfaces';
import type { CycleLibrary } from './types';

export function CycleLibraryPanel({ pipelineId, library, onChanged }: { pipelineId: number; library: CycleLibrary; onChanged: () => void }) {
  const [album,setAlbum] = useState(''); const [media,setMedia] = useState<number[]>([]);
  const [name,setName] = useState(''); const [body,setBody] = useState(''); const [approved,setApproved] = useState(false);
  const [busy,setBusy] = useState<'album' | 'template' | null>(null); const [error,setError] = useState<string | null>(null);
  async function save(kind: 'album' | 'template') {
    setBusy(kind); setError(null);
    try {
      await api.post(`/api/pipelines/${pipelineId}/auto-post/${kind === 'album' ? 'albums' : 'templates'}`,kind === 'album' ? { name: album, media_ids: media } : { name, body, approved });
      if (kind === 'album') { setAlbum('');setMedia([]); } else { setName('');setBody('');setApproved(false); }
      onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : 'Chưa lưu được nội dung.'); }
    finally { setBusy(null); }
  }
  return <div className="stack">
    {error ? <Alert tone="bad">{error}</Alert> : null}
    <Card title="Albums của Workspace"><div className="stack">
      {library.albums.map(a => <p key={a.id}>{a.name} · {a.media_ids.length} ảnh</p>)}
      <details><summary>Tạo Album từ thư viện ảnh</summary><div className="stack">
        <Input label="Tên Album" value={album} onChange={e => setAlbum(e.target.value)} />
        <div className="cycle-media">{library.media.map(m => <label className="cycle-check" key={m.id}><input type="checkbox" checked={media.includes(m.id)} onChange={() => setMedia(v => v.includes(m.id) ? v.filter(id => id !== m.id) : [...v,m.id])} /><img width={48} height={48} loading="lazy" src={`/api/pipelines/${pipelineId}/media/${m.id}/thumbnail`} alt="" />{m.name}{!m.eligible ? ' · Chưa đủ điều kiện đăng' : ''}</label>)}</div>
        <Button disabled={!album.trim() || busy !== null} loading={busy === 'album'} onClick={() => void save('album')}>Lưu Album</Button>
      </div></details>
    </div></Card>
    <Card title="Caption mẫu"><div className="stack">
      {library.templates.map(t => <details key={t.id}><summary>{t.name} · {t.approved ? 'Đã duyệt' : 'Bản nháp'}</summary><p className="cycle-caption">{t.body}</p></details>)}
      <Input label="Tên mẫu" value={name} onChange={e => setName(e.target.value)} />
      <Textarea label="Nội dung mẫu" value={body} onChange={e => setBody(e.target.value)} />
      <p className="hint">Có thể dùng {'{group_name}'}, {'{pipeline_name}'}, {'{image_description}'} và {'{cycle_date}'}.</p>
      <label className="cycle-check"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)} />Tôi đã duyệt nội dung mẫu này</label>
      <Button disabled={!name.trim() || !body.trim() || busy !== null} loading={busy === 'template'} onClick={() => void save('template')}>Lưu caption mẫu</Button>
    </div></Card>
  </div>;
}
