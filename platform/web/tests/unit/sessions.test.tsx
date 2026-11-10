// T3 会话列表 + 创建向导（计划 Task 3 三组断言）
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { SessionListPage } from '../../src/pages/sessions/SessionListPage';
import { CreateSessionWizard } from '../../src/pages/sessions/CreateSessionWizard';
import { useUiStore } from '../../src/stores/ui';
import {
  iterationCreateRequests,
  resetSessionFixtures,
} from '../msw/handlers';

function renderApp(route = '/sessions') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/sessions" element={<SessionListPage />} />
          <Route path="/sessions/new" element={<CreateSessionWizard />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetSessionFixtures();
  iterationCreateRequests.length = 0;
});

test('列表渲染：Steps 迷你条 + 待审批徽章 + 状态化操作', async () => {
  renderApp();
  await waitFor(() =>
    expect(screen.getByText('iter-aaaa1111')).toBeInTheDocument(),
  );
  // 全局待审批横幅
  expect(screen.getByText(/有 1 个会话等待审批/)).toBeInTheDocument();
  // 待审批徽章（列表行）
  expect(screen.getAllByText('待审批').length).toBeGreaterThanOrEqual(1);
  // Steps 迷你条（每行一个）
  expect(screen.getAllByTestId('step-dots').length).toBe(3);
  // running 行有 暂停/终止；paused 行有 恢复（横幅里也有 id，取表格行）
  const rowOf = (id: string) =>
    screen.getAllByText(id).find((e) => e.closest('tr') !== null)!.closest('tr')!;
  const runningRow = rowOf('iter-aaaa1111');
  expect(within(runningRow).getByRole('button', { name: /暂\s*停/ })).toBeInTheDocument();
  expect(within(runningRow).getByRole('button', { name: /终\s*止/ })).toBeInTheDocument();
  const pausedRow = rowOf('iter-bbbb2222');
  expect(within(pausedRow).getByRole('button', { name: /恢\s*复/ })).toBeInTheDocument();
  // 终态行 → 归档
  const doneRow = rowOf('iter-cccc3333');
  expect(within(doneRow).getByRole('button', { name: /归\s*档/ })).toBeInTheDocument();
}, 60000);

test('向导全流程：公式预览 + 权重和警告 + 提交请求体 + 列表刷新', async () => {
  renderApp('/sessions/new');
  // Step1：三个下拉（SUT/运行环境/评测集）
  await waitFor(() =>
    expect(screen.getByText('下一步')).toBeInTheDocument(),
  );
  await userEvent.click(screen.getByRole('combobox', { name: '网络Agent' }));
  await userEvent.click(await screen.findByText('ipops-agent-1'));
  await userEvent.click(screen.getByRole('combobox', { name: '运行环境' }));
  await userEvent.click(
    await screen.findByText('remote-1', {
      selector: '.ant-select-item-option-content',
    }),
  );
  await userEvent.click(screen.getByRole('combobox', { name: '评测集' }));
  await userEvent.click(await screen.findByText('netops-bgp-triage'));
  await userEvent.click(screen.getByText('下一步'));
  // Step2：默认 goals=[accuracy max 0.7]，添加 token_cost min 0.3
  const preview = screen.getByTestId('formula-preview');
  expect(preview.textContent).toContain('0.7');
  await userEvent.click(screen.getByRole('button', { name: /添加指标/ }));
  expect(screen.getByTestId('formula-preview').textContent).toContain('0.7');
  expect(screen.getByTestId('formula-preview').textContent).toContain('0.3');
  expect(screen.getByTestId('formula-preview').textContent).toContain('accuracy');
  expect(screen.getByTestId('formula-preview').textContent).toContain('token_cost');
  // 权重和 = 1.0：无警告
  expect(screen.queryByTestId('weight-sum-warning')).toBeNull();
  // 改第一个滑块 0.7 → 0.9（4×0.05）→ 权重和 1.20 警告出现
  const sliderEl = screen.getAllByRole('slider')[0];
  sliderEl.focus();
  const key = (k: string) =>
    fireEvent.keyDown(sliderEl, {
      key: k,
      keyCode: k === 'ArrowRight' ? 39 : 37,
    });
  for (let i = 0; i < 4; i++) key('ArrowRight');
  expect(screen.getByTestId('weight-sum-warning').textContent).toContain('1.20');
  // 恢复 0.7，进 Step3
  for (let i = 0; i < 4; i++) key('ArrowLeft');
  await userEvent.click(screen.getByText('下一步'));
  // 摘要含所选 SUT 名
  expect(screen.getByText('ipops-agent-1')).toBeInTheDocument();
  // 提交 → 201 → 跳列表并刷新出新会话
  await userEvent.click(screen.getByRole('button', { name: /提交创建/ }));
  await waitFor(() =>
    expect(screen.getByText('iter-dddd4444')).toBeInTheDocument(),
  );
  // 请求体形态对齐 iterations.py IterationCreate
  expect(iterationCreateRequests.length).toBe(1);
  const body = iterationCreateRequests[0] as Record<string, unknown>;
  expect(body.sut_id).toBe('sut-1');
  expect(body.evalset_id).toBe('eval-0001');
  expect(body.mode).toBe('p1');
  const profile = body.profile as {
    goals: { metric: string; direction: string; weight: number }[];
  };
  expect(profile.goals).toEqual([
    { metric: 'accuracy', direction: 'max', weight: 0.7 },
    { metric: 'token_cost', direction: 'min', weight: 0.3 },
  ]);
}, 60000);

test('提交 409（同 SUT 活跃）→ 表单 alert 显示后端 detail', async () => {
  renderApp('/sessions/new');
  await waitFor(() => expect(screen.getByText('下一步')).toBeInTheDocument());
  // sut-2 已有活跃会话（fixture iter-bbbb2222）
  await userEvent.click(screen.getByRole('combobox', { name: '网络Agent' }));
  await userEvent.click(await screen.findByText('ipops-agent-2'));
  await userEvent.click(screen.getByRole('combobox', { name: '运行环境' }));
  await userEvent.click(
    await screen.findByText('local', {
      selector: '.ant-select-item-option-content',
    }),
  );
  await userEvent.click(screen.getByRole('combobox', { name: '评测集' }));
  await userEvent.click(await screen.findByText('netops-bgp-triage'));
  await userEvent.click(screen.getByText('下一步'));
  await userEvent.click(screen.getByText('下一步'));
  await userEvent.click(screen.getByRole('button', { name: /提交创建/ }));
  await waitFor(() =>
    expect(screen.getByTestId('create-error')).toBeInTheDocument(),
  );
  expect(screen.getByTestId('create-error').textContent).toContain(
    "sut 'sut-2' already has an active iteration",
  );
}, 60000);
