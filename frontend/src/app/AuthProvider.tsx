import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ApiError, api, setCsrfToken } from '@/services/apiClient';
import type { Permission, Session } from '@/types/api';

interface AuthContextValue {
  session: Session | null;
  loading: boolean;
  can: (permission: Permission) => boolean;
  isAdmin: boolean;
  refresh: () => Promise<void>;
  signIn: (username: string, password: string, pipelineKey: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  session: null,
  loading: true,
  can: () => false,
  isAdmin: false,
  refresh: async () => undefined,
  signIn: async () => undefined,
  signOut: async () => undefined,
});

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  const apply = useCallback((next: Session | null) => {
    setSession(next);
    setCsrfToken(next?.csrf_token ?? '');
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await api.get<Session>('/api/auth/me');
      apply(next);
    } catch (error) {
      if (error instanceof ApiError && error.isAuthError) apply(null);
      else throw error;
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    void refresh().catch(() => setLoading(false));
  }, [refresh]);

  const signIn = useCallback(
    async (username: string, password: string, pipelineKey: string) => {
      const next = await api.post<Session>('/api/auth/login', {
        username,
        password,
        pipeline_key: pipelineKey,
      });
      apply(next);
    },
    [apply],
  );

  const signOut = useCallback(async () => {
    try {
      await api.post('/api/auth/logout');
    } finally {
      apply(null);
    }
  }, [apply]);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      loading,
      isAdmin: session?.role === 'admin',
      can: (permission: Permission) =>
        session?.role === 'admin' || (session?.permissions ?? []).includes(permission),
      refresh,
      signIn,
      signOut,
    }),
    [session, loading, refresh, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
