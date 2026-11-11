// 查询 hooks（DD-14 §5.2）：字段名对齐 rsi/api/routers/* 实际响应。
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { message } from 'antd';
import { ApiError, apiFetch, apiUpload } from './client';

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
  // U7a 会话标识（1-64 字符人可读名称；null → 列表回退 ID 前 8 位）
  label?: string | null;
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
  // U7b：RSI 可优化范围（SUT 树相对路径模式）；null/undefined=自动模式
  optimization_scope?: string[] | null;
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
  // 平台级设备清单（ADR-25 增补：监控寻址，与在用 lab 无关）
  device_count?: number;
  devices?: Record<string, { ip: string; port: number }>;
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

// evalsets.py EvalsetPage / EvalsetOut（T6：env = 解析后的 env.yaml）
export interface EvalsetEnv {
  backend?: string;
  devices?: { name: string; type?: string }[];
  ssh_access?: Record<string, { ip: string; port: number; username?: string }>;
  [k: string]: unknown;
}

// U7a 详情页摘要（凭证剥离：仅 name/type/version/ip/port/base_url）
export interface EvalsetEnvParsed {
  name?: string | null;
  backend?: string | null;
  devices: {
    name?: string | null;
    type?: string | null;
    version?: string | null;
    ip?: string | null;
    port?: number | null;
  }[];
}

