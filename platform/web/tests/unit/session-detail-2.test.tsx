// T5 会话详情（下）：修改对比 / 任务明细 / 轨迹+跨轮对比。
// Monaco（jsdom 无 worker）与 @tanstack/virtual（jsdom 无布局）均 mock，
// 以捕获的 props / 容器断言代替真实渲染。
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';

const diffProps: {
  original: string;
  modified: string;
  language?: string;
}[] = [];
vi.mock('@monaco-editor/react', () => ({
  DiffEditor: (props: {
    original: string;
    modified: string;
    language?: string;
  }) => {
    diffProps.push(props);
    return (
      <div
        data-testid="diff-editor-stub"
        data-original={props.original}
        data-modified={props.modified}
      />
    );
  },
}));

vi.mock('@tanstack/react-virtual', () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getTotalSize: () => count * 40,
    getVirtualItems: () =>
      Array.from({ length: count }, (_, i) => ({
        index: i,
        key: i,
        start: i * 40,
        size: 40,
      })),
    measureElement: () => {},
  }),
}));

import { SessionDetailPage } from '../../src/pages/sessions/detail/SessionDetailPage';
import { useUiStore } from '../../src/stores/ui';
import { detailId } from '../msw/handlers';

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
  diffProps.length = 0;
});

test('修改对比：轮次默认 R1，文件列表 + Monaco diff 内容 + 清单表 git ref 列', async () => {
  renderApp();
  await userEvent.click(await screen.findByRole('tab', { name: '修改对比' }));

  // 变更文件列表（versions/diff fixture：skill/bgp.md + mcp/tools.md）
  const fileList = await screen.findByTestId('diff-file-list');
  await waitFor(() =>
    expect(within(fileList).getByText('skill/bgp.md')).toBeInTheDocument(),
  );
  expect(within(fileList).getByText('mcp/tools.md')).toBeInTheDocument();
  // 文件行附优化项 target Tag（timeline files_changed）
  expect(within(fileList).getByText('skill')).toBeInTheDocument();

  // Monaco DiffEditor 收到 v1 旧文 / v2 新文（files 端点按 version 取内容）
  await waitFor(() => expect(diffProps.length).toBeGreaterThan(0), {
    timeout: 15000,
  });
  expect(diffProps[0].original).toContain('# BGP 排查');
  expect(diffProps[0].original).not.toContain('查看路由表');
  expect(diffProps[0].modified).toContain('查看路由表');
  expect(diffProps[0].language).toBe('markdown');

  // 修改清单表：path/target/git ref（ref=第 1 轮 config_version_after=2）
  const manifest = await screen.findByTestId('diff-manifest-table');
  expect(manifest.textContent).toContain('git ref');
  expect(manifest.textContent).toContain('v2');
  expect(manifest.textContent).toContain('skill/bgp.md');
}, 30000);

test('任务明细：轮次×任务表徽章/数字；行点击切到轨迹并加载该 run 事件', async () => {
  renderApp();
  await userEvent.click(await screen.findByRole('tab', { name: '任务明细' }));

  const table = await screen.findByTestId('tasks-table');
  await waitFor(() =>
    expect(table.querySelectorAll('tbody tr').length).toBe(4),
  );
  // 状态徽章：succeeded→成功（ITER/STATUS_META）；env_failed→环境失败 purple
  expect(within(table).getAllByText('成功').length).toBeGreaterThanOrEqual(3);
  const envTag = within(table).getByText('环境失败');
  expect(envTag.closest('.ant-tag')?.className).toContain('purple');
  // 数字列 + root_cause 命中/未命中
  expect(within(table).getByText('100')).toBeInTheDocument();
  expect(within(table).getByText('1,200')).toBeInTheDocument();
  expect(within(table).getAllByText('✓').length).toBeGreaterThanOrEqual(2);
  expect(within(table).getByText('✗')).toBeInTheDocument();

  // 行点击 run-1 → 轨迹 Tab + TraceTimeline 渲染 6 个事件
  const row = table.querySelectorAll('tbody tr')[0]!;
  expect(row.textContent).toContain('run-1');
  await userEvent.click(row);
  await waitFor(() => {
    const tab = screen.getByRole('tab', { name: '轨迹' });
    expect(tab.closest('.ant-tabs-tab')!.className).toContain(
      'ant-tabs-tab-active',
    );
  });
  const panel = await screen.findByTestId('trace-panel-run-1');
  await waitFor(() =>
    expect(within(panel).getByTestId('trace-row-6')).toBeInTheDocument(),
  );
  expect(within(panel).getAllByTestId(/^trace-row-/).length).toBe(6);
  // tool_call 摘要：data.cli 前 60 字
  expect(within(panel).getByText('show bgp summary')).toBeInTheDocument();
}, 30000);

test('跨轮对比：选同任务另一轮 run → 双栏各 6/4 事件；展开 tool_call JSON 树', async () => {
  renderApp();
  await userEvent.click(await screen.findByRole('tab', { name: '任务明细' }));
  const table = await screen.findByTestId('tasks-table');
  await waitFor(() =>
    expect(table.querySelectorAll('tbody tr').length).toBe(4),
  );
  await userEvent.click(table.querySelectorAll('tbody tr')[0]!);

  // 轨迹 Tab：选第 2 轮 run-4（同 task t-001）
  const combo = await screen.findByRole('combobox');
  await userEvent.click(combo);
  await userEvent.click(await screen.findByText('第 2 轮 · run-4'));

  const panelA = await screen.findByTestId('trace-panel-run-1');
  const panelB = await screen.findByTestId('trace-panel-run-4');
  expect(panelA.textContent).toContain('第 1 轮');
  expect(panelB.textContent).toContain('第 2 轮');
  await waitFor(() =>
    expect(within(panelA).getAllByTestId(/^trace-row-/).length).toBe(6),
  );
  await waitFor(() =>
    expect(within(panelB).getAllByTestId(/^trace-row-/).length).toBe(4),
  );

  // 展开 run-1 的 tool_call（seq 3）→ JSON 树渲染 data 键
  await userEvent.click(within(panelA).getByTestId('trace-toggle-3'));
  const json = await within(panelA).findByTestId('trace-json-3');
  expect(json.textContent).toContain('cli');
  expect(json.textContent).toContain('show bgp summary');
}, 30000);
