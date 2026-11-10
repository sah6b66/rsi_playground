import { http, HttpResponse } from 'msw';

export const loginHandler = http.post(
  '/api/v1/auth/login',
  async ({ request }) => {
    const body = (await request.json()) as {
      username?: string;
      password?: string;
    };
    if (body.username === 'admin' && body.password === 'rsi-admin') {
      return HttpResponse.json({
        token: 'test-token',
        expires_at: 1893456000,
        user_id: 'u-admin',
        role: 'admin',
      });
    }
    return HttpResponse.json({ detail: 'invalid credentials' }, { status: 401 });
  },
);

export const meHandler = http.get('/api/v1/auth/me', () =>
  HttpResponse.json({
    user_id: 'u-admin',
    role: 'admin',
    expires_at: 1893456000,
  }),
);

// handlers 导出见文件末尾

// ── Task 2 Dashboard fixtures（字段对齐 rsi/api/routers/*）───────────────
// iterations: IterationOut 字段；suts: SutPage；labs: LabPage；
// sandbox/backends、instances、system/dependencies、system/health 按实际响应 shape。

const iteration = (over: Record<string, unknown>) => ({
  id: 'iter-00000000',
  sut_id: 'sut-1a2b3c4d',
  profile_id: 'prof-9f8e7d6c',
  mode: 'p1',
  state: 'running',
  current_step: null,
  round: 1,
  experiments: [],
  budget_used: { tokens: 120000 },
  archived: false,
  created_at: '2026-11-05T08:00:00Z',
  ...over,
});

// ── Task 3 会话 fixtures（可变：POST 成功后追加，测试间重置）──────────────
const baseSessions = () => [
  iteration({
    id: 'iter-aaaa1111',
    sut_id: 'sut-1',
    label: 'BGP排障优化-第一轮',
    state: 'running',
    current_step: 'attributing',
    round: 3,
    budget_used: { tokens: 340000 },
  }),
  iteration({
    id: 'iter-bbbb2222',
    sut_id: 'sut-2',
    state: 'paused_awaiting',
    current_step: 'awaiting_hypothesis',
    round: 2,
    budget_used: { tokens: 210000 },
  }),
  iteration({
    id: 'iter-cccc3333',
    sut_id: 'sut-3',
    state: 'finalized',
    current_step: null,
    round: 6,
    budget_used: { tokens: 890000 },
  }),
];
let sessions: ReturnType<typeof baseSessions>;
export const resetSessionFixtures = () => {
  sessions = baseSessions();
};
resetSessionFixtures();
const sessionItems = () => sessions;

// ── Task 6 评测集 fixtures（可变：克隆/导入/删除后变化，测试间重置）──────
interface EvalsetFixture {
  id: string;
  name: string;
  purpose: string;
  tasks: { id: string }[];
  description: string;
  frozen: boolean;
  version: number;
  created_at: string;
  env: Record<string, unknown> | null;
  env_parsed: {
    name: string | null;
    backend: string | null;
    devices: {
      name: string;
      type: string;
      version: string | null;
      ip: string;
      port: number;
    }[];
    config_apis: { name: string; base_url: string }[];
  } | null;
}
const baseEvalsets = (): EvalsetFixture[] => [
  {
    id: 'eval-0001',
    name: 'netops-bgp-triage',
    purpose: 'iterate',
    tasks: [{ id: 't-001' }, { id: 't-002' }],
    description: '',
    frozen: true,
    version: 1,
    created_at: '2026-11-01T00:00:00Z',
    env: {
      backend: 'ensp-pro',
      devices: [{ name: 'R1', type: 'vrp' }],
      ssh_access: { R1: { ip: '192.168.56.10', port: 22, username: 'admin' } },
    },
    // U7a 详情页摘要（凭证剥离）
    env_parsed: {
      name: 'env-bgp',
      backend: 'ensp-pro',
      devices: [
        { name: 'R1', type: 'vrp', version: '8.180', ip: '192.168.56.10', port: 22 },
      ],
      config_apis: [{ name: 'nms', base_url: 'http://10.0.0.9/api' }],
    },
  },
  {
    id: 'eval-0002',
    name: 'netops-smoke',
    purpose: 'smoke',
    tasks: [{ id: 't-101' }],
    description: '',
    frozen: true,
    version: 1,
    created_at: '2026-11-01T00:00:00Z',
    env: null,
    env_parsed: null,
  },
];
let evalsetData: ReturnType<typeof baseEvalsets>;
const evalsets = () => evalsetData;
export const resetEvalsetFixtures = () => {
  evalsetData = baseEvalsets();
};
resetEvalsetFixtures();

// 向导提交请求体捕获（sessions.test.tsx 断言用）
export const iterationCreateRequests: unknown[] = [];

