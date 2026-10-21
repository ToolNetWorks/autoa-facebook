import { useEffect, useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';
import { Alert, Badge, Card, DataTable, EmptyState } from '@/components/ui/Surfaces';
import { usePermissions } from '@/hooks/usePermissions';
import type { FacebookAccount } from '@/types/api';
import type { WorkspaceData } from '@/features/workspace/useWorkspace';

const SESSION_LABELS: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  connected: { label: 'Đã đăng nhập', tone: 'ok' },
  expired: { label: 'Phiên hết hạn', tone: 'bad' },
  unknown: { label: 'Chưa kiểm tra', tone: 'neutral' },
  error: { label: 'Lỗi kiểm tra', tone: 'bad' },
  busy: { label: 'Đang bận', tone: 'warn' },
};

export function AccountsSection({ workspace }: { workspace: WorkspaceData }) {
  const { can } = usePermissions();
  const { push } = useToast();
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState('');
  const [profileUrl, setProfileUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [cookieFor, setCookieFor] = useState<FacebookAccount | null>(null);
  const [cookieFile, setCookieFile] = useState<File | null>(null);

  useEffect(() => {
    workspace.reloadAccounts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function createAccount() {
    setBusy(true);
    try {
      await api.post('/api/accounts', {
        pipeline_id: workspace.pipelineId,
        label: label.trim(),
        profile_url: profileUrl.trim(),
      });
      push('Đã thêm tài khoản Facebook', 'success');
      setAdding(false);
      setLabel('');
      setProfileUrl('');
      workspace.reloadAccounts();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thêm được', 'error');
    } finally {
      setBusy(false);
    }
  }

  async function runAction(account: FacebookAccount, action: 'check' | 'connect' | 'close') {
    setPendingId(account.id);
    try {
      if (action === 'check') {
        const result = await api.post<{ status: string }>(`/api/accounts/${account.id}/check`);
        push(
          result.status === 'connected' ? 'Phiên đăng nhập còn hiệu lực' : 'Phiên đăng nhập không còn hiệu lực',
          result.status === 'connected' ? 'success' : 'warn',
        );
      } else if (action === 'connect') {
        await api.post(`/api/accounts/${account.id}/connect`);
        push('Đã mở trình duyệt đăng nhập, dùng noVNC qua SSH để đăng nhập');
      } else {
        await api.post(`/api/accounts/${account.id}/close`);
        push('Đã đóng trình duyệt đăng nhập');
      }
      workspace.reloadAccounts();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Thao tác thất bại', 'error');
    } finally {
      setPendingId(null);
    }
  }

  async function uploadCookie() {
    if (!cookieFor || !cookieFile) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append('file', cookieFile);
      await api.upload(`/api/accounts/${cookieFor.id}/cookies`, form);
      push('Đã nạp cookie vào hồ sơ trình duyệt, hãy kiểm tra lại phiên', 'success');
      setCookieFor(null);
      setCookieFile(null);
      workspace.reloadAccounts();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không nạp được cookie', 'error');
    } finally {
      setBusy(false);
    }
  }

  const accounts = workspace.accounts;

  return (
    <div className="stack">
      <div className="section-title">
        <h2>Tài khoản Facebook</h2>
        {can('facebook.add') ? (
          <Button variant="primary" size="sm" onClick={() => setAdding(true)}>
            Thêm tài khoản
          </Button>
        ) : null}
      </div>

      {accounts.length === 0 ? (
        <EmptyState
          title="Chưa có tài khoản Facebook"
          description="Thêm tài khoản rồi đăng nhập bằng trình duyệt hoặc nạp cookie JSON của chính bạn."
        />
      ) : (
        <Card>
          <DataTable head={['Tài khoản', 'Phiên', 'Cách kết nối', 'Nguồn', 'Thao tác']}>
            {accounts.map((account) => {
              const session = SESSION_LABELS[account.session_status] ?? SESSION_LABELS.unknown;
              return (
                <tr key={account.id}>
                  <td>
                    <div className="cell-title">{account.label}</div>
                    {account.fb_name ? <div className="cell-sub">{account.fb_name}</div> : null}
                    {account.fb_uid ? <div className="cell-sub">UID {account.fb_uid}</div> : null}
                  </td>
                  <td>
                    <Badge tone={session.tone}>{session.label}</Badge>
                    {account.session_checked_at ? (
                      <div className="cell-sub">{new Date(account.session_checked_at).toLocaleString('vi-VN')}</div>
                    ) : null}
                  </td>
                  <td>
                    <Badge tone={account.owner_user_id ? 'info' : 'neutral'}>
                      {account.owner_user_id ? 'Của tôi' : 'Chung'}
                    </Badge>
                    {account.owner_username ? <div className="cell-sub">{account.owner_username}</div> : null}
                  </td>
                  <td>
                    <Badge tone="info">{account.auth_method === 'cookie_file' ? 'Cookie JSON' : 'Browser Login'}</Badge>
                  </td>
                  <td className="actions">
                    <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
                      <Button size="sm" loading={pendingId === account.id} onClick={() => void runAction(account, 'check')}>
                        Kiểm tra
                      </Button>
                      <Button size="sm" onClick={() => void runAction(account, 'connect')}>
                        {account.login_open ? 'Mở lại' : 'Đăng nhập'}
                      </Button>
                      <Button size="sm" onClick={() => { setCookieFor(account); setCookieFile(null); }}>
                        Nạp cookie
                      </Button>
                      {account.login_open ? (
                        <Button size="sm" onClick={() => void runAction(account, 'close')}>
                          Đóng
                        </Button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </DataTable>
        </Card>
      )}

      <Modal
        open={adding}
        title="Thêm tài khoản Facebook"
        description="Tài khoản sẽ thuộc Workspace này và không dùng chung với pipeline khác."
        onClose={() => setAdding(false)}
        footer={
          <>
            <Button onClick={() => setAdding(false)} disabled={busy}>
              Hủy
            </Button>
            <Button variant="primary" loading={busy} disabled={label.trim().length < 2} onClick={() => void createAccount()}>
              Thêm tài khoản
            </Button>
          </>
        }
      >
        <Input
          label="Tên tài khoản"
          name="account_label"
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="VD: Facebook chính"
        />
        <Input
          label="Link trang cá nhân (không bắt buộc)"
          name="profile_url"
          value={profileUrl}
          onChange={(event) => setProfileUrl(event.target.value)}
          placeholder="https://www.facebook.com/tên.tài.khoản"
        />
      </Modal>

      <Modal
        open={cookieFor !== null}
        title="Nạp cookie JSON"
        description="Dùng cookie của chính bạn, xuất từ trình duyệt. Hệ thống không lưu tệp gốc."
        onClose={() => setCookieFor(null)}
        footer={
          <>
            <Button onClick={() => setCookieFor(null)} disabled={busy}>
              Hủy
            </Button>
            <Button variant="primary" loading={busy} disabled={!cookieFile} onClick={() => void uploadCookie()}>
              Nạp cookie
            </Button>
          </>
        }
      >
        <Input
          label="Tệp cookie JSON"
          type="file"
          accept=".json,application/json"
          onChange={(event) => setCookieFile(event.target.files?.[0] ?? null)}
        />
        <Alert tone="warn">
          Nếu phiên đăng nhập đã hết hạn, hãy dùng nút đăng nhập trình duyệt thay vì nạp lại cookie.
        </Alert>
      </Modal>
    </div>
  );
}
