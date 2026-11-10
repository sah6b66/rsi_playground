// U7a：系统菜单拆分（/logs 等五路由）+ 会话标识列 + 评测集详情页 + 载体只读。
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { LogsPage } from '../../src/pages/logs/LogsPage';
import { SessionListPage } from '../../src/pages/sessions/SessionListPage';
import { EvalsetDetailPage } from '../../src/pages/evalsets/EvalsetDetailPage';
import { SutListPage } from '../../src/pages/suts/SutListPage';
import { MENU, can } from '../../src/utils/permissions';
import { useUiStore } from '../../src/stores/ui';
import {
  resetEvalsetFixtures,
  resetSessionFixtures,
} from '../msw/handlers';

function renderWith(
  routes: { path: string; element: React.ReactElement }[],
  initial: string,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          {routes.map((r) => (
            <Route key={r.path} path={r.path} element={r.element} />
          ))}
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetSessionFixtures();
  resetEvalsetFixtures();
});

// 1. 菜单：系统组 5 个直接子项 + 路由权限
test('U7a 菜单拆分：系统组五项 /settings /logs /credentials /security /users', () => {
  const sys = MENU.find((g) => g.title === '系统')!;
  expect(sys.items.map((i) => i.key)).toEqual([
    '/settings',
    '/logs',
    '/credentials',
    '/security',
    '/users',
  ]);
  expect(sys.items.map((i) => i.label)).toEqual([
    '参数配置',
    '日志与审计',
    '凭证管理',
    '安全边界',
    '用户管理',
  ]);
  // 权限投影：researcher 仅见 /logs 与 /security（view:read）
  const researcherVisible = sys.items
    .filter((i) => can('researcher', i.perm ?? 'view:read'))
    .map((i) => i.key);
  expect(researcherVisible).toEqual(['/logs', '/security']);
});

// 2. /logs：双 Tab + 区别说明 + 审计数据
test('LogsPage：系统日志/操作审计双 Tab + 独立性说明', async () => {
  renderWith([{ path: '/logs', element: <LogsPage /> }], '/logs');
  await screen.findByText(/iteration started/);
  expect(
    screen.getByText(
      /系统日志用于排障；操作审计记录全部写操作用于追责，两者相互独立/,
    ),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: '操作审计' }));
  // 审计动作以中文渲染（sut.create → 创建 Agent）
  await screen.findByText('创建 Agent');
}, 20_000);

// 3. 会话列表：会话标识列（有 label 显示 label；空显示 ID 前 8 位）
test('SessionListPage：会话标识列（label / ID 前 8 位回退）', async () => {
  renderWith([{ path: '/sessions', element: <SessionListPage /> }], '/sessions');
  await screen.findAllByText('BGP排障优化-第一轮');
  // iter-bbbb2222 无 label → 回退前 8 位（iter-bbb）
  const row = screen
    .getAllByText('iter-bbb')
    .find((e) => e.closest('a') !== null)!;
  expect(row).toBeInTheDocument();
}, 20_000);

// 4. 评测集详情页：三卡片 + 凭证不出现
test('EvalsetDetailPage：组网环境/设备清单/任务列表，无用户名密码', async () => {
  renderWith(
    [
      { path: '/evalsets/:id', element: <EvalsetDetailPage /> },
      { path: '/evalsets', element: <div>list</div> },
    ],
    '/evalsets/eval-0001',
  );
  await screen.findByTestId('evalset-detail-name');
  expect(screen.getByText('netops-bgp-triage')).toBeInTheDocument();
  // 组网环境
  expect(screen.getByText('ensp-pro')).toBeInTheDocument();
  expect(screen.getByText('nms：http://10.0.0.9/api')).toBeInTheDocument();
  // 设备清单：名称/类型/版本/IP/端口
  expect(screen.getByText('R1')).toBeInTheDocument();
  expect(screen.getByText('8.180')).toBeInTheDocument();
  expect(screen.getByText('192.168.56.10')).toBeInTheDocument();
  expect(screen.getByText('22')).toBeInTheDocument();
  // 任务列表
  expect(screen.getByText('t-001')).toBeInTheDocument();
  // 凭证字段剥离（fixture env 含 username，但 env_parsed 不含）
  expect(screen.queryByText(/admin/)).not.toBeInTheDocument();
}, 20_000);

// 5. 载体只读：Tag 展示 + 无 Radio；提交值固定 opencode
test('SutListPage 创建 Modal：载体只读 Tag，无 Radio', async () => {
  renderWith([{ path: '/suts', element: <SutListPage /> }], '/suts');
  await screen.findByRole('button', { name: /创建 Agent/ });
  await userEvent.click(screen.getByRole('button', { name: /创建 Agent/ }));
  expect(screen.getByTestId('carrier-readonly')).toHaveTextContent('OpenCode');
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
}, 20_000);
