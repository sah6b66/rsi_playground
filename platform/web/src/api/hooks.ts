// 查询 hooks（DD-14 §5.2）：字段名对齐 rsi/api/routers/* 实际响应。
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { message } from 'antd';
import { ApiError, apiFetch } from './client';

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
  evalset_id?: string | null;
  profile?: Record<string, unknown> | null;
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

// evalsets.py EvalsetPage / EvalsetOut
export interface Evalset {
  id: string;
  name: string;
  purpose: string;
  tasks: Record<string, unknown>[];
  description: string;
  frozen: boolean;
  version: number;
  created_at: string;
}

export interface EvalsetPage {
  items: Evalset[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export function useEvalsets(): UseQueryResult<EvalsetPage, Error> {
  return useQuery({
    queryKey: ['evalsets'],
    queryFn: () => apiFetch<EvalsetPage>('GET', '/evalsets'),
    staleTime: 15_000,
  });
}

// T6 GET /carrier/hosts 契约（本任务以 MSW stub 消费，T6 落真端点后无缝）
export interface EnvironmentHost {
  key: string;
  mode: 'local' | 'remote';
  endpoint: string;
  online: boolean;
  active_instances: number;
  max_instances: number;
  subnets?: string[];
}

export interface EnvironmentPage {
  items: EnvironmentHost[];
}

export function useEnvironments(): UseQueryResult<EnvironmentPage, Error> {
  return useQuery({
    queryKey: ['environments'],
    queryFn: () => apiFetch<EnvironmentPage>('GET', '/carrier/hosts'),
    staleTime: 15_000,
  });
}

// ── 会话写操作（T3）───────────────────────────────────────────────────────

// iterations.py IterationCreate：{sut_id, profile_id?, mode, evalset_id?, profile?}
export interface CreateSessionBody {
  sut_id: string;
  mode: string;
  evalset_id?: string;
  profile_id?: string;
  profile?: Record<string, unknown>;
}

export function useCreateSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateSessionBody) =>
      apiFetch<{ iteration_id: string }>('POST', '/iterations', body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}

export type SessionAction = 'pause' | 'resume' | 'abort' | 'archive';

export function useSessionAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      action,
    }: {
      id: string;
      action: SessionAction;
    }): Promise<void> => {
      if (action === 'archive') {
        await apiFetch<void>('DELETE', `/iterations/${id}`);
        return;
      }
      await apiFetch<Iteration>('POST', `/iterations/${id}:${action}`);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}

// ── 会话详情（T4）─────────────────────────────────────────────────────────

export function useSession(
  id: string | undefined,
): UseQueryResult<Iteration, Error> {
  return useQuery({
    queryKey: ['session', id],
    queryFn: () => apiFetch<Iteration>('GET', `/iterations/${id}`),
    enabled: !!id,
  });
}

// GET /iterations/{id}/timeline（iterations.py get_iteration_timeline）
export interface TimelineRound {
  round: number;
  status: string | null;
  batch_ids: string[];
  config_version_before: number | null;
  config_version_after: number | null;
  files_changed: { path: string | null; target: string | null }[];
  tasks: {
    task_id: string | null;
    batch_id: string;
    run_id: string;
    status: string | null;
    tokens: number | null;
    latency_ms: number | null;
    root_cause_match: boolean | null;
  }[];
  scores: { accuracy: number | null; noise_band: [number | null, number | null] };
}

export interface SessionTimeline {
  iteration_id: string;
  rounds: TimelineRound[];
  summary: {
    accuracy_series: { round: number; value: number }[];
    tokens_series: { round: number; value: number }[];
    latency_series: { round: number; value: number }[];
  };
}

export function useSessionTimeline(
  id: string | undefined,
): UseQueryResult<SessionTimeline, Error> {
  return useQuery({
    queryKey: ['session-timeline', id],
    queryFn: () => apiFetch<SessionTimeline>('GET', `/iterations/${id}/timeline`),
    enabled: !!id,
  });
}