export const sessionsHandlers = [
  // POST /iterations：sut-2 已有活跃会话 → 409（同后端 ConflictError detail 形态）
  http.post('/api/v1/iterations', async ({ request }) => {
    const body = await request.json();
    iterationCreateRequests.length = 0;
    iterationCreateRequests.push(body);
    if ((body as { sut_id?: string }).sut_id === 'sut-2') {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message:
              "sut 'sut-2' already has an active iteration 'iter-bbbb2222'",
          },
        },
        { status: 409 },
      );
    }
    const id = 'iter-dddd4444';
    sessions = [
      iteration({
        id,
        sut_id: (body as { sut_id?: string }).sut_id ?? 'sut-1',
        label: (body as { label?: string }).label ?? null,
        state: 'attributing',
        current_step: 'awaiting_direction',
        round: 1,
        budget_used: { tokens: 0 },
      }),
      ...sessions,
    ];
    return HttpResponse.json({ iteration_id: id }, { status: 201 });
  }),
  // 会话操作（T3 列表）：pause/resume/abort → IterationOut；archive → 204
  http.post(/\/api\/v1\/iterations\/[^/]+:pause$/, () =>
    HttpResponse.json(iteration({ state: 'paused_awaiting' })),
  ),
  http.post(/\/api\/v1\/iterations\/[^/]+:resume$/, () =>
    HttpResponse.json(iteration({ state: 'attributing' })),
  ),
  http.post(/\/api\/v1\/iterations\/[^/]+:abort$/, () =>
    HttpResponse.json(iteration({ state: 'aborted' })),
  ),
  http.delete(/\/api\/v1\/iterations\/[^/]+$/, () => new HttpResponse(null, { status: 204 })),
  // 评测集列表（evalsets.py EvalsetPage shape；T6 起 env 为解析后 env.yaml）
  http.get('/api/v1/evalsets', () =>
    HttpResponse.json({
      items: evalsets(),
      next_cursor: null,
      has_more: false,
    }),
  ),
  // U7a 评测集详情
  http.get(/\/api\/v1\/evalsets\/[^/]+$/, ({ request }) => {
    const id = new URL(request.url).pathname.split('/').pop();
    const es = evalsets().find((e) => e.id === id);
    if (!es)
      return HttpResponse.json({ detail: 'not found' }, { status: 404 });
    return HttpResponse.json(es);
  }),
  // 运行环境（T6 /carrier/hosts 契约 stub；本任务 hooks 按该契约消费）
  http.get('/api/v1/carrier/hosts', () =>
    HttpResponse.json({
      items: [
        {
          key: 'local',
          mode: 'local',
          endpoint: 'local://docker',
          online: true,
          active_instances: 0,
          max_instances: 2,
          subnets: [],
        },
        {
          key: 'remote-1',
          mode: 'remote',
          endpoint: 'ssh://user@192.168.1.50',
          online: true,
          active_instances: 1,
          max_instances: 2,
          subnets: [],
        },
      ],
    }),
  ),
];

export const dashboardHandlers = [  http.get('/api/v1/iterations', () =>
    HttpResponse.json({
      items: sessionItems(),
      next_cursor: null,
      has_more: false,
    }),
  ),
  http.get('/api/v1/suts', () =>
    HttpResponse.json({
      items: [1, 2, 3, 4, 5].map((n) => ({
        id: `sut-${n}`,
        name: `ipops-agent-${n}`,
        carrier: 'opencode',
        image_ref: `ipops-agent:v1.${n}`,
        description: '',
        created_at: '2026-11-01T00:00:00Z',
      })),
      next_cursor: null,
      has_more: false,
    }),
  ),
  http.get('/api/v1/labs', () =>
    HttpResponse.json({
      items: [
        {
          id: 'lab-1',
          sut_id: 'sut-1',
          status: 'running',
          online: true,
          topology: {},
          created_at: '2026-11-05T07:00:00Z',
        },
        {
          id: 'lab-2',
          sut_id: 'sut-2',
          status: 'idle',
          online: false,
          topology: {},
          created_at: '2026-11-05T06:00:00Z',
        },
      ],
    }),
  ),
  http.get('/api/v1/sandbox/backends', () =>
    HttpResponse.json({
      items: [
        {
          name: 'ensp-pro-1',
          kind: 'ensp-pro',
          status: 'ready',
          available: true,
          active_labs: 1,
          detail: null,
        },
        {
          name: 'pnetlab',
          kind: 'pnetlab',
          status: 'offline',
          available: false,
          active_labs: 0,
          detail: 'connect timeout',
        },
        {
          name: 'fake',
          kind: 'fake',
          status: 'ready',
          available: true,
          active_labs: 0,
          detail: null,
        },
        {
          name: 'phys-1',
          kind: 'physical',
          status: 'ready',
          available: true,
          active_labs: 0,
          detail: '设备 SSH 可达（平台配置）',
          device_count: 2,
          devices: {
            R1: { ip: '192.168.56.10', port: 22 },
            SW2: { ip: '192.168.56.11', port: 2222 },
          },
        },
      ],
    }),
  ),
  http.get('/api/v1/instances', () =>
    HttpResponse.json({
      items: [
        { id: 'inst-1', sut_id: 'sut-1', lab_id: 'lab-1', status: 'running' },
        { id: 'inst-2', sut_id: 'sut-2', lab_id: null, status: 'running' },
        { id: 'inst-3', sut_id: 'sut-3', lab_id: null, status: 'stopped' },
      ],
    }),
  ),
  http.get('/api/v1/system/dependencies', () =>
    HttpResponse.json({
      source: 'monitor',
      items: [
        { name: 'platform:ensp-pro-1', status: 'ok', latency_ms: 12.5 },
        {
          name: 'platform:pnetlab',
          status: 'down',
          latency_ms: null,
          detail: 'connect timeout',
        },
        // 以下三项（docker/lab/instances）在前端环境状态面板中隐藏
        { name: 'docker', status: 'ok', latency_ms: 0 },
        { name: 'lab', status: 'ok', latency_ms: 1 },
        { name: 'instances', status: 'ok', latency_ms: 0 },
        { name: 'db', status: 'ok', latency_ms: 0.6 },
        { name: 'llm', status: 'ok', latency_ms: 211 },
      ],
    }),
  ),
  http.get('/api/v1/system/health', () => HttpResponse.json({ status: 'ok' })),
];

