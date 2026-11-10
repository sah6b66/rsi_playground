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
  // 会话指标（fixture: running 1 / paused_awaiting 1 / finalized 1）——标题名词化（U1）
  expect(screen.getByText('运行中会话')).toBeInTheDocument();
  expect(screen.getByText('待审批会话')).toBeInTheDocument();
  expect(screen.getByText('已完成会话')).toBeInTheDocument();
  expect(screen.getAllByText('1').length).toBeGreaterThan(0);
  // 网络Agent 数 = 5
  expect(screen.getByText('网络Agent 数')).toBeInTheDocument();
  expect(screen.getByText('5')).toBeInTheDocument();
  // 运行环境（platform:* 检查项 1/2）
  expect(screen.getByText('运行环境（在线）')).toBeInTheDocument();
  expect(screen.getByText('1/2')).toBeInTheDocument();
  // 网络仿真平台 3（2 在线）
  expect(screen.getByText('网络仿真平台（在线）')).toBeInTheDocument();
  expect(screen.getByText('2/3')).toBeInTheDocument();
  // Agent 实例运行数 = 2
  expect(screen.getByText('Agent 实例（运行中）')).toBeInTheDocument();
  expect(screen.getAllByText('2').length).toBeGreaterThanOrEqual(1);
  // MetricCard 数量 = 8，且指标卡栅格统一（U1：全部 span=6）
  expect(screen.getAllByTestId('metric-card').length).toBe(8);
  expect(document.querySelectorAll('.ant-row .ant-col-6').length).toBeGreaterThanOrEqual(8);
});

test('依赖面板展示依赖项（中文名）与离线原因', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('网络仿真平台（pnetlab）')).toBeInTheDocument(),
  );
  expect(screen.getByText('网络仿真平台（ensp-pro-1）')).toBeInTheDocument();
  expect(screen.getByText('数据库')).toBeInTheDocument();
  expect(screen.getByText('LLM 服务')).toBeInTheDocument();
  expect(screen.getByText('外部依赖状态')).toBeInTheDocument();
  expect(screen.getByText('connect timeout')).toBeInTheDocument();
  expect(screen.getByText(/211/)).toBeInTheDocument();
});

test('最近会话迷你表：徽章+待审批高亮+行链接', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('iter-aaaa1111')).toBeInTheDocument(),
  );
  expect(screen.getAllByText('运行中会话').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('待审批会话').length).toBeGreaterThanOrEqual(1);
  expect(screen.getAllByText('成功')[0]).toBeInTheDocument();
  // 待审批行高亮 class
  expect(document.querySelectorAll('tr.row-awaiting').length).toBe(1);
  // 点击会话 ID 跳转详情（Link href）
  const link = screen.getByText('iter-bbbb2222').closest('a');
  expect(link).not.toBeNull();
  expect(link!.getAttribute('href')).toBe('/sessions/iter-bbbb2222');
});
