// 查询 hooks（DD-14 §5.2）：字段名对齐 rsi/api/routers/* 实际响应。
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { apiFetch } from './client';

// iterations.py IterationOut
export interface Iteration {
  id: string;
  sut_id: string;
  profile_id: string;
  mode: string;
  state: string;
  current_step: string | null;
  round: number;
  experiments: string[];
  budget_used: Record<string, unknown>;
  archived: boolean;
  created_at: string;
}

export interface IterationPage {
  items: Iteration[];
}

// suts.py SutPage
export interface Sut {
  id: string;
  name: string;
  carrier: string;
  image_ref: string;
  description: string;
  created_at: string;
}

export interface SutPage {
  items: Sut[];
  next_cursor?: string | null;
  has_more?: boolean;
}

// labs.py LabPage / LabOut
export interface Lab {
  id: string;
  sut_id: string;
  status: string;
  online: boolean;
  topology: Record<string, unknown>;
  created_at: string;
}

export interface LabPage {
  items: Lab[];
}

// labs.py GET /sandbox/backends（sandbox_service 已 wiring 时的真实 shape）
export interface BackendStatus {
  name: string;
  kind: string;
  status: string;
  available: boolean;
  active_labs: number;
  detail: string | null;
}

export interface BackendPage {
  items: BackendStatus[];
}

// instances.py InstancePage / InstanceOut
export interface SutInstance {
  id: string;
  sut_id: string;
  lab_id: string | null;
  status: string;
}

export interface InstancePage {
  items: SutInstance[];
}

// system.py DependencyPage / DependencyStatus；
// detail 为可选扩展字段（探活失败原因，监视器提供时展示）
export interface Dependency {
  name: string;
  status: string;
  latency_ms?: number | null;
  detail?: string | null;
}

export interface DependencyPage {
  items: Dependency[];
  source: string;
}

export interface SystemHealth {
  status: string;
}

export function useSessions(params?: {
  sut_id?: string;
}): UseQueryResult<IterationPage, Error> {
  const qs = params?.sut_id ? `?sut_id=${encodeURIComponent(params.sut_id)}` : '';
  return useQuery({
    queryKey: ['sessions', params?.sut_id ?? null],
    queryFn: () => apiFetch<IterationPage>('GET', `/iterations${qs}`),
    staleTime: 15_000,
  });
}

export function useSuts(): UseQueryResult<SutPage, Error> {
  return useQuery({
    queryKey: ['suts'],
    queryFn: () => apiFetch<SutPage>('GET', '/suts'),
    staleTime: 15_000,
  });
}

export function useLabs(): UseQueryResult<LabPage, Error> {
  return useQuery({
    queryKey: ['labs'],
    queryFn: () => apiFetch<LabPage>('GET', '/labs'),
    staleTime: 15_000,
  });
}

export function useBackends(): UseQueryResult<BackendPage, Error> {
  return useQuery({
    queryKey: ['backends'],
    queryFn: () => apiFetch<BackendPage>('GET', '/sandbox/backends'),
    staleTime: 15_000,
  });
}

export function useInstances(): UseQueryResult<InstancePage, Error> {
  return useQuery({
    queryKey: ['instances'],
    queryFn: () => apiFetch<InstancePage>('GET', '/instances'),
    staleTime: 15_000,
  });
}

export function useDependencies(): UseQueryResult<DependencyPage, Error> {
  return useQuery({
    queryKey: ['dependencies'],
    queryFn: () => apiFetch<DependencyPage>('GET', '/system/dependencies'),
    staleTime: 15_000,
  });
}

export function useSystemHealth(): UseQueryResult<SystemHealth, Error> {
  return useQuery({
    queryKey: ['system-health'],
    queryFn: () => apiFetch<SystemHealth>('GET', '/system/health'),
    staleTime: 15_000,
  });
}