// ── Task 4 会话详情 fixtures ───────────────────────────────────────────────
// paused@awaiting_hypothesis 会话 + 2 轮 timeline + 门② 201/409。
export const detailId = 'iter-bbbb2222';

export const hypothesisRequests: unknown[] = [];
// 测试切换：true 时门② 返回 409（step 不符）
export let hypothesisShould409 = false;
export const setHypothesisShould409 = (v: boolean) => {
  hypothesisShould409 = v;
};

const timeline = {
  iteration_id: detailId,
  rounds: [
    {
      round: 1,
      status: 'finalized',
      batch_ids: ['bat-1'],
      config_version_before: 1,
      config_version_after: 2,
      files_changed: [{ path: 'skill/bgp.md', target: 'skill' }],
      tasks: [
        {
          task_id: 't-001', batch_id: 'bat-1', run_id: 'run-1',
          status: 'succeeded', tokens: 100, latency_ms: 1200,
          root_cause_match: true,
        },
        {
          task_id: 't-002', batch_id: 'bat-1', run_id: 'run-2',
          status: 'succeeded', tokens: 200, latency_ms: 1800,
          root_cause_match: false,
        },
      ],
      scores: { accuracy: 0.5, noise_band: [0.6, 0.7] },
    },
    {
      round: 2,
      status: 'finalized',
      batch_ids: ['bat-2'],
      config_version_before: 2,
      config_version_after: 3,
      files_changed: [{ path: 'skill/ospf.md', target: 'skill' }],
      tasks: [
        {
          task_id: 't-003', batch_id: 'bat-2', run_id: 'run-3',
          status: 'succeeded', tokens: 150, latency_ms: 900,
          root_cause_match: true,
        },
        {
          // 同任务 t-001 第 2 轮复跑：轨迹跨轮对比 fixture（T5）
          task_id: 't-001', batch_id: 'bat-2', run_id: 'run-4',
          status: 'env_failed', tokens: null, latency_ms: null,
          root_cause_match: null,
        },
      ],
      scores: { accuracy: 0.8, noise_band: [0.6, 0.7] },
    },
  ],
  summary: {
    accuracy_series: [
      { round: 1, value: 0.5 },
      { round: 2, value: 0.8 },
    ],
    tokens_series: [
      { round: 1, value: 300 },
      { round: 2, value: 150 },
    ],
    latency_series: [
      { round: 1, value: 1500 },
      { round: 2, value: 900 },
    ],
  },
};

export const sessionDetailHandlers = [
  http.get(`/api/v1/iterations/${detailId}`, () =>
    HttpResponse.json(
      iteration({
        id: detailId,
        sut_id: 'sut-2',
        state: 'paused',
        current_step: 'awaiting_hypothesis',
        round: 2,
        budget_used: { tokens: 210000 },
      }),
    ),
  ),
  http.get(`/api/v1/iterations/${detailId}/timeline`, () =>
    HttpResponse.json(timeline),
  ),
  http.post(`/api/v1/iterations/${detailId}/hypothesis`, async ({ request }) => {
    hypothesisRequests.length = 0;
    hypothesisRequests.push(await request.json());
    if (hypothesisShould409) {
      return HttpResponse.json(
        {
          detail: {
            code: 'INVALID_TRANSITION',
            message:
              "gate 'hypothesis' expects current_step 'awaiting_hypothesis', got 'awaiting_review'",
          },
        },
        { status: 409 },
      );
    }
    return HttpResponse.json({ hypothesis_id: 'hyp-1234abcd' }, { status: 201 });
  }),
];

