import type { FacebookAccount } from '@/types/api';
import { Input, Select } from '@/components/ui/Field';
import { Switch } from '@/components/ui/Choice';
import type { CycleConfig, CycleLibrary, CycleSettings } from './types';

export function CycleSettingsForm({ settings, onChange, accounts, config, library }: {
  settings: CycleSettings; onChange: (patch: Partial<CycleSettings>) => void;
  accounts: FacebookAccount[]; config: CycleConfig; library: CycleLibrary;
}) {
  function toggle(key: 'account_ids' | 'group_ids', id: number, all: number[]) {
    const current = settings[key].length ? settings[key] : all;
    const next = current.includes(id) ? current.filter(x => x !== id) : [...current,id];
    if (next.length) onChange({ [key]: next });
  }
  return <div className="stack">
    <Switch checked={settings.enabled} onChange={v => onChange({ enabled: v })} label="Bật Auto Posting theo chu kỳ" />
    <div className="cycle-form-grid">
      <Select label="Album sử dụng" value={settings.album_id ?? ''} onChange={e => onChange({ album_id: Number(e.target.value) || null, media_ids: [] })}>
        <option value="">Tất cả ảnh đủ điều kiện trong Workspace</option>
        {library.albums.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
      </Select>
      <Select label="Nguồn caption" value={settings.caption_mode} onChange={e => onChange({ caption_mode: e.target.value as CycleSettings['caption_mode'] })}>
        <option value="template">Caption mẫu đã duyệt</option><option value="ai">AI theo ngữ cảnh nhóm</option>
      </Select>
      <Select label="Caption mẫu" value={settings.template_ids[0] ?? ''} onChange={e => onChange({ template_ids: e.target.value ? [Number(e.target.value)] : [] })}>
        <option value="">Chọn mẫu</option>{library.templates.filter(t => t.approved).map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
      </Select>
      <Select label="Danh tính đăng yêu cầu" value={settings.posting_mode} onChange={e => onChange({ posting_mode: e.target.value as CycleSettings['posting_mode'] })}>
        <option value="anonymous">Ẩn danh</option><option value="nickname">Biệt danh đã xác nhận</option>
      </Select>
      <Input label="Group mỗi chu kỳ" type="number" min={1} value={settings.groups_per_cycle} onChange={e => onChange({ groups_per_cycle: Number(e.target.value) })} />
      <Input label="Chu kỳ lặp lại (phút)" type="number" min={1} value={settings.cycle_interval_minutes} onChange={e => onChange({ cycle_interval_minutes: Number(e.target.value) })} />
    </div>
    <details><summary>Chọn tài khoản và Group</summary><div className="cycle-form-grid">
      <fieldset><legend>Facebook Accounts ({settings.account_ids.length || accounts.length})</legend>
        <label className="cycle-check"><input type="checkbox" checked={!settings.account_ids.length} onChange={() => onChange({ account_ids: [] })} />Tất cả tài khoản trong Workspace</label>
        {accounts.map(a => <label className="cycle-check" key={a.id}><input type="checkbox" checked={!settings.account_ids.length || settings.account_ids.includes(a.id)} onChange={() => toggle('account_ids',a.id,accounts.map(x => x.id))} />{a.fb_name || a.label}</label>)}
      </fieldset>
      <fieldset><legend>Facebook Groups ({settings.group_ids.length || config.overview.groups.length})</legend>
        <label className="cycle-check"><input type="checkbox" checked={!settings.group_ids.length} onChange={() => onChange({ group_ids: [] })} />Tất cả Group trong Workspace</label>
        {config.overview.groups.map(g => <label className="cycle-check" key={g.id}><input type="checkbox" checked={!settings.group_ids.length || settings.group_ids.includes(g.id)} onChange={() => toggle('group_ids',g.id,config.overview.groups.map(x => x.id))} />{g.label}{g.global_state === 'DISABLED' ? ' · Group Bị Vô Hiệu Hoá' : ''}</label>)}
      </fieldset>
    </div></details>
    <details><summary>Lịch đăng và tùy chọn nâng cao</summary><div className="stack">
      <div className="cycle-form-grid">
        <Input label="Từ giờ" type="time" value={settings.start_time} onChange={e => onChange({ start_time: e.target.value })} />
        <Input label="Đến giờ" type="time" value={settings.end_time} onChange={e => onChange({ end_time: e.target.value })} />
        <Input label="Timezone" value={settings.timezone} onChange={e => onChange({ timezone: e.target.value })} />
        <Input label="Khoảng cách bài đăng (phút)" type="number" min={1} value={settings.post_interval_minutes} onChange={e => onChange({ post_interval_minutes: Number(e.target.value) })} />
        <Input label="Giới hạn mỗi account/ngày" type="number" min={1} value={settings.per_account_daily_limit} onChange={e => onChange({ per_account_daily_limit: Number(e.target.value) })} />
        <Input label="Chủ đề cho AI" value={settings.topic} onChange={e => onChange({ topic: e.target.value })} />
        <Select label="Mẫu dự phòng khi AI lỗi" value={settings.fallback_template_id ?? ''} onChange={e => onChange({ fallback_template_id: Number(e.target.value) || null })}>
          <option value="">Dừng và báo lỗi</option>{library.templates.filter(t => t.approved).map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
        </Select>
      </div>
      <fieldset><legend>Ngày hoạt động</legend><div className="cycle-days">{['Thứ hai','Thứ ba','Thứ tư','Thứ năm','Thứ sáu','Thứ bảy','Chủ nhật'].map((name,i) => <label className="cycle-check" key={name}><input type="checkbox" checked={settings.days.includes(i)} onChange={() => onChange({ days: settings.days.includes(i) ? settings.days.filter(d => d !== i) : [...settings.days,i] })} />{name}</label>)}</div></fieldset>
      <Switch checked={settings.repeat_media} onChange={v => onChange({ repeat_media: v })} label="Bắt đầu vòng ảnh mới khi dùng hết" />
      <Switch checked={settings.unique_captions} onChange={v => onChange({ unique_captions: v })} label="Yêu cầu caption khác nhau giữa các bài" />
      <Switch checked={settings.require_approval} onChange={v => onChange({ require_approval: v })} label="Duyệt nội dung trước khi worker chạy" />
      <details><summary>Chỉ dùng ảnh chỉ định</summary><div className="cycle-media">{library.media.filter(m => m.eligible && (!settings.album_id || library.albums.find(a => a.id === settings.album_id)?.media_ids.includes(m.id))).map(m => <label className="cycle-check" key={m.id}><input type="checkbox" checked={settings.media_ids.includes(m.id)} onChange={() => onChange({ media_ids: settings.media_ids.includes(m.id) ? settings.media_ids.filter(id => id !== m.id) : [...settings.media_ids,m.id] })} />{m.name}</label>)}</div><p className="hint">Để trống để xoay vòng tất cả ảnh đủ điều kiện trong Album.</p></details>
    </div></details>
  </div>;
}