export interface Evalset {
  id: string;
  name: string;
  purpose: string;
  tasks: Record<string, unknown>[];
  description: string;
  frozen: boolean;
  version: number;
  created_at: string;
  env?: EvalsetEnv | null;
  env_parsed?: EvalsetEnvParsed | null;
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

// U7a 评测集详情
export function useEvalset(
  id: string | undefined,
): UseQueryResult<Evalset, Error> {
  return useQuery({
    queryKey: ['evalset', id],
    queryFn: () => apiFetch<Evalset>('GET', `/evalsets/${id}`),
    enabled: !!id,
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
  // U7a 会话标识（1-64 字符，中文可用）
  label?: string;
  // ADR-27 双模式：CLI（离线评估集+SSH 环境准备）/ API（在线评估集+网络准备 API）
  prep_mode?: 'cli' | 'api';
  api_evalset_name?: string;
}

// D3 GET /evalsets/online/{name} 在线评估集预览（ADR-27）
export interface OnlineEvalsetPreview {
  name: string;
  task_count: number;
  tasks: { task_id: string; title: string; network_env_name: string }[];
  api_url: string;
}

export function useOnlineEvalset(
  name: string,
): UseQueryResult<OnlineEvalsetPreview, Error> {
  return useQuery({
    queryKey: ['evalset-online', name],
    queryFn: () =>
      apiFetch<OnlineEvalsetPreview>(
        'GET',
        `/evalsets/online/${encodeURIComponent(name)}`,
      ),
    enabled: !!name,
    staleTime: 30_000,
  });
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

// ── 资源页（T6）：镜像 / 执行主机 / 沙箱 / 评测集写操作 ────────────────────

// GET /docker/images（images.py DockerImagePage）
export interface DockerImage {
  host: string;
  ref: string;
  size_mb: number | null;
  created_at: string | null;
  used_by: string[];
}

export interface DockerImagePage {
  items: DockerImage[];
  offline_hosts: string[];
}

export function useImages(): UseQueryResult<DockerImagePage, Error> {
  return useQuery({
    queryKey: ['images'],
    queryFn: () => apiFetch<DockerImagePage>('GET', '/docker/images'),
    staleTime: 15_000,
  });
}

export function useLoadImage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { host: string; tar_path: string }) =>
      apiFetch<{ status: string; host: string }>('POST', '/docker/images:load', body),
    onSuccess: (out) => {
      void message.success(`镜像导入任务已提交（${out.host}）`);
      void qc.invalidateQueries({ queryKey: ['images'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useDeleteImage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ref, host }: { ref: string; host: string }) =>
      apiFetch<void>('DELETE', `/docker/images/${encodeURIComponent(ref)}?host=${encodeURIComponent(host)}`),
    onSuccess: () => {
      void message.success('镜像已删除');
      void qc.invalidateQueries({ queryKey: ['images'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// PUT /carrier/hosts（hosts.py：静态段重写 → restart_required）
export interface HostSaveItem {
  key: string;
  endpoint: string;
  max_instances?: number | null;
  subnets: string[];
}

export function useSaveHosts() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (items: HostSaveItem[]) =>
      apiFetch<{ restart_required: boolean }>('PUT', '/carrier/hosts', { items }),
    onSuccess: () => {
      void message.success('已写入 settings.yaml，重启平台后生效');
      void qc.invalidateQueries({ queryKey: ['environments'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// PUT /sandbox/backends/{key}（labs.py：静态段 → restart_required）
export interface BackendSaveBody {
  kind: string;
  url: string;
  verify_tls: boolean;
  subnets: string[];
  devices?: Record<string, { ip: string; port: number }>;
}

export function useSaveBackend() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, ...body }: BackendSaveBody & { key: string }) =>
      apiFetch<{ restart_required: boolean }>(
        'PUT',
        `/sandbox/backends/${encodeURIComponent(key)}`,
        body,
      ),
    onSuccess: () => {
      void message.success('已写入 settings.yaml，重启平台后生效');
      void qc.invalidateQueries({ queryKey: ['backends'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useDeleteEvalset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<void>('DELETE', `/evalsets/${id}`),
    onSuccess: () => {
      void message.success('评测集已删除');
      void qc.invalidateQueries({ queryKey: ['evalsets'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// POST /evalsets/{id}:clone（evalsets.py CloneBody）
export interface CloneOverrides {
  device_endpoints?: Record<string, { ip?: string; port?: number }>;
  site_name?: string;
}

export function useCloneEvalset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      new_name,
      overrides,
    }: {
      id: string;
      new_name: string;
      overrides: CloneOverrides;
    }) =>
      apiFetch<Evalset>('POST', `/evalsets/${id}:clone`, {
        new_name,
        overrides,
      }),
    onSuccess: (out) => {
      void message.success(`已克隆为 ${out.name}`);
      void qc.invalidateQueries({ queryKey: ['evalsets'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// POST /evalsets:import（multipart zip；导入即入库，前端预览卡展示摘要）
export function useImportEvalset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ file, name }: { file: File; name?: string }) => {
      const form = new FormData();
      form.append('file', file);
      if (name) form.append('name', name);
      return apiUpload<Evalset>('/evalsets:import', form);
    },
    onSuccess: (out) => {
      void message.success(`已导入 ${out.name}`);
      void qc.invalidateQueries({ queryKey: ['evalsets'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// ── T7 参数六 Tab：settings / logs / audit / credentials / users ───────────

// system.py SettingItem / SettingsPage
export interface SettingItem {
  path: string;
  value: unknown;
  source: string;
  mutable: boolean;
  // C2：静态键自启动后被改过 → 需重启平台才生效
  pending_restart?: boolean;
}

export interface SettingsPageDto {
  items: SettingItem[];
}

export function useSystemSettings(): UseQueryResult<SettingsPageDto, Error> {
  return useQuery({
    queryKey: ['system-settings'],
    queryFn: () => apiFetch<SettingsPageDto>('GET', '/system/settings'),
  });
}

export function useUpdateSetting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) =>
      apiFetch<SettingItem>(
        'PUT',
        `/system/settings/${encodeURIComponent(path)}`,
        { value },
      ),
    onSuccess: (out) => {
      void message.success(
        out.pending_restart ? `已保存，重启平台后生效：${out.path}` : `已热生效：${out.path}`,
      );
      void qc.invalidateQueries({ queryKey: ['system-settings'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// ── C2：LLM 提供商直配（半脱敏）+ 平台重启 ────────────────────────────────

// system.py LlmProviderOut（api_key 仅返回掩码 ***尾3位）
export interface LlmProviderDto {
  name: string;
  api_key_masked: string;
  base_url: string;
}

export interface LlmConfigDto {
  providers: LlmProviderDto[];
}

export function useLlmConfig(): UseQueryResult<LlmConfigDto, Error> {
  return useQuery({
    queryKey: ['system-settings-llm'],
    queryFn: () => apiFetch<LlmConfigDto>('GET', '/system/settings/llm'),
  });
}

export function useUpdateLlmProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      name,
      api_key,
      base_url,
    }: {
      name: string;
      api_key?: string;
      base_url?: string;
    }) =>
      apiFetch<LlmProviderDto>(
        'PUT',
        `/system/settings/llm/providers/${encodeURIComponent(name)}`,
        { api_key, base_url },
      ),
    onSuccess: (out) => {
      void message.success(`已保存模型服务配置：${out.name}`);
      void qc.invalidateQueries({ queryKey: ['system-settings-llm'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useRestartPlatform() {
  return useMutation({
    mutationFn: () => apiFetch<{ status: string }>('POST', '/system:restart'),
  });
}

// system.py LogEntry（extra allow——component 等扩展字段）
export interface LogEntryDto {
  ts: string;
  level: string;
  message: string;
  component?: string;
}

export function useSystemLogs(
  params?: { level?: string },
): UseQueryResult<{ items: LogEntryDto[] }, Error> {
  const qs = params?.level ? `?level=${encodeURIComponent(params.level)}` : '';
  return useQuery({
    queryKey: ['system-logs', params?.level ?? null],
    queryFn: () =>
      apiFetch<{ items: LogEntryDto[] }>('GET', `/system/logs${qs}`),
  });
}

// audit_logs.py AuditEntryOut：query 参数 cursor/limit/action/actor
export interface AuditEntry {
  ts: string;
  actor: string;
  action: string;
  object_type: string;
  object_id: string;
  detail: Record<string, unknown>;
}

export function useAuditLogs(
  params?: { action?: string; actor?: string },
): UseQueryResult<{ items: AuditEntry[] }, Error> {
  const qs = new URLSearchParams();
  if (params?.action) qs.set('action', params.action);
  if (params?.actor) qs.set('actor', params.actor);
  const s = qs.toString();
  return useQuery({
    queryKey: ['audit-logs', params?.action ?? null, params?.actor ?? null],
    queryFn: () =>
      apiFetch<{ items: AuditEntry[] }>(
        'GET',
        `/audit-logs${s ? `?${s}` : ''}`,
      ),
  });
}

// credentials.py：Kind = device_ssh|nms_api|llm_api|backend_ssh；
// CredentialOut 刻意无 secret/指纹字段——列表展示 id（密钥库引用）
export const CREDENTIAL_KINDS = [
  'device_ssh',
  'nms_api',
  'llm_api',
  'backend_ssh',
] as const;
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number];

export interface Credential {
  id: string;
  name: string;
  kind: string;
  created_at: string;
}

export function useCredentials(): UseQueryResult<
  { items: Credential[] },
  Error
> {
  return useQuery({
    queryKey: ['credentials'],
    queryFn: () => apiFetch<{ items: Credential[] }>('GET', '/credentials'),
  });
}

export function useCreateCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; kind: CredentialKind; secret: string }) =>
      apiFetch<Credential>('POST', '/credentials', body),
    onSuccess: (out) => {
      // secret 永不回显——成功后仅提示密钥库引用 id
      void message.success(`凭证已创建（引用 ${out.id}）`);
      void qc.invalidateQueries({ queryKey: ['credentials'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useRotateCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, secret }: { id: string; secret: string }) =>
      apiFetch<void>(
        'POST',
        `/credentials/${encodeURIComponent(id)}:rotate`,
        { secret },
      ),
    onSuccess: () => {
      void message.success('凭证已轮换');
      void qc.invalidateQueries({ queryKey: ['credentials'] });
    },
    // 503 = credential store 未配置（后端 N-8：拒绝假装成功）
    onError: (err) => void message.error(err.message),
  });
}

// users.py：GET/POST /users + PUT /users/{user_id}（role/disabled）；
// 后端无 reset-password 端点、无 created_at 字段——按实际对齐
export interface UserRow {
  id: string;
  username: string;
  role: string;
  disabled: boolean;
}

export function useUsers(): UseQueryResult<{ items: UserRow[] }, Error> {
  return useQuery({
    queryKey: ['users'],
    queryFn: () => apiFetch<{ items: UserRow[] }>('GET', '/users'),
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { username: string; password: string; role: string }) =>
      apiFetch<UserRow>('POST', '/users', body),
    onSuccess: () => {
      void message.success('用户已创建');
      void qc.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      role?: string;
      disabled?: boolean;
    }) => apiFetch<void>('PUT', `/users/${encodeURIComponent(id)}`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// users.py：POST /users/{user_id}:reset-password（admin，密码 ≥8 位，204）
export function useResetPassword() {
  return useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) =>
      apiFetch<void>(
        'POST',
        `/users/${encodeURIComponent(id)}:reset-password`,
        { password },
      ),
    onSuccess: () => {
      void message.success('密码已重置');
    },
    onError: (err) => void message.error(err.message),
  });
}

// ── T7 SUT 管理页 ──────────────────────────────────────────────────────────

export function useSut(id: string | undefined): UseQueryResult<Sut, Error> {
  return useQuery({
    queryKey: ['sut', id],
    queryFn: () => apiFetch<Sut>('GET', `/suts/${id}`),
    enabled: !!id,
  });
}

// POST /suts（SutCreate：name/carrier/image_ref/description/optimization_scope）
export function useCreateSut() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name: string;
      carrier: string;
      image_ref: string;
      description?: string;
      optimization_scope?: string[];
    }) => apiFetch<Sut>('POST', '/suts', body),
    onSuccess: (out) => {
      void message.success(`SUT ${out.name} 已创建`);
      void qc.invalidateQueries({ queryKey: ['suts'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

export function useDeleteSut() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<void>('DELETE', `/suts/${id}`),
    onSuccess: () => {
      void message.success('SUT 已删除');
      void qc.invalidateQueries({ queryKey: ['suts'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// PUT /suts/{id}/optimization-scope（U7b：编辑 RSI 可优化范围；null/[]=自动模式）
export function useUpdateSutScope(id: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (optimization_scope: string[] | null) =>
      apiFetch<Sut>(
        'PUT',
        `/suts/${id}/optimization-scope`,
        { optimization_scope },
      ),
    onSuccess: () => {
      void message.success('优化范围已保存');
      void qc.invalidateQueries({ queryKey: ['sut', id] });
      void qc.invalidateQueries({ queryKey: ['suts'] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// GET /suts/{id}/verify-image → {ok, image_ref, checks: string[], detail}
export interface ImageCheckResult {
  ok: boolean | null;
  image_ref: string;
  checks: string[];
  detail: string;
}

export function useVerifyImage() {
  return useMutation({
    mutationFn: (sutId: string) =>
      apiFetch<ImageCheckResult>('GET', `/suts/${sutId}/verify-image`),
  });
}

// GET /suts/{id}/lineage → {nodes, edges}（版本谱系——列表/详情共用）
export interface LineageNode {
  version_id: string;
  label: string;
  kind: string;
  parent_ids: string[];
  experiment_ids: string[];
}

export interface LineageGraph {
  nodes: LineageNode[];
  edges: [string, string][];
}

export function useSutLineage(
  sutId: string | undefined,
): UseQueryResult<LineageGraph, Error> {
  return useQuery({
    queryKey: ['sut-lineage', sutId],
    queryFn: () => apiFetch<LineageGraph>('GET', `/suts/${sutId}/lineage`),
    enabled: !!sutId,
  });
}

// GET /suts/{id}/files（无 version → staging 全文件 {path: content}）
export function useSutFiles(
  sutId: string | undefined,
): UseQueryResult<Record<string, string>, Error> {
  return useQuery({
    queryKey: ['sut-files', sutId],
    queryFn: async () =>
      (
        await apiFetch<{ files: Record<string, string> }>(
          'GET',
          `/suts/${sutId}/files`,
        )
      ).files,
    enabled: !!sutId,
  });
}

// PUT /suts/{id}/files/{path} body {content, commit, message} → 204；
// 越界/冻结路径 → 409（409 由调用方行内提示，这里只 invalidate）
export function useWriteSutFile(sutId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      path,
      content,
      commit,
      message,
    }: {
      path: string;
      content: string;
      commit: boolean;
      message: string;
    }) =>
      apiFetch<void>(
        'PUT',
        `/suts/${sutId}/files/${path.split('/').map(encodeURIComponent).join('/')}`,
        { content, commit, message },
      ),
    onSuccess: (_d, vars) => {
      void message.success(
        vars.commit
          ? '已提交新版本'
          : '已存入 staging 区（开启 commit 后提交版本）',
      );
      void qc.invalidateQueries({ queryKey: ['sut-files', sutId] });
      void qc.invalidateQueries({ queryKey: ['sut-lineage', sutId] });
    },
    onError: (err) => void message.error(err.message),
  });
}

// POST /suts/{id}/files（新建文件，path 在 body）→ 201
export function useCreateSutFile(sutId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      path: string;
      content: string;
      commit?: boolean;
      message?: string;
    }) => apiFetch<void>('POST', `/suts/${sutId}/files`, body),
    onSuccess: () => {
      void message.success('文件已新建');
      void qc.invalidateQueries({ queryKey: ['sut-files', sutId] });
    },
    onError: (err) => void message.error(err.message),
  });
}
