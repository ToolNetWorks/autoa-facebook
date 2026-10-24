import { useState } from 'react';
import { api } from '@/services/apiClient';
import { useToast } from '@/app/ToastProvider';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Field';
import { Modal } from '@/components/ui/Modal';

interface PasswordStrength {
  score: number;
  label: string;
  message: string;
}

function checkPasswordStrength(password: string): PasswordStrength {
  let score = 0;
  if (password.length >= 12) score += 1;
  if (password.length >= 16) score += 1;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
  if (/\d/.test(password)) score += 1;
  if (/[^a-zA-Z\d]/.test(password)) score += 2;
  if (score >= 5) return { score: 5, label: 'Rất mạnh', message: 'Mật khẩu rất mạnh.' };
  if (score >= 4) return { score: 4, label: 'Mạnh', message: 'Mật khẩu khá mạnh.' };
  if (score >= 3) return { score: 3, label: 'Trung bình', message: 'Mật khẩu đủ an toàn nhưng có thể cải thiện.' };
  if (score >= 2) return { score: 2, label: 'Yếu', message: 'Mật khẩu chưa đủ mạnh. Cần ít nhất 12 ký tự, bao gồm chữ hoa, chữ thường, số và ký tự đặc biệt.' };
  return { score: 1, label: 'Rất yếu', message: 'Mật khẩu quá yếu. Cần ít nhất 12 ký tự đa dạng.' };
}

function showPasswordLabel(visible: boolean): string {
  return visible ? 'Ẩn mật khẩu' : 'Hiện mật khẩu';
}

export function PasswordChangeModal({
  open,
  onClose,
  onPasswordChanged,
  force = false,
}: {
  open: boolean;
  onClose: () => void;
  onPasswordChanged: () => void;
  force?: boolean;
}) {
  const { push } = useToast();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [currentVisible, setCurrentVisible] = useState(false);
  const [newVisible, setNewVisible] = useState(false);
  const [confirmVisible, setConfirmVisible] = useState(false);
  const [busy, setBusy] = useState(false);

  const strength = checkPasswordStrength(newPassword);
  const passwordsMatch = confirmPassword.length > 0 && newPassword === confirmPassword;
  const canSubmit = !force
    ? currentPassword && newPassword && newPassword.length >= 12 && passwordsMatch && strength.score >= 3
    : newPassword && newPassword.length >= 12 && passwordsMatch && strength.score >= 3;

  async function handleSubmit() {
    if (!canSubmit) return;
    setBusy(true);
    try {
      const payload: Record<string, string> = {
        new_password: newPassword,
      };
      if (!force) {
        payload.current_password = currentPassword;
      }
      await api.post<{ ok: boolean }>('/api/auth/change-password', payload);
      push(force ? 'Đã đổi mật khẩu. Vui lòng đăng nhập lại.' : 'Đã đổi mật khẩu thành công.', 'success');
      onPasswordChanged();
      onClose();
    } catch (failure) {
      push(failure instanceof Error ? failure.message : 'Không thể đổi mật khẩu', 'error');
    } finally {
      setBusy(false);
    }
  }

  function resetForm() {
    setCurrentPassword('');
    setNewPassword('');
    setCurrentVisible(false);
    setNewVisible(false);
    setConfirmVisible(false);
    setConfirmPassword('');
  }

  function handleClose() {
    if (!force) {
      resetForm();
    }
    onClose();
  }

  function getStrengthColor(score: number): string {
    if (score >= 4) return 'var(--color-ok)';
    if (score >= 3) return 'var(--color-warn)';
    return 'var(--color-bad)';
  }

  return (
    <Modal
      open={open}
      title={force ? 'Đổi mật khẩu bắt buộc' : 'Đổi mật khẩu'}
      description={force
        ? 'Đây là lần đầu đăng nhập. Vui lòng đặt mật khẩu mới để tiếp tục.'
        : 'Nhập mật khẩu hiện tại và mật khẩu mới.'}
      onClose={handleClose}
      footer={
        <>
          <Button onClick={handleClose} disabled={busy} variant="ghost">
            {force ? 'Đăng nhập lại' : 'Hủy'}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!canSubmit}
            onClick={() => handleSubmit()}
          >
            {force ? 'Đặt mật khẩu mới' : 'Lưu thay đổi'}
          </Button>
        </>
      }
    >
      <div className="stack">
        {!force ? (
          <div style={{ position: 'relative' }}>
            <Input
              label="Mật khẩu hiện tại"
              name="current_password"
              type={currentVisible ? 'text' : 'password'}
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
            />
            <button
              type="button"
              className="icon-btn"
              style={{ position: 'absolute', right: 'var(--sp-2)', top: 'var(--sp-2)' }}
              onClick={() => setCurrentVisible(!currentVisible)}
              aria-label={showPasswordLabel(currentVisible)}
            >
              {currentVisible ? 'Ẩn' : '👁'}
            </button>
          </div>
        ) : null}
        <div style={{ position: 'relative' }}>
          <Input
            label="Mật khẩu mới"
            name="new_password"
            type={newVisible ? 'text' : 'password'}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            minLength={12}
          />
          <button
            type="button"
            className="icon-btn"
            style={{ position: 'absolute', right: 'var(--sp-2)', top: 'var(--sp-2)' }}
            onClick={() => setNewVisible(!newVisible)}
            aria-label={showPasswordLabel(newVisible)}
          >
            {newVisible ? 'Ẩn' : '👁'}
          </button>
        </div>
        {newPassword ? (
          <div className="hint">
            Độ mạnh: {''}
            <b style={{ color: getStrengthColor(strength.score) }}>{strength.label}</b>
            {' - '}
            {strength.message}
          </div>
        ) : null}
        <div style={{ position: 'relative' }}>
          <Input
            label="Xác nhận mật khẩu mới"
            name="confirm_password"
            type={confirmVisible ? 'text' : 'password'}
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={12}
            error={confirmPassword && !passwordsMatch ? 'Mật khẩu không khớp' : null}
          />
          <button
            type="button"
            className="icon-btn"
            style={{ position: 'absolute', right: 'var(--sp-2)', top: 'var(--sp-2)' }}
            onClick={() => setConfirmVisible(!confirmVisible)}
            aria-label={showPasswordLabel(confirmVisible)}
          >
            {confirmVisible ? 'Ẩn' : '👁'}
          </button>
        </div>
      </div>
    </Modal>
  );
}