// ── Task 5 fixtures：versions diff / sut 文件内容 / run 轨迹 ───────────────
// versions.py GET /versions/{a}/diff/{b} → [{path, op, patch}]
const versionDiff = (a: string, b: string) => [
  {
    path: 'skill/bgp.md',
    op: 'modified',
    patch: '@@ -1,3 +1,4 @@\n # BGP 排查\n+先查邻居状态\n 检查 peer',
  },
  {
    path: 'mcp/tools.md',
    op: 'added',
    patch: '@@ -0,0 +1,2 @@\n+# 工具清单\n+show bgp summary',
  },
  ...(a === '2'
    ? [
        {
          path: 'skill/ospf.md',
          op: 'modified',
          patch: '@@ -1 +1 @@\n-hello 30s\n+hello 10s',
        },
      ]
    : []),
];

// suts.py list_sut_files：version 1 = 旧文；2/3 = 新文
const sutFileBodies: Record<string, string> = {
  'skill/bgp.md': '# BGP 排查\n检查 peer 状态\n查看路由表\n',
  'skill/ospf.md': '# OSPF 排查\n检查 hello 定时器\n',
  'mcp/tools.md': '# 工具清单\nshow bgp summary\nshow ip route\n',
};
const sutFileVersions: Record<string, Record<string, string>> = {
  '1': {
    'skill/bgp.md': '# BGP 排查\n检查 peer 状态\n',
    'skill/ospf.md': '# OSPF 排查\n',
    'mcp/tools.md': '',
  },
  '2': sutFileBodies,
  '3': sutFileBodies,
};

// runs.py GET /runs/{id}/traces（事件 shape 对齐 core/models.py TraceEvent）
const traceEvent = (
  seq: number,
  event_type: string,
  data: Record<string, unknown>,
  offsetMs = 0,
) => ({
  run_id: 'run-1',
  task_id: 't-001',
  lab_id: 'lab-1',
  sut_id: 'sut-2',
  seq,
  ts: new Date(Date.parse('2026-11-05T08:00:00Z') + offsetMs).toISOString(),
  event_type,
  model: 'gpt-test',
  data,
  duration_ms: null,
  session_id: null,
  step_seq: null,
  late: false,
});
const tracesByRun: Record<string, unknown[]> = {
  'run-1': [
    traceEvent(1, 'run_started', { profile: 'p1', evalset: 'eval-0001' }),
    traceEvent(
      2,
      'llm_request',
      { system: '你是网络排障智能体', prompt: 'BGP 邻居 down 排查' },
      120,
    ),
    traceEvent(3, 'tool_call', { cli: 'show bgp summary', device: 'leaf-1' }, 300),
    traceEvent(
      4,
      'tool_result',
      { cli: 'show bgp summary', rc: 0, stdout: 'BGP neighbor is.Established' },
      480,
    ),
    traceEvent(5, 'llm_response', { content: '邻居已建立，检查路由', tokens: 180 }, 700),
    traceEvent(6, 'run_finalized', { verdict: 'pass', score: 1.0 }, 1200),
  ],
  'run-4': [
    { ...traceEvent(1, 'run_started', { profile: 'p1' }), run_id: 'run-4' },
    {
      ...traceEvent(2, 'tool_call', { cli: 'show ip interface brief', device: 'leaf-1' }, 200),
      run_id: 'run-4',
    },
    {
      ...traceEvent(
        3,
        'env_failed',
        { reason: 'device unreachable', device: 'spine-2' },
        900,
      ),
      run_id: 'run-4',
    },
    { ...traceEvent(4, 'run_finalized', { verdict: 'env_failed' }, 1500), run_id: 'run-4' },
  ],
};

export const task5Handlers = [
  http.get(/\/api\/v1\/versions\/[^/]+\/diff\/[^/]+$/, ({ request }) => {
    const m = request.url.match(/\/versions\/([^/]+)\/diff\/([^/]+)$/)!;
    return HttpResponse.json(versionDiff(decodeURIComponent(m[1]), decodeURIComponent(m[2])));
  }),
  http.get('/api/v1/suts/sut-2/files', ({ request }) => {
    const url = new URL(request.url);
    const path = url.searchParams.get('path') ?? '';
    const version = url.searchParams.get('version') ?? '';
    const content = sutFileVersions[version]?.[path];
    if (content === undefined) {
      return HttpResponse.json({ detail: `no file '${path}' @ v${version}` }, { status: 404 });
    }
    return HttpResponse.json({ files: { [path]: content } });
  }),
  http.get(/\/api\/v1\/runs\/[^/]+\/traces$/, ({ request }) => {
    const runId = new URL(request.url).pathname.split('/')[4]!;
    const events = tracesByRun[runId];
    if (!events) return HttpResponse.json({ detail: `run '${runId}' not found` }, { status: 404 });
    return HttpResponse.json({ items: events, next_cursor: null, has_more: false });
  }),
];

