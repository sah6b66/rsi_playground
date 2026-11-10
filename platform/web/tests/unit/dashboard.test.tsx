import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { DashboardPage } from '../../src/pages/dashboard/DashboardPage';

function renderDashboard(route = '/') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 15_000 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test('仪表盘渲染指标卡片：会话/镜像/环境/沙箱/实例计数', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('iter-aaaa1111')).toBeInTheDocument(),
  );
  // 会话指标（fixture: running 1 / paused_awaiting 1 / finalized 1）
  expect(screen.getAllByText('运行中').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('1').length).toBeGreaterThan(0);
  expect(screen.getByText(/待审批 1/)).toBeInTheDocument();
  expect(screen.getByText(/已完成 1/)).toBeInTheDocument();
  // SUT 镜像数 = 5
  expect(screen.getByText('SUT 镜像')).toBeInTheDocument();
  expect(screen.getByText('5')).toBeInTheDocument();
  // 运行环境（platform:* 检查项 1/2）
  expect(screen.getByText('运行环境')).toBeInTheDocument();
  expect(screen.getByText('1/2')).toBeInTheDocument();
  expect(screen.getByText('执行主机')).toBeInTheDocument();
  // 仿真沙箱 3（2 在线）
  expect(screen.getByText('仿真沙箱')).toBeInTheDocument();
  expect(screen.getByText('共 3（2 在线）')).toBeInTheDocument();
  // SUT 实例运行数 = 2
  expect(screen.getByText(/SUT 实例/)).toBeInTheDocument();
  expect(screen.getAllByText('2').length).toBeGreaterThanOrEqual(1);
  // MetricCard 数量 ≥ 6
  expect(screen.getAllByTestId('metric-card').length).toBeGreaterThanOrEqual(6);
});

test('依赖面板展示依赖项与离线原因', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('platform:pnetlab')).toBeInTheDocument(),
  );
  expect(screen.getByText('platform:ensp-pro-1')).toBeInTheDocument();
  expect(screen.getByText('connect timeout')).toBeInTheDocument();
  expect(screen.getByText(/211/)).toBeInTheDocument();
});

test('最近会话迷你表：徽章+待审批高亮+行链接', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('iter-aaaa1111')).toBeInTheDocument(),
  );
  expect(screen.getAllByText('运行中').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('待审批').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('成功')[0]).toBeInTheDocument();
  // 待审批行高亮 class
  expect(document.querySelectorAll('tr.row-awaiting').length).toBe(1);
  // 点击会话 ID 跳转详情（Link href）
  const link = screen.getByText('iter-bbbb2222').closest('a');
  expect(link).not.toBeNull();
  expect(link!.getAttribute('href')).toBe('/sessions/iter-bbbb2222');
});
