// 用户第 4 轮反馈（U4）：门卡等分布局 / 当前阶段中文 / 模式中文名 /
// 参数页类型中文与适用层级 / 向导裁判模型提交体。
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';

// jsdom 无 canvas：mock echarts-for-react（SessionDetailPage 引入图表）
vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echart-mock" />,
}));

import { SessionDetailPage } from '../../src/pages/sessions/detail/SessionDetailPage';
import { SessionListPage } from '../../src/pages/sessions/SessionListPage';
import { CreateSessionWizard } from '../../src/pages/sessions/CreateSessionWizard';
import { SettingsPage } from '../../src/pages/settings/SettingsPage';
import { modeLabel, stepLabel } from '../../src/utils/labels';
import { useUiStore } from '../../src/stores/ui';
import {
  detailId,
  iterationCreateRequests,
  resetSessionFixtures,
  resetT7Settings,
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
  resetT7Settings();
  iterationCreateRequests.length = 0;
});

// ── 1. 门卡等分：四门 Col span=6（等分一行） ──
test('GateWorkbench：四门 Row/Col 等分（ant-col-6 ×4）', async () => {
  renderWith(
    [{ path: '/sessions/:id', element: <SessionDetailPage /> }],
    `/sessions/${detailId}`,
  );
  const row = await screen.findByTestId('gate-row');
  const cols = row.querySelectorAll('.ant-col-6');
  expect(cols.length).toBe(4);
  // 每列 flex 拉伸 → Card 等高
  cols.forEach((c) => expect(c.getAttribute('style')).toContain('flex'));
});

// ── 2. 当前阶段中文：列表列 + 详情横幅一致 ──
test('当前阶段中文：awaiting_hypothesis → 等待假设提交', async () => {
  renderWith([{ path: '/sessions', element: <SessionListPage /> }], '/sessions');
  await screen.findAllByText('iter-bbbb2222');
  expect(screen.getByText('等待假设提交')).toBeInTheDocument();
  // 非门态映射（纯函数）
  expect(stepLabel('attributing')).toBe('归因中');
  expect(stepLabel('hypothesizing')).toBe('假设生成中');
  expect(stepLabel('applying')).toBe('应用变更中');
  expect(stepLabel('idle')).toBe('未开始');
});

// ── 3. modeLabel 三值 ──
test('modeLabel：p0/p1/p2 用户友好中文名', () => {
  expect(modeLabel('p0')).toBe('人工模式');
  expect(modeLabel('p1')).toBe('半自动模式');
  expect(modeLabel('p2')).toBe('全自动模式（未开放）');
});

// ── 4. 向导：模式 Radio 文案 + 裁判模型选择入 profile.judge_model ──
test('向导：模式 Radio 中文描述 + 裁判模型提交体', async () => {
  renderWith(
    [
      { path: '/sessions/new', element: <CreateSessionWizard /> },
      { path: '/sessions', element: <SessionListPage /> },
    ],
    '/sessions/new',
  );
  await waitFor(() => expect(screen.getByText('下一步')).toBeInTheDocument());
  await userEvent.click(screen.getByRole('combobox', { name: '网络Agent' }));
  await userEvent.click(await screen.findByText('ipops-agent-1'));
  await userEvent.click(screen.getByRole('combobox', { name: '运行环境' }));
  await userEvent.click(
    await screen.findByText('remote-1', { selector: '.ant-select-item-option-content' }),
  );
  await userEvent.click(screen.getByRole('combobox', { name: '评测集' }));
  await userEvent.click(await screen.findByText('netops-bgp-triage'));
  await userEvent.click(screen.getByText('下一步'));
  // 模式 Radio：中文名 + 一句描述；p2 禁用
  expect(screen.getByText(/人工模式/)).toBeInTheDocument();
  expect(screen.getByText(/半自动模式/)).toBeInTheDocument();
  expect(screen.getByText(/全自动模式（未开放）/)).toBeInTheDocument();
  expect(screen.getByText(/四步均由人工/)).toBeInTheDocument();
  // 裁判模型：fixture judge.llm.model=default → 选项 default
  await userEvent.click(screen.getByRole('combobox', { name: '裁判模型' }));
  await userEvent.click(
    await screen.findByText('default', { selector: '.ant-select-item-option-content' }),
  );
  await userEvent.click(screen.getByText('下一步'));
  await userEvent.click(screen.getByRole('button', { name: /提交创建/ }));
  await waitFor(() => expect(iterationCreateRequests.length).toBe(1));
  const body = iterationCreateRequests[0] as {
    mode: string;
    profile: { judge_model?: string };
  };
  expect(body.mode).toBe('p1');
  expect(body.profile.judge_model).toBe('default');
}, 60_000);

// ── 5. 参数页：值类型中文 + 适用层级 Tag ──
test('ParamsTab：类型中文 + judge 行 RSI 会话缺省 Tag', async () => {
  renderWith([{ path: '/settings', element: <SettingsPage /> }], '/settings');
  await screen.findByText('裁判模型');
  // 类型中文：数值（数字项）/ 字符串（judge.llm.model/default 项）
  expect(screen.getAllByText('数值').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('字符串').length).toBeGreaterThanOrEqual(1);
  // 适用层级：judge.llm.model → RSI 会话缺省
  expect(screen.getByTestId('scope-judge.llm.model')).toHaveTextContent('RSI 会话缺省');
  // system 行示例
  expect(screen.getByTestId('scope-sandbox.build_retry')).toHaveTextContent('系统级');
}, 20_000);