// 四门（iterations.py _gate_router）：confirm-direction / hypothesis /
// review / decision。门② 201 {hypothesis_id}，其余 204。
export type GateName = 'confirm-direction' | 'hypothesis' | 'review' | 'decision';

export interface GateBodyVars {
  selected?: string[]; // 门① 多选
  hypothesis?: string; // 门②
  approved?: boolean; // 门③
  decision?: string; // 门④ promote/revert/advise_hold
  reasons?: string; // 门③/④ 备注（扩展字段）
}

export function useGateAction(id: string, gate: GateName) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: GateBodyVars) =>
      apiFetch<{ hypothesis_id?: string } | null>(
        'POST',
        `/iterations/${id}/${gate}`,
        vars,
      ),
    onSuccess: (data) => {
      if (data?.hypothesis_id) {
        void message.info(`已提交假设：${data.hypothesis_id}`);
      }
      void qc.invalidateQueries({ queryKey: ['session', id] });
      void qc.invalidateQueries({ queryKey: ['session-timeline', id] });
      void qc.invalidateQueries({ queryKey: ['sessions'] });
    },
    onError: (err) => {
      // 409（门不匹配/状态不符）→ 提示并 invalidate 让服务端状态自动纠正
      if (err instanceof ApiError && err.status === 409) {
        void message.error(err.message || '当前门状态已变化，已刷新会话状态');
      }
      void qc.invalidateQueries({ queryKey: ['session', id] });
    },
  });
}

// ── 会话详情（T5）：版本 diff / 文件内容 / run 轨迹 ───────────────────────

// versions.py GET /versions/{a}/diff/{b}
export interface VersionDiffItem {
  path: string;
  op: string;
  patch: string;
}

export function useVersionDiff(
  fromV: number | string | null,
  toV: number | string | null,
): UseQueryResult<VersionDiffItem[], Error> {
  return useQuery({
    queryKey: ['version-diff', fromV, toV],
    queryFn: () =>
      apiFetch<VersionDiffItem[]>('GET', `/versions/${fromV}/diff/${toV}`),
    enabled: fromV != null && toV != null,
  });
}

// suts.py GET /suts/{id}/files?path=&version= → FileTreeOut{files:{path:content}}
function useSutFileContent(
  sutId: string | undefined,
  version: number | null | undefined,
  path: string | null | undefined,
): UseQueryResult<string | undefined, Error> {
  return useQuery({
    queryKey: ['sut-file', sutId, version, path],
    queryFn: async () => {
      const qs = new URLSearchParams({ path: path ?? '' });
      if (version != null) qs.set('version', String(version));
      const out = await apiFetch<{ files: Record<string, string> }>(
        'GET',
        `/suts/${sutId}/files?${qs.toString()}`,
      );
      return out.files[path ?? ''];
    },
    enabled: !!sutId && !!path,
    retry: false,
  });
}

export function useSutFilePair(
  sutId: string | undefined,
  fromV: number | null,
  toV: number | null,
  path: string | null | undefined,
): { oldText?: string; newText?: string } {
  const oldQ = useSutFileContent(sutId, fromV, path);
  const newQ = useSutFileContent(sutId, toV, path);
  return { oldText: oldQ.data, newText: newQ.data };
}

// runs.py GET /runs/{run_id}/traces → TracePage{items,...}
// 事件 shape 对齐 rsi/core/models.py TraceEvent（seq/ts/event_type/data/...）
export interface TraceEventDto {
  seq: number;
  ts: string;
  event_type: string;
  data: Record<string, unknown>;
  run_id?: string;
  task_id?: string | null;
  model?: string | null;
  duration_ms?: number | null;
}

export interface TracePage {
  items: TraceEventDto[];
  next_cursor: string | null;
  has_more: boolean;
}

export function useRunTrace(
  runId: string | null | undefined,
): UseQueryResult<TracePage, Error> {
  return useQuery({
    queryKey: ['run-trace', runId],
    queryFn: () => apiFetch<TracePage>('GET', `/runs/${runId}/traces`),
    enabled: !!runId,
  });
}
