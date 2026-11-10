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
  // 资源卡统一 X/Y 风格（第 5 轮反馈 1a）：
  // 网络Agent=运行实例 2 / 注册总数 5
  expect(screen.getAllByText('网络Agent').length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText('2/5')).toBeInTheDocument();
  expect(screen.getByText('运行实例 / 注册总数')).toBeInTheDocument();
  // 运行环境：GET /carrier/hosts（fixture 2 在线 / 共 2）
  expect(screen.getByText('运行环境')).toBeInTheDocument();
  expect(screen.getByText('2/2')).toBeInTheDocument();
  // 网络仿真平台：4 个平台（ensp-pro-1/pnetlab/fake/phys-1）3 在线
  expect(screen.getByText('网络仿真平台')).toBeInTheDocument();
  expect(screen.getByText('3/4')).toBeInTheDocument();
  // 指标卡 6 张两行（会话类/资源类分行，span=8 等宽）；无独立 Token 卡
  expect(screen.getAllByTestId('metric-card').length).toBe(6);
  expect(
    document.querySelectorAll('.ant-row .ant-col-8').length,
  ).toBeGreaterThanOrEqual(6);
  expect(screen.queryByText('Token 预算消耗')).not.toBeInTheDocument();
});

test('环境状态面板：保留 LLM/数据库等项，隐藏已由指标卡覆盖的项', async () => {
  renderDashboard();
  await waitFor(() =>
    expect(screen.getByText('数据库')).toBeInTheDocument(),
  );
  expect(screen.getByText('LLM 服务')).toBeInTheDocument();
  expect(screen.getByText('环境状态')).toBeInTheDocument(); // 标题改名（原「外部依赖状态」）
  expect(screen.queryByText('外部依赖状态')).not.toBeInTheDocument();
  // 隐藏集（第 5 轮反馈 1a）：docker/docker:*/lab/instances/platform:*
  expect(screen.queryByText('Docker 引擎')).not.toBeInTheDocument();
  expect(screen.queryByText('实验环境设备')).not.toBeInTheDocument();
  expect(screen.queryByText('Agent 实例')).not.toBeInTheDocument();
  expect(screen.queryByText('网络仿真平台（pnetlab）')).not.toBeInTheDocument();
  expect(screen.queryByText('网络仿真平台（ensp-pro-1）')).not.toBeInTheDocument();
  // 中文状态词 + 延迟展示（db/llm 在线）
  expect(screen.getAllByText('在线').length).toBeGreaterThanOrEqual(2);
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
