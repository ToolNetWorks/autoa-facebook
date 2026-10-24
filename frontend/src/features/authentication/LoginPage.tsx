import { useState, type FormEvent } from 'react';
import { Button } from '@/components/ui/Button';
import { Input, PasswordInput } from '@/components/ui/Field';
import { Alert } from '@/components/ui/Surfaces';

interface LoginPageProps {
  onSignIn: (username: string, password: string, pipelineKey: string) => Promise<void>;
}

export function LoginPage({ onSignIn }: LoginPageProps) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [pipelineKey, setPipelineKey] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSignIn(username.trim(), password, pipelineKey.trim());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Đăng nhập thất bại');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        padding: 'var(--sp-4)',
      }}
    >
      <div style={{ width: 'min(420px, 100%)' }}>
        <div className="row" style={{ marginBottom: 'var(--sp-4)' }}>
          <span className="brand-mark">T</span>
          <span>
            <b style={{ display: 'block', fontSize: 'var(--fs-16)' }}>ToolNet Publisher</b>
            <span className="subtle" style={{ fontSize: 'var(--fs-11)', letterSpacing: '0.06em' }}>
              FACEBOOK AUTOMATION
            </span>
          </span>
        </div>
        <form id="loginForm" className="card" onSubmit={handleSubmit}>
          <h2 style={{ marginBottom: 'var(--sp-4)' }}>Đăng nhập</h2>
          <div className="stack">
            <Input
              label="Tài khoản"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
            <PasswordInput
              label="Mật khẩu"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <Input
              label="Pipeline Access Key"
              name="pipeline_key"
              hint="Chỉ cần cho tài khoản cộng tác viên."
              value={pipelineKey}
              onChange={(event) => setPipelineKey(event.target.value)}
            />
            {error ? <Alert tone="bad">{error}</Alert> : null}
            <Button type="submit" variant="primary" block loading={busy}>
              Đăng nhập
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