export const handlers = [
  loginHandler,
  meHandler,
  ...dashboardHandlers,
  ...sessionsHandlers,
  ...sessionDetailHandlers,
  ...task5Handlers,
];

// ── Task 6 资源页 fixtures：镜像 / 主机 / 沙箱 / 克隆 ─────────────────────
// 请求捕获（resources.test.tsx 断言用）
export const resourceRequests: Record<string, unknown[]> = {
  imageLoad: [],
  carrierHosts: [],
  sandboxBackend: [],
  clone: [],
};

const dockerImages = () => ({
  items: [
    {
      host: 'main',
      ref: 'ipops-agent:v1.2',
      size_mb: 512.0,
      created_at: '2026-11-01T00:00:00Z',
      used_by: ['sut-1'],
    },
    {
      host: 'main',
      ref: 'busybox:latest',
      size_mb: 4.3,
      created_at: '2026-10-01T00:00:00Z',
      used_by: [],
    },
    {
      host: 'remote-1',
      ref: 'ipops-agent:v1.1',
      size_mb: 500.0,
      created_at: '2026-09-01T00:00:00Z',
      used_by: [],
    },
  ],
  offline_hosts: ['remote-2'],
});

export const resourcesHandlers = [
  http.get('/api/v1/docker/images', () => HttpResponse.json(dockerImages())),
  http.post('/api/v1/docker/images:load', async ({ request }) => {
    const body = (await request.json()) as { host?: string; tar_path?: string };
    resourceRequests.imageLoad.push(body);
    if ((body.tar_path ?? '').includes('missing')) {
      return HttpResponse.json(
        { code: 'NOT_FOUND', message: 'tar file not found on host' },
        { status: 404 },
      );
    }
    return HttpResponse.json(
      { status: 'loaded', host: body.host ?? 'main' },
      { status: 202 },
    );
  }),
  http.delete(/\/api\/v1\/docker\/images\/.+$/, ({ request }) => {
    const ref = decodeURIComponent(
      new URL(request.url).pathname.split('/').pop() ?? '',
    );
    if (ref === 'ipops-agent:v1.2') {
      return HttpResponse.json(
        {
          code: 'CONFLICT',
          message: "image 'ipops-agent:v1.2' is used by suts ['sut-1']",
          detail: { used_by: ['sut-1'] },
        },
        { status: 409 },
      );
    }
    return new HttpResponse(null, { status: 204 });
  }),
  http.put('/api/v1/carrier/hosts', async ({ request }) => {
    resourceRequests.carrierHosts.push(await request.json());
    return HttpResponse.json({ restart_required: true });
  }),
  http.put(/\/api\/v1\/sandbox\/backends\/[^/]+$/, async ({ request }) => {
    resourceRequests.sandboxBackend.push({
      key: new URL(request.url).pathname.split('/').pop(),
      body: await request.json(),
    });
    return HttpResponse.json({ restart_required: true });
  }),
  http.post(/\/api\/v1\/evalsets\/[^/]+:clone$/, async ({ request }) => {
    const body = (await request.json()) as { new_name?: string };
    resourceRequests.clone.push(body);
    if (evalsets().some((e) => e.name === body.new_name)) {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message: `evalset name '${body.new_name}' already exists`,
          },
        },
        { status: 409 },
      );
    }
    const id = `eval-new-${evalsets().length + 1}`;
    const clone = {
      id,
      name: body.new_name ?? 'cloned',
      purpose: 'iterate',
      tasks: [{ id: 't-001' }, { id: 't-002' }],
      description: 'cloned',
      frozen: false,
      version: 1,
      created_at: '2026-11-05T00:00:00Z',
      env: {
        backend: 'ensp-pro',
        devices: [{ name: 'R1', type: 'vrp' }],
        ssh_access: { R1: { ip: '10.201.0.10', port: 2222, username: 'admin' } },
      },
      env_parsed: {
        name: null,
        backend: 'ensp-pro',
        devices: [
          { name: 'R1', type: 'vrp', version: null, ip: '10.201.0.10', port: 2222 },
        ],
        config_apis: [],
      },
    };
    evalsetData = [...evalsets(), clone];
    return HttpResponse.json(clone, { status: 201 });
  }),
  http.post('/api/v1/evalsets:import', async ({ request }) => {
    const form = await request.formData();
    const name = String(form.get('name') || '') || 'imported-set';
    if (evalsets().some((e) => e.name === name)) {
      return HttpResponse.json(
        { code: 'CONFLICT', message: `evalset name '${name}' already exists` },
        { status: 409 },
      );
    }
    const es = {
      id: `eval-imp-${evalsets().length + 1}`,
      name,
      purpose: 'iterate',
      tasks: [{ id: 't-900' }],
      description: 'imported',
      frozen: false,
      version: 1,
      created_at: '2026-11-05T00:00:00Z',
      env: null,
      env_parsed: null,
    };
    evalsetData = [...evalsets(), es];
    return HttpResponse.json(es, { status: 201 });
  }),
  http.delete(/\/api\/v1\/evalsets\/[^/]+$/, ({ request }) => {
    const id = new URL(request.url).pathname.split('/').pop() ?? '';
    evalsetData = evalsets().filter((e) => e.id !== id);
    return new HttpResponse(null, { status: 204 });
  }),
];

