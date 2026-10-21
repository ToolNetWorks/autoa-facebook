
import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input, Select } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, Card, DataTable, EmptyState, Skeleton } from '@/components/ui/Surfaces';
import type { Collaborator, Permission, Pipeline } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const PERMISSIONS: { id: Permission; label: string }[] = [
  { id: 'facebook.view', label: 'Xem tài khoản Facebook' },
  { id: 'facebook.add', label: 'Thêm tài khoản Facebook' },
  { id: 'facebook.update_session', label: 'Cập nhật phiên đăng nhập' },
  { id: 'groups.view', label: 'Xem nhóm' },
  { id: 'groups.manage', label: 'Quản lý nhóm' },
  { id: 'group_posts.create', label: 'Tạo bài group' },
  { id: 'group_posts.publish', label: 'Đăng bài group' },
  { id: 'reels.create', label: 'Tạo Reel' },
  { id: 'reels.publish', label: 'Đăng Reel' },
  { id: 'comments.manage', label: 'Quản lý bình luận' },
  { id: 'ai_caption.use', label: 'Dùng AI Caption' },
  { id: 'media.view', label: 'Xem Album ảnh' },
  { id: 'media.upload', label: 'Tải ảnh lên Album' },
  { id: 'media.edit', label: 'Sửa ảnh' },
  { id: 'media.delete', label: 'Xóa ảnh' },
  { id: 'media.approve', label: 'Duyệt ảnh' },
  { id: 'auto_post.configure', label: 'Cấu hình tự động đăng' },
  { id: 'auto_post.create', label: 'Tạo bài tự động' },
  { id: 'auto_post.approve', label: 'Duyệt bài tự động' },
  { id: 'auto_post.publish', label: 'Đăng bài tự động' },
  { id: 'history.view', label: 'Xem lịch sử' },
  { id: 'schedule.manage', label: 'Quản lý lịch đăng' },
];

