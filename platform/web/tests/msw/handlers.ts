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

export const dashboardHandlers = [
  http.get('/api/v1/iterations', () =>
    HttpResponse.json({
      items: [
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
      ],
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
export const handlers = [loginHandler, meHandler, ...dashboardHandlers];