handlers.push(...resourcesHandlers);

// ── Task 7 fixtures：settings 六 Tab + SUT 管理 ────────────────────────────
// 请求捕获（settings/suts 测试断言用）
export const t7Requests: Record<string, unknown[]> = {
  settingPut: [],
  userCreate: [],
  userUpdate: [],
  userResetPassword: [],
  credCreate: [],
  credRotate: [],
  sutCreate: [],
  sutScopePut: [],
  filePut: [],
  fileCreate: [],
};

// system.py SettingItem 形态：{path, value, source, mutable}（静态 → PUT 409）
type SettingRow = { path: string; value: unknown; source: string; mutable: boolean };
// 与后端 deps.py default_stores["settings"] 键清单一致（8 项）
const baseSettings = (): SettingRow[] => [
  { path: 'sandbox.build_retry', value: 2, source: 'config', mutable: true },
  { path: 'sandbox.ready_timeout_s', value: 600, source: 'config', mutable: true },
  { path: 'judge.det_timeout_s', value: 120, source: 'config', mutable: true },
  { path: 'judge.llm_retry', value: 2, source: 'config', mutable: true },
  { path: 'logging.level', value: 'info', source: 'config', mutable: true },
  { path: 'judge.llm.model', value: 'default', source: 'config', mutable: true },
  { path: 'budget.llm.daily', value: 100, source: 'runtime', mutable: true },
  { path: 'platform.static_item', value: 'static', source: 'runtime', mutable: false },
];
let settingsData: SettingRow[];
export const resetT7Settings = () => {
  settingsData = baseSettings();
};

// users.py UserOut：{id, username, role, disabled}（无 created_at/reset-password）
type UserRowData = { id: string; username: string; role: string; disabled: boolean };
const baseUsers = (): UserRowData[] => [
  { id: 'u-admin', username: 'admin', role: 'admin', disabled: false },
  { id: 'u-eng-1', username: 'eng-1', role: 'agent_engineer', disabled: false },
];
let usersData: UserRowData[];
export const resetT7Users = () => {
  usersData = baseUsers();
};

const auditEntries = [
  {
    ts: '2026-11-05T08:00:00Z',
    actor: 'admin',
    action: 'sut.create',
    object_type: 'sut',
    object_id: 'sut-t7',
    detail: { name: 'ipops-agent', image_ref: 'ipops-agent:v1.2' },
  },
  {
    ts: '2026-11-05T08:05:00Z',
    actor: 'eng-1',
    action: 'sut.file_write',
    object_type: 'sut',
    object_id: 'sut-t7',
    detail: { path: 'skills/bgp.md', commit: false },
  },
];

const logEntries = [
  { ts: '2026-11-05T08:00:00Z', level: 'INFO', message: 'iteration started', component: 'engine' },
  { ts: '2026-11-05T08:01:00Z', level: 'ERROR', message: 'device unreachable', component: 'sandbox' },
];

// suts 详情 fixture（区别于 dashboardHandlers 的 sut-1..5，避免 handler 冲突）
export const t7SutId = 'sut-t7';
const t7Sut = () => ({
  id: t7SutId,
  name: 'ipops-agent',
  carrier: 'opencode',
  image_ref: 'ipops-agent:v1.2',
  description: 'Network ops SUT',
  created_at: '2026-11-01T00:00:00Z',
});
const t7Files: Record<string, string> = {
  'skills/bgp.md': '# BGP triage\ncheck peers\n',
  'judge/rubric.md': '# judge rubric (frozen fixture)\n',
};
// 与后端 _validate_editable 同构的白名单前缀（D-12）
const EDITABLE_RE =
  /^(system-prompt(\.md)?|skills\/|scripts\/|mcp\.json|mcp-src\/|memory\/|knowledge\/|model\.json)/;

