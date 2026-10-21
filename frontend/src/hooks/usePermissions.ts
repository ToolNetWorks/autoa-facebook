import { useAuth } from '@/app/AuthProvider';
import type { Permission } from '@/types/api';

export interface PermissionState {
  can: (permission: Permission) => boolean;
  isAdmin: boolean;
  userId: number | null;
}

/** The permissions of the signed in user. Administrators may do everything. */
export function usePermissions(): PermissionState {
  const { session } = useAuth();
  const permissions = session?.permissions ?? [];
  return {
    can: (permission) => (session?.role === 'admin' ? true : permissions.includes(permission)),
    isAdmin: session?.role === 'admin',
    userId: session?.user_id ?? null,
  };
}
