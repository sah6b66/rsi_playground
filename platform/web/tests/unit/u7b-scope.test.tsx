// U7b per-agent 优化范围：创建 Modal 自动/明确定义两态提交 body；
// 详情「优化范围」Tab 保存 PUT body + 越域项 422 行内错误
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { SutListPage } from '../../src/pages/suts/SutListPage';
import { SutDetailPage } from '../../src/pages/suts/SutDetailPage';
import { useUiStore } from '../../src/stores/ui';
import { resetT7Settings, t7Requests, t7SutId } from '../msw/handlers';

function renderApp(route: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/suts" element={<SutListPage />} />
          <Route path="/suts/:id" element={<SutDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetT7Settings();
  Object.values(t7Requests).forEach((arr) => {
    arr.length = 0;
  });
});

// ① 创建 Modal：自动模式（默认）→ body 不带 optimization_scope
test('创建 Modal：自动模式提交 body 无 optimization_scope', async () => {
  renderApp('/suts');
  await screen.findByText('ipops-agent-1');
  await userEvent.click(screen.getByRole('button', { name: /创建 Agent/ }));
  await userEvent.type(screen.getByRole('textbox', { name: /名称/ }), 'agent-a');
  await userEvent.type(
    screen.getByRole('textbox', { name: /镜像引用/ }),
    'agent-a:v1',
  );
  await userEvent.click(screen.getByRole('button', { name: /^创\s*建$/ }));
  await waitFor(() => expect(t7Requests.sutCreate.length).toBe(1));
  expect(t7Requests.sutCreate[0]).not.toHaveProperty('optimization_scope');
}, 20_000);

// ② 创建 Modal：明确定义（skills 域 + 细化 bgp-diag/，mcp_config 整域）
//    → optimization_scope = ['skills/bgp-diag/', 'mcp.json']
test('创建 Modal：明确定义组装 optimization_scope', async () => {
  renderApp('/suts');
  await screen.findByText('ipops-agent-1');
  await userEvent.click(screen.getByRole('button', { name: /创建 Agent/ }));
  await userEvent.type(screen.getByRole('textbox', { name: /名称/ }), 'agent-b');
  await userEvent.type(
    screen.getByRole('textbox', { name: /镜像引用/ }),
    'agent-b:v1',
  );
  // 切"明确定义"→ 出现 8 域 Checkbox 编辑器
  await userEvent.click(screen.getByRole('radio', { name: /明确定义/ }));
  expect(screen.getByTestId('scope-domain-skills')).toBeInTheDocument();
  // skills 域 + 细化 bgp-diag/（自动补 skills/ 前缀与 / 后缀）
  await userEvent.click(
    within(screen.getByTestId('scope-domain-skills')).getByRole('checkbox'),
  );
  const tagInput = screen
    .getByTestId('scope-patterns-skills')
    .querySelector('input')!;
  await userEvent.type(tagInput, 'bgp-diag');
  await userEvent.keyboard('{Enter}');
  // mcp_config 整域（不细化 → 域根 mcp.json）
  await userEvent.click(
    within(screen.getByTestId('scope-domain-mcp_config')).getByRole('checkbox'),
  );
  await userEvent.click(screen.getByRole('button', { name: /^创\s*建$/ }));
  await waitFor(() => expect(t7Requests.sutCreate.length).toBe(1));
  expect(t7Requests.sutCreate[0]).toMatchObject({
    name: 'agent-b',
    optimization_scope: ['skills/bgp-diag/', 'mcp.json'],
  });
}, 20_000);

// ③ 详情「优化范围」Tab：自动模式保存 → PUT body null（恢复自动）；
//    越域项（fixture 冻结 scripts/）→ 422 行内错误；合法项 → PUT 成功
test('详情优化范围 Tab：保存 PUT body + 422 行内错误', async () => {
  renderApp(`/suts/${t7SutId}`);
  await screen.findByText('配置文件');
  await userEvent.click(screen.getByRole('tab', { name: '优化范围' }));
  // 初始为自动模式；保存 → PUT null
  expect(screen.getByText(/当前模式：自动/)).toBeInTheDocument();
  await userEvent.click(screen.getByTestId('scope-save'));
  await waitFor(() => expect(t7Requests.sutScopePut.length).toBe(1));
  expect(t7Requests.sutScopePut[0]).toBeNull();
  // 切明确定义勾 scripts（fixture 视为越域）→ 422 行内错误
  await userEvent.click(
    screen.getByRole('radio', { name: /明确定义/ }),
  );
  await userEvent.click(
    within(screen.getByTestId('scope-domain-scripts')).getByRole('checkbox'),
  );
  await userEvent.click(screen.getByTestId('scope-save'));
  const err = await screen.findByTestId('scope-422-error');
  expect(err).toHaveTextContent(/超出全局优化白名单域/);
  expect(err).toHaveTextContent("scripts/");
  // 换勾 memory 域（合法）→ PUT body ['memory/']
  await userEvent.click(
    within(screen.getByTestId('scope-domain-scripts')).getByRole('checkbox'),
  );
  await userEvent.click(
    within(screen.getByTestId('scope-domain-memory')).getByRole('checkbox'),
  );
  await userEvent.click(screen.getByTestId('scope-save'));
  await waitFor(() =>
    expect(t7Requests.sutScopePut[t7Requests.sutScopePut.length - 1]).toEqual([
      'memory/',
    ]),
  );
  // 保存成功后错误清除
  await waitFor(() =>
    expect(screen.queryByTestId('scope-422-error')).not.toBeInTheDocument(),
  );
}, 20_000);
