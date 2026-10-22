import { useMemo, useState } from 'react';
import { Badge, Card, EmptyState } from '@/components/ui/Surfaces';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Field';
import type { GroupRow } from '@/types/api';

type ClassificationFilter = 'all' | 'no_approval' | 'needs_approval' | 'unclassified';

const CLASSIFICATION_FILTERS: { id: ClassificationFilter; label: string }[] = [
  { id: 'all', label: 'Tất cả' },
  { id: 'no_approval', label: 'Đăng Không Cần Duyệt' },
  { id: 'needs_approval', label: 'Cần Duyệt' },
  { id: 'unclassified', label: 'Chưa phân loại' },
];

const CLASSIFICATION_LABEL: Record<string, string> = {
  no_approval: 'Đăng Không Cần Duyệt',
  needs_approval: 'Cần Duyệt',
  unclassified: 'Chưa phân loại',
};

/**
 * Bộ chọn group của một chiến dịch.
 *
 * Danh sách có thể dài, nên nó cuộn trong một khung cố định thay vì kéo dài
 * trang. Người dùng chọn bằng ô tick, lọc theo Facebook hoặc theo phân loại,
 * và luôn thấy đã chọn bao nhiêu group.
 */
export function GroupSelector({
  groups,
  selected,
  onChange,
  maxGroups,
  busy,
}: {
  groups: GroupRow[];
  selected: number[];
  onChange: (ids: number[]) => void;
  maxGroups: number;
  busy: boolean;
}) {
  const [search, setSearch] = useState('');
  const [accountFilter, setAccountFilter] = useState<number | ''>('');
  const [classification, setClassification] = useState<ClassificationFilter>('all');

  const accounts = useMemo(() => {
    const seen = new Map<number, string>();
    groups.forEach((group) => {
      if (group.account_id && group.account_label) {
        seen.set(group.account_id, group.account_label);
      }
    });
    return [...seen.entries()];
  }, [groups]);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return groups.filter((group) => {
      if (accountFilter !== '' && group.account_id !== accountFilter) return false;
      if (classification !== 'all' && group.classification !== classification) return false;
      if (!needle) return true;
      return (
        group.label.toLowerCase().includes(needle) ||
        (group.account_label ?? '').toLowerCase().includes(needle)
      );
    });
  }, [groups, search, accountFilter, classification]);

  const visibleIds = visible.map((group) => group.id);
  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id));
  const overLimit = selected.length > maxGroups;

  function toggle(id: number) {
    if (selected.includes(id)) {
      onChange(selected.filter((item) => item !== id));
      return;
    }
    onChange([...selected, id]);
  }

  function toggleAllVisible() {
    if (allVisibleSelected) {
      const removed = new Set(visibleIds);
      onChange(selected.filter((id) => !removed.has(id)));
      return;
    }
    const merged = [...selected];
    visibleIds.forEach((id) => {
      if (!merged.includes(id)) merged.push(id);
    });
    onChange(merged);
  }

  if (groups.length === 0) {
    return (
      <EmptyState
        title="Chưa có group sẵn sàng"
        description="Thêm group và kiểm tra quyền đăng ở mục Nhóm Facebook."
      />
    );
  }

  return (
    <Card
      title="1. Chọn group"
      actions={
        <>
          <Button size="sm" onClick={() => onChange(groups.map((group) => group.id))}>
            Chọn tất cả
          </Button>
          <Button size="sm" onClick={() => onChange([])}>
            Bỏ chọn tất cả
          </Button>
        </>
      }
    >
      <div className="stack-sm">
        <div className="row" style={{ gap: 'var(--sp-2)', flexWrap: 'wrap' }}>
          <Input
            name="group_search"
            placeholder="Tìm group"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="grow"
          />
          <select
            name="group_account_filter"
            value={accountFilter}
            onChange={(event) =>
              setAccountFilter(event.target.value === '' ? '' : Number(event.target.value))
            }
            aria-label="Lọc theo Facebook"
          >
            <option value="">Mọi Facebook</option>
            {accounts.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </div>

        <div className="tabs" role="tablist">
          {CLASSIFICATION_FILTERS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={classification === entry.id}
              className={`tab ${classification === entry.id ? 'active' : ''}`}
              onClick={() => setClassification(entry.id)}
            >
              {entry.label}
              <span className="tab-count">
                {entry.id === 'all'
                  ? groups.length
                  : groups.filter((group) => group.classification === entry.id).length}
              </span>
            </button>
          ))}
        </div>

        <div className="row" style={{ gap: 'var(--sp-2)', alignItems: 'center' }}>
          <Button size="sm" onClick={toggleAllVisible} disabled={busy}>
            {allVisibleSelected ? 'Bỏ chọn danh sách này' : 'Chọn danh sách này'}
          </Button>
          <span className={overLimit ? 'hint' : 'hint'}>
            Đã chọn <b>{selected.length}</b> / {maxGroups} group
            {overLimit ? ' — vượt giới hạn, hãy bỏ bớt' : ''}
          </span>
        </div>

        <div className="choice-list" style={{ maxHeight: '320px', overflowY: 'auto' }}>
          {visible.length === 0 ? (
            <p className="hint">Không group nào khớp bộ lọc.</p>
          ) : (
            visible.map((group) => (
              <label className="check" key={group.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(group.id)}
                  disabled={busy}
                  onChange={() => toggle(group.id)}
                />
                <span className="check-text">
                  <b>{group.label}</b>
                  <small>
                    {group.account_label ?? 'Chưa gán tài khoản'} ·{' '}
                    {group.selected_posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'} ·{' '}
                    {CLASSIFICATION_LABEL[group.classification] ?? group.classification}
                  </small>
                  {group.capability_state === 'known' ? (
                    <span className="badge-row">
                      {group.supports_anonymous ? <Badge tone="ok">Ẩn danh</Badge> : null}
                      {group.supports_nickname ? <Badge tone="ok">Biệt danh</Badge> : null}
                    </span>
                  ) : null}
                </span>
              </label>
            ))
          )}
        </div>
      </div>
    </Card>
  );
}
