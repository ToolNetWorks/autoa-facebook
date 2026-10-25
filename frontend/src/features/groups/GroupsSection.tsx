import { useEffect, useMemo, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select, Textarea } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, Card, DataTable, EmptyState, ErrorState, Tabs } from '@/components/ui/Surfaces';
import { Radio } from '@/components/ui/Choice';
import { usePermissions } from '@/hooks/usePermissions';
import { JoinControls, useMemberships } from '@/features/groups/JoinControls';
import type { GroupRow } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const FILTERS = [
  { id: 'all', label: 'Tất cả' },
  { id: 'no_approval', label: 'Đăng Không Cần Duyệt' },
  { id: 'needs_approval', label: 'Cần Duyệt' },
  { id: 'unclassified', label: 'Chưa phân loại' },
];

interface Draft {
  id?: number;
  url: string;
  label: string;
  accountId: number | null;
  mode: 'anonymous' | 'nickname';
  nickname: string;
  approved: boolean;
}

const EMPTY_DRAFT: Draft = {
  url: '',
  label: '',
  accountId: null,
  mode: 'anonymous',
  nickname: '',
  approved: false,
};

export function GroupsSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const { can } = usePermissions();
  const [filter, setFilter] = useState('all');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkUrls, setBulkUrls] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingId, setPendingId] = useState<number | null>(null);

  useEffect(() => {
    workspace.reloadGroups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const groups = workspace.groups;
  const memberships = useMemberships(workspace.pipelineId);

  const visible = useMemo(() => {
    return groups.filter((group) => {
      if (filter === 'no_approval') return group.classification === 'no_approval';
      if (filter === 'needs_approval') return group.classification === 'needs_approval';
      if (filter === 'unclassified') return group.classification === 'unclassified';
      return true;
    });
  }, [groups, filter]);

  const classificationCounts = useMemo(() => {
    const counts = { no_approval: 0, needs_approval: 0, unclassified: 0 };
    groups.forEach((group) => {
      if (counts[group.classification] !== undefined) counts[group.classification] += 1;
    });
    return counts;
  }, [groups]);

  async function saveDraft() {
    if (!draft) return;
    setBusy(true);
    try {
      const body = {
        label: draft.label.trim() || 'Group',
        account_id: draft.accountId,
        posting_mode: draft.mode,
        nickname: draft.nickname.trim(),
        approved_target: draft.approved,
      };
      if (draft.id) {
        await api.patch(`/api/pipelines/${workspace.pipelineId}/groups/${draft.id}`, body);
        push('Đã cập nhật group', 'success');
      } else {
        await api.post(`/api/pipelines/${workspace.pipelineId}/groups`, {
          ...body,
          pipeline_id: workspace.pipelineId,
          url: draft.url.trim(),
        });
        push('Đã thêm group', 'success');
      }
      setDraft(null);
      workspace.reloadGroups();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không lưu được group', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function bulkAdd() {
    setBusy(true);
    try {
      const result = await api.post<{ added_count: number; duplicate_count: number; invalid_count: number }>(
        `/api/pipelines/${workspace.pipelineId}/groups/bulk`,
        {
          urls: bulkUrls,
          account_id: workspace.accounts[0]?.id ?? null,
          posting_mode: 'anonymous',
          nickname: '',
          approved_target: true,
        },
      );
      push(
        `Đã thêm ${result.added_count} group, bỏ qua ${result.duplicate_count} trùng, ${result.invalid_count} sai định dạng`,
        'success',
      );
      setBulkOpen(false);
      setBulkUrls('');
      workspace.reloadGroups();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thêm được', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function checkGroup(group: GroupRow) {
    setPendingId(group.id);
    try {
      const result = await api.post<{ status: string }>(
        `/api/pipelines/${workspace.pipelineId}/groups/${group.id}/check`,
      );
      push(
        result.status === 'ok' ? 'Đã kiểm tra group' : 'Kiểm tra group chưa thành công',
        result.status === 'ok' ? 'success' : 'warn',
      );
      workspace.reloadGroups();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không kiểm tra được group', 'error');
    } finally {
      setPendingId(null);
    }
  }

  async function toggleGroup(group: GroupRow) {
    try {
      await api.patch(`/api/pipelines/${workspace.pipelineId}/groups/${group.id}`, {
        status: group.status === 'active' ? 'paused' : 'active',
      });
      workspace.reloadGroups();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được group', 'error');
    }
  }

  async function deleteGroup(group: GroupRow) {
    if (!window.confirm(`Xóa group ${group.label} khỏi Workspace?`)) return;
    try {
      await api.delete(`/api/pipelines/${workspace.pipelineId}/groups/${group.id}`);
      push('Đã xóa group', 'success');
      workspace.reloadGroups();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được group', 'error');
    }
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>Nhóm Facebook</h2>
          <p className="hint">Mỗi group có tài khoản và chế độ đăng riêng. Chỉ group đã xác minh mới được đăng tự động.</p>
        </div>
        <div className="btn-row">
          {can('groups.manage') ? (
            <>
              <Button size="sm" onClick={() => setBulkOpen(true)}>
                Thêm nhiều
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={() => setDraft({ ...EMPTY_DRAFT, accountId: workspace.accounts[0]?.id ?? null })}
              >
                Thêm group
              </Button>
            </>
          ) : (
            <span className="hint">Bạn chỉ xem được nhóm của mình. Nhóm thêm bởi người khác không hiển thị.</span>
          )}
        </div>
      </div>

      {can('groups.manage') && workspace.accounts.length === 0 ? (
        <Alert tone="warn">
          Bạn chưa có tài khoản Facebook nào trong Workspace. Thêm tài khoản trước khi tạo group.
        </Alert>
      ) : null}

      <Tabs
        tabs={FILTERS.map((entry) => ({
          id: entry.id,
          label: entry.label,
          count:
            entry.id === 'all'
              ? groups.length
              : entry.id === 'no_approval'
                ? classificationCounts.no_approval
                : entry.id === 'needs_approval'
                  ? classificationCounts.needs_approval
                  : classificationCounts.unclassified,
        }))}
        active={filter}
        onChange={setFilter}
      />

      {workspace.groupsState.error ? (
        <ErrorState
          title="Không thể tải danh sách group. Vui lòng thử lại."
          action={
            <Button size="sm" onClick={() => workspace.reloadGroups()}>
              Thử lại
            </Button>
          }
        />
      ) : groups.length === 0 ? (
        <EmptyState title="Chưa có group nào" description="Thêm group bằng link để bắt đầu." />
      ) : (
        <Card>
          <DataTable head={['Group', 'Tài khoản', 'Phân loại', 'Chế độ', 'Khả năng đăng', 'Thành viên', 'Thao tác']}>
            {visible.map((group) => (
              <tr key={group.id}>
                <td>
                  <div className="cell-title">{group.label}</div>
                  <div className="cell-sub truncate" style={{ maxWidth: '260px' }}>
                    {group.url}
                  </div>
                </td>
                <td>{group.account_label ?? '—'}</td>
                <td>
                  <Badge tone={group.classification_tone}>
                    {group.classification_label}
                  </Badge>
                  <div className="cell-sub">
                    {group.last_verified_at
                      ? new Date(group.last_verified_at).toLocaleString('vi-VN')
                      : 'chưa xác minh'}
                  </div>
                </td>
                <td>
                  <Badge tone={group.selected_posting_mode === 'nickname' ? 'info' : 'neutral'}>
                    {group.selected_posting_mode === 'nickname' ? 'Biệt danh' : 'Ẩn danh'}
                  </Badge>
                  {group.nickname ? <div className="cell-sub">{group.nickname}</div> : null}
                </td>
                <td>
                  <div className="badge-row">
                    {group.supports_anonymous ? <Badge tone="ok">Ẩn danh</Badge> : null}
                    {group.supports_nickname ? <Badge tone="ok">Biệt danh</Badge> : null}
                    {group.capability_state === 'unknown' ? (
                      <Badge tone="neutral">Chưa xác định</Badge>
                    ) : null}
                  </div>
                </td>
                <td>
                  <JoinControls
                    pipelineId={workspace.pipelineId ?? 0}
                    groupId={group.id}
                    membership={memberships.byGroup.get(group.id)}
                    canManage={can('groups.manage')}
                    onChanged={() => {
                      void memberships.reload(true);
                      workspace.reloadGroups();
                    }}
                  />
                </td>
                <td>
                  <Badge tone={group.owner_user_id ? 'info' : 'neutral'}>
                    {group.owner_user_id ? 'Của tôi' : 'Chung'}
                  </Badge>
                  {group.owner_username ? <div className="cell-sub">{group.owner_username}</div> : null}
                </td>
                <td className="actions">
                  <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                    {can('groups.manage') ? (
                      <Button size="sm" loading={pendingId === group.id} onClick={() => void checkGroup(group)}>
                        Kiểm tra
                      </Button>
                    ) : null}
                    {can('groups.manage') ? (
                      <>
                        <Button
                          size="sm"
                          onClick={() =>
                            setDraft({
                              id: group.id,
                              url: group.url,
                              label: group.label,
                              accountId: group.account_id,
                              mode: group.posting_mode,
                              nickname: group.nickname,
                              approved: Boolean(group.approved_target),
                            })
                          }
                        >
                          Sửa
                        </Button>
                        <Button size="sm" onClick={() => void toggleGroup(group)}>
                          {group.status === 'active' ? 'Tạm dừng' : 'Bật lại'}
                        </Button>
                        <Button size="sm" variant="danger" onClick={() => void deleteGroup(group)}>
                          Xóa
                        </Button>
                      </>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Modal
        open={draft !== null}
        title={draft?.id ? 'Chỉnh sửa group' : 'Thêm group'}
        description="Group dùng chung cho bài đăng ảnh ẩn danh hoặc biệt danh của Workspace này."
        onClose={() => setDraft(null)}
        footer={
          <>
            <Button onClick={() => setDraft(null)} disabled={busy}>
              Hủy
            </Button>
            <Button
              variant="primary"
              loading={busy}
              disabled={!draft || (!draft.id && draft.url.trim().length < 4)}
              onClick={() => void saveDraft()}
            >
              Lưu group
            </Button>
          </>
        }
      >
        {draft ? (
          <div className="stack">
            {!draft.id ? (
              <Input
                label="Link group"
                name="group_url"
                placeholder="https://www.facebook.com/groups/123456789/"
                value={draft.url}
                onChange={(event) => setDraft({ ...draft, url: event.target.value })}
              />
            ) : null}
            <Input
              label="Tên hiển thị"
              name="group_label"
              value={draft.label}
              onChange={(event) => setDraft({ ...draft, label: event.target.value })}
            />
            <Select
              label="Tài khoản Facebook"
              name="group_account"
              value={draft.accountId ?? ''}
              onChange={(event) => setDraft({ ...draft, accountId: Number(event.target.value) || null })}
            >
              <option value="">Chọn tài khoản...</option>
              {workspace.accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.label}
                </option>
              ))}
            </Select>
            <div className="field">
              <span className="field-label">Chế độ đăng</span>
              <div className="choice-list">
                <Radio
                  name="group_mode"
                  value="anonymous"
                  checked={draft.mode === 'anonymous'}
                  onChange={() => setDraft({ ...draft, mode: 'anonymous' })}
                  title="Ẩn danh"
                  description="Bài đăng không hiện tên tài khoản."
                />
                <Radio
                  name="group_mode"
                  value="nickname"
                  checked={draft.mode === 'nickname'}
                  onChange={() => setDraft({ ...draft, mode: 'nickname' })}
                  title="Biệt danh"
                  description="Dùng đúng biệt danh đã cấu hình."
                />
              </div>
            </div>
            {draft.mode === 'nickname' ? (
              <Input
                label="Tên biệt danh"
                name="group_nickname"
                value={draft.nickname}
                onChange={(event) => setDraft({ ...draft, nickname: event.target.value })}
                hint="Chỉ là cấu hình mong muốn. Hệ thống không tự tạo biệt danh trên Facebook."
              />
            ) : null}
            <label className="check">
              <input
                type="checkbox"
                checked={draft.approved}
                onChange={(event) => setDraft({ ...draft, approved: event.target.checked })}
              />
              <span className="check-text">
                <b>Tôi là thành viên được phép đăng bài</b>
                <small>Và sẽ tuân thủ nội quy nhóm.</small>
              </span>
            </label>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={bulkOpen}
        title="Thêm nhiều group"
        description="Mỗi dòng là một link group. Link trùng sẽ được bỏ qua."
        onClose={() => setBulkOpen(false)}
        footer={
          <>
            <Button onClick={() => setBulkOpen(false)} disabled={busy}>
              Hủy
            </Button>
            <Button variant="primary" loading={busy} disabled={bulkUrls.trim().length < 4} onClick={() => void bulkAdd()}>
              Thêm group
            </Button>
          </>
        }
      >
        <Textarea
          label="Danh sách link"
          name="bulk_urls"
          rows={8}
          value={bulkUrls}
          onChange={(event) => setBulkUrls(event.target.value)}
          placeholder={'https://www.facebook.com/groups/123456789/\nhttps://www.facebook.com/groups/987654321/'}
        />
        <Alert tone="info">
          Group thêm vào sẽ ở trạng thái chờ kiểm tra. Bấm Kiểm tra để hệ thống xác minh quyền đăng.
        </Alert>
      </Modal>
    </div>
  );
}