export const settingsHandlers = [
  http.get('/api/v1/system/settings', () =>
    HttpResponse.json({ items: settingsData }),
  ),
  http.put(/\/api\/v1\/system\/settings\/.+$/, async ({ request }) => {
    const path = decodeURIComponent(
      new URL(request.url).pathname.replace('/api/v1/system/settings/', ''),
    );
    const body = (await request.json()) as { value?: unknown };
    t7Requests.settingPut.push({ path, body });
    const item = settingsData.find((s) => s.path === path);
    if (!item) {
      return HttpResponse.json(
        { detail: { code: 'CONFLICT', message: `unknown setting '${path}'` } },
        { status: 409 },
      );
    }
    if (!item.mutable) {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message: `setting '${path}' is static and cannot be changed at runtime`,
          },
        },
        { status: 409 },
      );
    }
    item.value = body.value;
    return HttpResponse.json({ ...item });
  }),
  http.get('/api/v1/system/freeze', () => HttpResponse.json({ frozen: false })),
  http.get('/api/v1/system/logs', ({ request }) => {
    const level = new URL(request.url).searchParams.get('level');
    const items = level
      ? logEntries.filter((e) => e.level.toLowerCase() === level.toLowerCase())
      : logEntries;
    return HttpResponse.json({ items });
  }),
  http.get('/api/v1/audit-logs', ({ request }) => {
    const url = new URL(request.url);
    const action = url.searchParams.get('action');
    const actor = url.searchParams.get('actor');
    let items = auditEntries;
    if (action) items = items.filter((e) => e.action.includes(action));
    if (actor) items = items.filter((e) => e.actor === actor);
    return HttpResponse.json({ items });
  }),
  // credentials.py：响应无 secret/指纹字段（值永不回显）
  http.get('/api/v1/credentials', () =>
    HttpResponse.json({
      items: [
        {
          id: 'cred-vault-0001',
          name: 'core-sw-ssh',
          kind: 'device_ssh',
          created_at: '2026-11-01T00:00:00Z',
        },
      ],
    }),
  ),
  http.post('/api/v1/credentials', async ({ request }) => {
    const body = (await request.json()) as { name?: string; kind?: string; secret?: string };
    t7Requests.credCreate.push(body);
    return HttpResponse.json(
      {
        id: 'cred-vault-0002',
        name: body.name ?? '',
        kind: body.kind ?? 'llm_api',
        created_at: '2026-11-05T00:00:00Z',
      },
      { status: 201 },
    );
  }),
  http.post(/\/api\/v1\/credentials\/[^:]+:rotate$/, async ({ request }) => {
    t7Requests.credRotate.push(await request.json());
    return new HttpResponse(null, { status: 204 });
  }),
  // users.py：POST /users + PUT /users/{id}（role/disabled）
  http.get('/api/v1/users', () => HttpResponse.json({ items: usersData })),
  http.post('/api/v1/users', async ({ request }) => {
    const body = (await request.json()) as {
      username?: string;
      password?: string;
      role?: string;
    };
    t7Requests.userCreate.push(body);
    if (usersData.some((u) => u.username === body.username)) {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message: `username '${body.username}' already exists`,
          },
        },
        { status: 409 },
      );
    }
    const u: UserRowData = {
      id: `u-${body.username ?? ''}`,
      username: body.username ?? '',
      role: body.role ?? 'researcher',
      disabled: false,
    };
    usersData = [...usersData, u];
    return HttpResponse.json(u, { status: 201 });
  }),
  http.put(/\/api\/v1\/users\/[^/]+$/, async ({ request }) => {
    const id = new URL(request.url).pathname.split('/').pop() ?? '';
    const body = (await request.json()) as { role?: string; disabled?: boolean };
    t7Requests.userUpdate.push({ id, body });
    const u = usersData.find((x) => x.id === id);
    if (u) {
      if (body.disabled !== undefined) u.disabled = body.disabled;
      if (body.role !== undefined) u.role = body.role;
    }
    return new HttpResponse(null, { status: 204 });
  }),
  // users.py：POST /users/{user_id}:reset-password（admin，≥8 位，204）
  http.post(/\/api\/v1\/users\/[^/]+:reset-password$/, async ({ request }) => {
    const id =
      new URL(request.url).pathname.split('/').pop()?.replace(':reset-password', '') ?? '';
    const body = (await request.json()) as { password?: string };
    t7Requests.userResetPassword.push({ id, password: body.password });
    if (!usersData.some((u) => u.id === id)) {
      return HttpResponse.json(
        {
          detail: {
            code: 'NOT_FOUND',
            message: `user '${id}' not found`,
          },
        },
        { status: 404 },
      );
    }
    if ((body.password ?? '').length < 8) {
      return HttpResponse.json(
        {
          detail: {
            code: 'VALIDATION',
            message: 'password must be at least 8 characters',
          },
        },
        { status: 422 },
      );
    }
    return new HttpResponse(null, { status: 204 });
  }),
];

