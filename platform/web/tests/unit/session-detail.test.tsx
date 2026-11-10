// T4 会话详情（计划 Task 4 断言）：Steps 高亮 / 审批横幅 / 门② 提交 /
// 409 处理 / EffectTab option 含轮次数据 / 状态映射中文 label。
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';

// jsdom 无 canvas：mock echarts-for-react 捕获 option prop
const chartOptions: unknown[] = [];
vi.mock('echarts-for-react', () => ({
  default: (props: { option: unknown }) => {
    chartOptions.push(props.option);
    return <div data-testid="echart-mock" />;
  },
}));

import { SessionDetailPage } from '../../src/pages/sessions/detail/SessionDetailPage';
import { StatusBadge } from '../../src/components/StatusBadge';
import { useUiStore } from '../../src/stores/ui';
import {
  detailId,
  hypothesisRequests,
  setHypothesisShould409,
} from '../msw/handlers';

function renderApp(route = `/sessions/${detailId}`) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/sessions/:id" element={<SessionDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  hypothesisRequests.length = 0;
  chartOptions.length = 0;
  setHypothesisShould409(false);
});
test('状态映射：ITER_STATE_META 优先渲染中文 label', () => {
  render(
    <div>
      <StatusBadge value="attributing" />
      <StatusBadge value="paused" />
      <StatusBadge value="running" />
    </div>,
  );
  expect(screen.getByText('归因中')).toBeInTheDocument();
  expect(screen.getByText('已暂停待审批')).toBeInTheDocument();
  // 聚合值回落 STATUS_META 保持兼容
  expect(screen.getByText('运行中')).toBeInTheDocument();
});
test('PAUSED 详情页：横幅文案 + Steps 高亮"假设" + 门② 可操作/门①③④ 禁用', async () => {
  renderApp();
  // 顶部橙色横幅
  expect(
    await screen.findByTestId('awaiting-alert'),
  ).toHaveTextContent('等待您审批：假设提交');
  // 概要
  expect(screen.getByText(detailId)).toBeInTheDocument();
  expect(screen.getByText('已暂停待审批')).toBeInTheDocument();
  expect(screen.getByText('210,000 Token')).toBeInTheDocument();
  // Steps：当前 process 步为"假设"
  const steps = await screen.findByTestId('steps-panel');
  const current = steps.querySelector('.ant-steps-item-process');
  expect(current).not.toBeNull();
  expect(current!).toHaveTextContent('假设');
  // 四门卡片：仅门② 可操作（门① 已过、门③④ 未到）
  expect(screen.getByTestId('gate-confirm-direction').textContent).toContain(
    '已过',
  );
  expect(screen.getByTestId('gate-review').textContent).toContain('未到');
  expect(screen.getByTestId('gate-decision').textContent).toContain('未到');
  const g2 = screen.getByTestId('gate-hypothesis');
  expect(g2.textContent).toContain('待审批');
  // 门② 输入可用（提交按钮在无文本前 disabled，属表单校验而非门禁）
  expect(within(g2).getByRole('textbox')).toBeEnabled();
  const others = ['gate-confirm-direction', 'gate-review', 'gate-decision'];
  for (const t of others) {
    const card = screen.getByTestId(t);
    const input = card.querySelector('button, textarea, input');
    expect(input).not.toBeNull();
    expect(input!).toBeDisabled();
  }
}, 30000);
test('门② 提交假设：请求体含 hypothesis 文本，201 后显示 hypothesis_id', async () => {
  renderApp();
  const g2 = await screen.findByTestId('gate-hypothesis');
  const area = within(g2).getByRole('textbox');
  await userEvent.type(
    area,
    '为 BGP 排查 skill 增加先查邻居状态的步骤',
  );
  await userEvent.click(
    within(g2).getByRole('button', { name: /提交假设/ }),
  );
  await waitFor(() => expect(hypothesisRequests.length).toBe(1), { timeout: 15000 });
  expect(hypothesisRequests[0]).toMatchObject({
    hypothesis: '为 BGP 排查 skill 增加先查邻居状态的步骤',
  });
  // 成功 message 含 hypothesis_id
  await waitFor(() =>
    expect(screen.getByText(/hyp-1234abcd/)).toBeInTheDocument(),
  );
  // invalidate 后门状态被服务端纠正（fixture 仍 paused，卡片重新渲染）
  await waitFor(() =>
    expect(screen.getByTestId('gate-hypothesis')).toBeInTheDocument(),
  );
});
test('门② 409：错误提示 + 自动 invalidate 刷新会话', async () => {
  setHypothesisShould409(true);
  renderApp();
  const g2 = await screen.findByTestId('gate-hypothesis');
  await userEvent.type(
    within(g2).getByRole('textbox'),
    '一个注定 409 的假设',
  );
  await userEvent.click(
    within(g2).getByRole('button', { name: /提交假设/ }),
  );
  await waitFor(
    () =>
      expect(
        screen.getByText(/当前门状态已变化|current_step/),
      ).toBeInTheDocument(),
    { timeout: 15000 },
  );
  // invalidate：详情重新请求（横幅仍渲染 = 组件刷新）
  await waitFor(() =>
    expect(screen.getByTestId('awaiting-alert')).toBeInTheDocument(),
  );
});
test('进展 Tab 轮次时间线 + 效果 Tab 三图 option 含轮次数据', async () => {
  renderApp();
  // 轮次时间线
  expect(await screen.findByTestId('round-1')).toHaveTextContent('第 1 轮');
  expect(screen.getByTestId('round-1')).toHaveTextContent('1 处文件修改');
  expect(screen.getByTestId('round-2')).toHaveTextContent('第 2 轮');
  // 切到效果 Tab（Tabs 懒渲染：默认销毁隐藏面板）
  await userEvent.click(screen.getByText('效果'));
  const acc = await screen.findByTestId('chart-accuracy');
  expect(acc).toBeInTheDocument();
  expect(screen.getByTestId('chart-tokens')).toBeInTheDocument();
  expect(screen.getByTestId('chart-latency')).toBeInTheDocument();
  // mock 捕获的 option：accuracy 轴含两轮、数据 [0.5, 0.8]
  const accOption = chartOptions[chartOptions.length - 3] as {
    xAxis: { data: string[] };
    series: { name: string; data: number[] }[];
  };
  expect(accOption.xAxis.data).toEqual(['第 1 轮', '第 2 轮']);
  expect(accOption.series[0].data).toEqual([0.5, 0.8]);
  const latOption = chartOptions[chartOptions.length - 1] as {
    series: { data: number[]; markLine?: unknown }[];
  };
  expect(latOption.series[0].data).toEqual([1500, 900]);
  expect(latOption.series[0].markLine).toBeDefined();
});
