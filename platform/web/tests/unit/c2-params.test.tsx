// C2：ParamsTab 增强——静态项可编辑保存 + 待重启 Badge + 页顶待重启汇总 +
// 重启平台按钮（DangerConfirm → POST /system:restart）+ LLM 提供商直配区。
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { SettingsPage } from '../../src/pages/settings/SettingsPage';
import { useUiStore } from '../../src/stores/ui';
import { resetT7Settings, resetT7Users, t7Requests } from '../msw/handlers';

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetT7Settings();
  resetT7Users();
  Object.values(t7Requests).forEach((arr) => {
    arr.length = 0;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

test('静态项可编辑：PUT 成功后出现「待重启」Badge 与页顶汇总 Alert', async () => {
  renderSettings();
  // 初始无待重启变更（Alert 隐藏）
  await screen.findByText('裁判模型');
  expect(screen.queryByTestId('pending-restart-alert')).not.toBeInTheDocument();
  // 编辑静态项 seed.master（数值）：20261102 → 42
  const num = screen.getByTestId('edit-seed.master');
  await userEvent.clear(num);
  await userEvent.type(num, '42');
  await userEvent.tab();
  await waitFor(() => expect(t7Requests.settingPut.length).toBe(1));
  expect(t7Requests.settingPut[0]).toEqual({
    path: 'seed.master',
    body: { value: 42 },
  });
  // 保存后该行显示「待重启」Badge
  await waitFor(() =>
    expect(screen.getByTestId('mut-seed.master').textContent).toContain('待重启'),
  );
  // 页顶汇总 Alert 出现，计数为 1
  const alert = await screen.findByTestId('pending-restart-alert');
  expect(alert.textContent).toContain('1');
}, 20_000);

test('重启平台：DangerConfirm 输入确认 → POST /system:restart → 5s 后刷新页面', async () => {
  const reload = vi.fn();
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload },
    writable: true,
  });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  renderSettings();
  // 先制造一个待重启变更让 Alert 出现
  await screen.findByText('裁判模型');
  const num = screen.getByTestId('edit-seed.master');
  await userEvent.clear(num);
  await userEvent.type(num, '42');
  await userEvent.tab();
  const alert = await screen.findByTestId('pending-restart-alert');
  expect(alert.textContent).toContain('待重启变更 1 项');
  // DangerConfirm：需输入「重启平台」才可确认
  await userEvent.click(screen.getByRole('button', { name: '重启平台' }));
  const confirmBtn = screen.getByRole('button', { name: '确认执行' });
  expect(confirmBtn).toBeDisabled();
  await userEvent.type(screen.getByTestId('danger-confirm-input'), '重启平台');
  expect(confirmBtn).not.toBeDisabled();
  await userEvent.click(confirmBtn);
  await waitFor(() => expect(t7Requests.restartPost.length).toBe(1));
  // 5s 后自动刷新
  expect(reload).not.toHaveBeenCalled();
  vi.advanceTimersByTime(5100);
  await waitFor(() => expect(reload).toHaveBeenCalled());
}, 30_000);

test('LLM 提供商直配区：半脱敏渲染 + 保存 PUT（新 key / 新 base_url）', async () => {
  renderSettings();
  // 模型服务分组表在最前，anthropic 行密钥占位为掩码
  const anthropicRow = await screen
    .findByTestId('llm-provider-anthropic')
    .then((el) => el.closest('tr') ?? el);
  expect(within(anthropicRow).getByTestId('llm-key-anthropic')).toHaveAttribute(
    'placeholder',
    '***xyz',
  );
  const compatRow = screen.getByTestId('llm-provider-openai_compat');
  expect(within(compatRow).getByDisplayValue('http://llm-gw:8000/v1')).toBeInTheDocument();
  // 编辑 openai_compat：输入新密钥 + 改 base_url → 保存
  await userEvent.type(
    within(compatRow).getByTestId('llm-key-openai_compat'),
    'sk-new-999',
  );
  await userEvent.clear(within(compatRow).getByTestId('llm-url-openai_compat'));
  await userEvent.type(
    within(compatRow).getByTestId('llm-url-openai_compat'),
    'http://gw2:9000/v1',
  );
  await userEvent.click(within(compatRow).getByRole('button', { name: /保\s*存/ }));
  await waitFor(() => expect(t7Requests.llmProviderPut.length).toBe(1));
  expect(t7Requests.llmProviderPut[0]).toEqual({
    name: 'openai_compat',
    body: { api_key: 'sk-new-999', base_url: 'http://gw2:9000/v1' },
  });
}, 20_000);
