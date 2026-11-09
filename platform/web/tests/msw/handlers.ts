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
export const handlers = [
  loginHandler,
  meHandler,
  ...dashboardHandlers,
  ...sessionsHandlers,
];
