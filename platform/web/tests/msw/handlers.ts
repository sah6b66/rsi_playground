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
  // 评测集列表（evalsets.py EvalsetPage shape）
  http.get('/api/v1/evalsets', () =>
    HttpResponse.json({
      items: [
        {
          id: 'eval-0001',
          name: 'netops-bgp-triage',
          purpose: 'iterate',
          tasks: [{ id: 't-001' }, { id: 't-002' }],
          description: '',
          frozen: true,
          version: 1,
          created_at: '2026-11-01T00:00:00Z',
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
        },
      ],
      next_cursor: null,
      has_more: false,
    }),
  ),
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