export function CollaboratorsSection({ workspace }: { workspace: WorkspaceData }) {
  const { push } = useToast();
  const [users, setUsers] = useState<Collaborator[]>([]);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Collaborator | null>(null);
  const [draft, setDraft] = useState<{ username: string; password: string; pipelineId: number | null; permissions: Permission[] }>({
    username: '',
    password: '',
    pipelineId: null,
    permissions: [],
  });
  const [creating, setCreating] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const [userRows, pipelineRows] = await Promise.all([
        api.get<Collaborator[]>('/api/admin/users'),
        api.get<Pipeline[]>('/api/pipelines'),
      ]);
      setUsers(userRows.filter((user) => user.role === 'ctv'));
      setPipelines(pipelineRows);
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tải được danh sách CTV', 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function create() {
    setBusy(true);
    try {
      await api.post('/api/admin/users', {
        username: draft.username.trim(),
        password: draft.password,
        pipeline_id: draft.pipelineId,
        permissions: draft.permissions,
      });
      push('Đã tạo tài khoản CTV', 'success');
      setCreating(false);
      setDraft({ username: '', password: '', pipelineId: null, permissions: [] });
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không tạo được CTV', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!editing) return;
    setBusy(true);
    try {
      await api.patch(`/api/admin/users/${editing.id}`, {
        pipeline_id: draft.pipelineId,
        permissions: draft.permissions,
      });
      push('Đã cập nhật CTV', 'success');
      setEditing(null);
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được CTV', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus(user: Collaborator) {
    try {
      await api.patch(`/api/admin/users/${user.id}`, {
        status: user.status === 'active' ? 'suspended' : 'active',
      });
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không cập nhật được', 'error');
    }
  }

  async function remove(user: Collaborator) {
    if (!window.confirm(`Xóa tài khoản CTV ${user.username}?`)) return;
    try {
      await api.delete(`/api/admin/users/${user.id}`);
      push('Đã xóa CTV', 'success');
      await load();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không xóa được CTV', 'error');
    }
  }

  function openEdit(user: Collaborator) {
    setEditing(user);
    setDraft({
      username: user.username,
      password: '',
      pipelineId: user.pipeline_id,
      permissions: user.permissions ?? [],
    });
  }

  return (
    <div className="stack">
      <div className="section-title">
        <div>
          <h2>CTV và phân quyền</h2>
          <p className="hint">
            Mỗi CTV thuộc đúng một Workspace và chỉ thấy dữ liệu của Workspace đó.
          </p>
        </div>
        <Button
          variant="primary"
          size="sm"
          onClick={() => {
            setCreating(true);
            setDraft({ username: '', password: '', pipelineId: workspace.pipelineId ?? null, permissions: [] });
          }}
        >
          Thêm CTV
        </Button>
      </div>

      {loading ? (
        <Skeleton variant="block" />
      ) : users.length === 0 ? (
        <EmptyState title="Chưa có tài khoản CTV" description="Thêm CTV để giao quyền đăng bài." />
      ) : (
        <Card>
          <DataTable head={['CTV', 'Workspace', 'Quyền', 'Trạng thái', 'Thao tác']}>
            {users.map((user) => (
              <tr key={user.id}>
                <td>
                  <div className="cell-title">{user.username}</div>
                  <div className="cell-sub">tạo {new Date(user.created_at).toLocaleDateString('vi-VN')}</div>
                </td>
                <td>{user.pipeline_name ?? '—'}</td>
                <td className="cell-sub">{(user.permissions ?? []).length} quyền</td>
                <td>
                  <Badge tone={user.status === 'active' ? 'ok' : 'bad'}>
                    {user.status === 'active' ? 'Đang hoạt động' : 'Đã khóa'}
                  </Badge>
                </td>
                <td className="actions">
                  <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                    <Button size="sm" onClick={() => openEdit(user)}>
                      Phân quyền
                    </Button>
                    <Button size="sm" onClick={() => void toggleStatus(user)}>
                      {user.status === 'active' ? 'Khóa' : 'Mở khóa'}
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => void remove(user)}>
                      Xóa
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Modal
        open={creating}
        title="Thêm tài khoản CTV"
        description="CTV đăng nhập bằng tên, mật khẩu và Pipeline Access Key."
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button onClick={() => setCreating(false)} disabled={busy}>
              Hủy
            </Button>
            <Button
              variant="primary"
              loading={busy}
              disabled={draft.username.trim().length < 3 || draft.password.length < 12 || !draft.pipelineId}
              onClick={() => void create()}
            >
              Tạo CTV
            </Button>
          </>
        }
      >
        <Input
          label="Username"
          name="ctv_username"
          value={draft.username}
          onChange={(event) => setDraft({ ...draft, username: event.target.value })}
        />
        <Input
          label="Mật khẩu"
          type="password"
          name="ctv_password"
          value={draft.password}
          onChange={(event) => setDraft({ ...draft, password: event.target.value })}
          hint="Tối thiểu 12 ký tự."
        />
        <Select
          label="Workspace được cấp"
          name="ctv_pipeline"
          value={draft.pipelineId ?? ''}
          onChange={(event) => setDraft({ ...draft, pipelineId: Number(event.target.value) || null })}
        >
          <option value="">Chọn Workspace...</option>
          {pipelines.map((pipeline) => (
            <option key={pipeline.id} value={pipeline.id}>
              {pipeline.name}
            </option>
          ))}
        </Select>
        <PermissionPicker
          value={draft.permissions}
          onChange={(permissions) => setDraft({ ...draft, permissions })}
        />
      </Modal>

      <Modal
        open={editing !== null}
        title={`Phân quyền cho ${editing?.username ?? ''}`}
        onClose={() => setEditing(null)}
        footer={
          <>
            <Button onClick={() => setEditing(null)} disabled={busy}>
              Hủy
            </Button>
            <Button variant="primary" loading={busy} onClick={() => void save()}>
              Lưu quyền
            </Button>
          </>
        }
      >
        <div className="stack">
          <Select
            label="Workspace được cấp"
            name="ctv_edit_pipeline"
            value={draft.pipelineId ?? ''}
            onChange={(event) => setDraft({ ...draft, pipelineId: Number(event.target.value) || null })}
          >
            <option value="">Chọn Workspace...</option>
            {pipelines.map((pipeline) => (
              <option key={pipeline.id} value={pipeline.id}>
                {pipeline.name}
              </option>
            ))}
          </Select>
          <PermissionPicker
            value={draft.permissions}
            onChange={(permissions) => setDraft({ ...draft, permissions })}
          />
          <Alert tone="info">
            Đổi Workspace, khóa tài khoản hoặc thu hồi quyền sẽ kết thúc phiên đăng nhập đang mở của CTV.
          </Alert>
        </div>
      </Modal>
    </div>
  );
}

function PermissionPicker({
  value,
  onChange,
}: {
  value: Permission[];
  onChange: (permissions: Permission[]) => void;
}) {
  return (
    <div className="field">
      <span className="field-label">Quyền được cấp</span>
      <div className="choice-list">
        {PERMISSIONS.map((permission) => {
          const checked = value.includes(permission.id);
          return (
            <label className="check" key={permission.id}>
              <input
                type="checkbox"
                checked={checked}
                onChange={(event) =>
                  onChange(
                    event.target.checked
                      ? [...value, permission.id]
                      : value.filter((item) => item !== permission.id),
                  )
                }
              />
              <span className="check-text">
                <b>{permission.label}</b>
                <small className="mono">{permission.id}</small>
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}