export const sutsHandlers = [
  // POST /suts：捕获 body（suts.test.tsx 断言提交形态）
  http.post('/api/v1/suts', async ({ request }) => {
    const body = (await request.json()) as {
      name?: string;
      carrier?: string;
      image_ref?: string;
    };
    t7Requests.sutCreate.push(body);
    if (body.name === 'dup') {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message: `sut name '${body.name}' already exists`,
          },
        },
        { status: 409 },
      );
    }
    return HttpResponse.json(
      { ...t7Sut(), name: body.name ?? '', image_ref: body.image_ref ?? '' },
      { status: 201 },
    );
  }),
  http.get(`/api/v1/suts/${t7SutId}`, () => HttpResponse.json(t7Sut())),
  // verify-image 为 GET（suts.py 实际）；checks 为 string 列表（carrier ImageCheck）
  http.get(/\/api\/v1\/suts\/[^/]+\/verify-image$/, () =>
    HttpResponse.json({
      ok: false,
      image_ref: 'ipops-agent:v1.2',
      checks: ['image exists on host main', 'container runs --version'],
      detail: 'docker: image not found',
    }),
  ),
  http.get(`/api/v1/suts/${t7SutId}/lineage`, () =>
    HttpResponse.json({
      nodes: [
        {
          version_id: 'v1',
          label: 'v1',
          kind: 'snapshot',
          parent_ids: [],
          experiment_ids: [],
        },
        {
          version_id: 'v2',
          label: 'v2',
          kind: 'commit',
          parent_ids: ['v1'],
          experiment_ids: ['exp-1'],
        },
      ],
      edges: [['v1', 'v2']],
    }),
  ),
  // GET files（无 path/version → staging 全文件 {path:content}）
  http.get(`/api/v1/suts/${t7SutId}/files`, ({ request }) => {
    const path = new URL(request.url).searchParams.get('path') ?? '';
    const files = path
      ? path in t7Files
        ? { [path]: t7Files[path]! }
        : {}
      : t7Files;
    return HttpResponse.json({ files });
  }),
  // PUT files：越界路径（非 suts/<id>/ 白名单）→ 409 CONFLICT（D-12）
  http.put(/\/api\/v1\/suts\/[^/]+\/files\/.+$/, async ({ request }) => {
    const path = decodeURIComponent(
      new URL(request.url).pathname.replace(/^\/api\/v1\/suts\/[^/]+\/files\//, ''),
    );
    const body = (await request.json()) as {
      content?: string;
      commit?: boolean;
      message?: string;
    };
    t7Requests.filePut.push({ path, body });
    if (!EDITABLE_RE.test(path)) {
      return HttpResponse.json(
        {
          detail: {
            code: 'CONFLICT',
            message: `path '${path}' outside the editable target domain`,
          },
        },
        { status: 409 },
      );
    }
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`/api/v1/suts/${t7SutId}/files`, async ({ request }) => {
    const body = (await request.json()) as { path?: string };
    t7Requests.fileCreate.push(body);
    return new HttpResponse(null, { status: 201 });
  }),
  http.delete(/\/api\/v1\/suts\/[^/]+$/, () => new HttpResponse(null, { status: 204 })),
  // U7b PUT optimization-scope：与后端 _validate_scope 同构（越域项 → 422；
  // fixture 约定 scripts/ 域在本测试环境视为已冻结，用于行内 422 错误断言）
  http.put(/\/api\/v1\/suts\/[^/]+\/optimization-scope$/, async ({ request }) => {
    const body = (await request.json()) as { optimization_scope?: string[] | null };
    const scope = body.optimization_scope ?? null;
    t7Requests.sutScopePut.push(scope);
    const invalid = (scope ?? []).filter(
      (i) =>
        i.startsWith('scripts/') ||
        !/^(system-prompt(\.md)?|skills\/.*|scripts\/.*|mcp\.json|mcp-src\/.*|memory\/.*|knowledge\/.*|model\.json)$/.test(i),
    );
    if (invalid.length) {
      return HttpResponse.json(
        {
          detail: {
            code: 'VALIDATION',
            message: `optimization_scope 项超出全局优化白名单域: ${invalid
              .map((i) => `'${i}'`)
              .join(', ')}`,
            invalid,
          },
        },
        { status: 422 },
      );
    }
    return HttpResponse.json({
      ...t7Sut(),
      optimization_scope: scope && scope.length ? scope : null,
    });
  }),
];

resetT7Settings();
resetT7Users();
handlers.push(...settingsHandlers, ...sutsHandlers);
