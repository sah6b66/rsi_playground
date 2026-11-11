// ADR-27 D6：双模式向导 + 在线评估集预览 + config_apis 清理
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { CreateSessionWizard } from '../../src/pages/sessions/CreateSessionWizard';
import { EvalsetListPage } from '../../src/pages/evalsets/EvalsetListPage';
import { useUiStore } from '../../src/stores/ui';
import { DEP_LABELS } from '../../src/utils/labels';
import {
  iterationCreateRequests,
  resetEvalsetFixtures,
  resetSessionFixtures,
} from '../msw/handlers';

function renderApp(route = '/sessions/new') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/sessions/new" element={<CreateSessionWizard />} />
          <Route path="/evalsets" element={<EvalsetListPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetSessionFixtures();
  resetEvalsetFixtures();
  iterationCreateRequests.length = 0;
});

test('向导默认 CLI 模式；切 API 模式 → 评估集 Input+预览按钮 + 运行环境隐藏', async () => {
  renderApp();
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: /会话标识/ })).toBeInTheDocument(),
  );
  // 默认 CLI：离线评估集 Select + 运行环境 Select 存在
  expect(screen.getByRole('combobox', { name: '运行环境' })).toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: '评测集' })).toBeInTheDocument();
  expect(
    screen.queryByTestId('api-evalset-input'),
  ).not.toBeInTheDocument();

  // 切 API 模式
  await userEvent.click(screen.getByText('API 模式（在线评估集 + 网络准备 API）'));
  expect(
    screen.queryByRole('combobox', { name: '运行环境' }),
  ).not.toBeInTheDocument();
  expect(screen.getByTestId('api-evalset-input')).toBeInTheDocument();
  expect(screen.getByTestId('online-preview-btn')).toBeInTheDocument();

  // 切回 CLI：恢复离线 Select
  await userEvent.click(screen.getByText('CLI 模式（离线评估集 + 平台 SSH 环境准备）'));
  expect(screen.getByRole('combobox', { name: '运行环境' })).toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: '评测集' })).toBeInTheDocument();
});

test('API 模式在线预览：输入名称 → Alert 显示任务数', async () => {
  renderApp();
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: /会话标识/ })).toBeInTheDocument(),
  );
  await userEvent.click(screen.getByText('API 模式（在线评估集 + 网络准备 API）'));
  await userEvent.type(screen.getByTestId('api-evalset-input'), 'bgp-faults-v2');
  await userEvent.click(screen.getByTestId('online-preview-btn'));
  const alert = await screen.findByTestId('online-preview-result');
  expect(alert.textContent).toContain('任务数 2');
  expect(alert.textContent).toContain('BGP 邻居中断排障');
});

test('API 模式在线预览失败：行内错误', async () => {
  renderApp();
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: /会话标识/ })).toBeInTheDocument(),
  );
  await userEvent.click(screen.getByText('API 模式（在线评估集 + 网络准备 API）'));
  await userEvent.type(screen.getByTestId('api-evalset-input'), 'not-found-set');
  await userEvent.click(screen.getByTestId('online-preview-btn'));
  const err = await screen.findByTestId('online-preview-error');
  expect(err.textContent).toContain('在线预览失败');
});

test('提交 API 模式：body 含 prep_mode=api + api_evalset_name', async () => {
  renderApp();
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: /会话标识/ })).toBeInTheDocument(),
  );
  await userEvent.type(
    screen.getByRole('textbox', { name: /会话标识/ }),
    'API模式会话',
  );
  await userEvent.click(screen.getByRole('combobox', { name: '网络Agent' }));
  await userEvent.click(await screen.findByText('ipops-agent-1'));
  await userEvent.click(screen.getByText('API 模式（在线评估集 + 网络准备 API）'));
  await userEvent.type(screen.getByTestId('api-evalset-input'), 'bgp-faults-v2');
  await userEvent.click(screen.getByText('下一步'));
  await userEvent.click(screen.getByText('下一步'));
  await userEvent.click(screen.getByText('提交创建'));
  await waitFor(() => expect(iterationCreateRequests.length).toBe(1));
  const body = iterationCreateRequests[0] as Record<string, unknown>;
  expect(body['prep_mode']).toBe('api');
  expect(body['api_evalset_name']).toBe('bgp-faults-v2');
  expect(body['evalset_id']).toBeUndefined();
}, 60000);

test('克隆 Modal 无 config_apis 覆盖项', async () => {
  renderApp('/evalsets');
  const link = await screen.findByTestId('evalset-link-eval-0001');
  // 打开第一行克隆 Modal
  const row = link.closest('tr')!;
  await userEvent.click(
    row.querySelectorAll('a[role="button"]')[0]!, // 克隆
  );
  await waitFor(() =>
    expect(screen.getByText(/克隆评测集/)).toBeInTheDocument(),
  );
  expect(screen.queryByText(/网络环境准备API/)).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: /config_apis/ })).not.toBeInTheDocument();
});

test('DEP_LABELS：network_prep/evalset_api 存在、config_api 已移除', () => {
  expect(DEP_LABELS['network_prep']).toBe('网络环境准备API');
  expect(DEP_LABELS['evalset_api']).toBe('评估集在线 API');
  expect(DEP_LABELS['config_api']).toBeUndefined();
});
