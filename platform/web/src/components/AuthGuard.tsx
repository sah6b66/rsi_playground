// 路由守卫：无 token → /login；<RequirePerm perm> 按角色拦截（403）。
import { useEffect } from 'react';
import { Result, Spin } from 'antd';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { apiFetch, getToken } from '../api/client';
import { USER_KEY } from '../utils/constants';
import type { Role } from '../utils/permissions';
import { can } from '../utils/permissions';
import { useUiStore } from '../stores/ui';

interface MeResponse {
  user_id: string;
  role: Role;
  expires_at: number;
}

function useHydrateUser() {
  const user = useUiStore((s) => s.user);
  const setUser = useUiStore((s) => s.setUser);
  useEffect(() => {
    if (user || !getToken()) return;
    const raw = localStorage.getItem(USER_KEY);
    if (raw) {
      try {
        setUser(JSON.parse(raw) as { username: string; role: Role });
        return;
      } catch {
        localStorage.removeItem(USER_KEY);
      }
    }
  }, [user, setUser]);
  // 后端权威校验角色（token 有效 + role 最新）
  useQuery({
    queryKey: ['me'],
    enabled: !!getToken(),
    queryFn: () => apiFetch<MeResponse>('GET', '/auth/me'),
    staleTime: 5 * 60 * 1000,
  });
}

export function AuthGuard() {
  useHydrateUser();
  const location = useLocation();
  if (!getToken()) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

export function RequirePerm({ perm, children }: { perm: string; children: React.ReactNode }) {
  useHydrateUser();
  const role = useUiStore((s) => s.user)?.role;
  if (!role) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }
  if (!can(role, perm)) {
    return <Result status="403" title="403" subTitle="抱歉，您没有权限访问此页面。" />;
  }
  return <>{children}</>;
}
