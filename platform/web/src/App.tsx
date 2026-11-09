// 应用根：QueryClientProvider + 路由表（DD-14 §4）+ 全局 SSE 失效
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { AuthGuard, RequirePerm } from './components/AuthGuard';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './pages/login/LoginPage';
import { ComingSoon } from './pages/placeholder';
import { DashboardPage } from './pages/dashboard/DashboardPage';
import { useEventStream } from './api/sse';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 15_000 } },
});

function Shell() {
  useEventStream();
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AuthGuard />}>
        <Route element={<Layout />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/sessions" element={<ComingSoon title="会话管理" />} />
          <Route path="/sessions/:id" element={<ComingSoon title="会话详情" />} />
          <Route path="/evalsets" element={<ComingSoon title="评测集" />} />
          <Route path="/suts" element={<ComingSoon title="SUT 管理" />} />
          <Route path="/suts/:id" element={<ComingSoon title="SUT 详情" />} />
          <Route path="/images" element={<ComingSoon title="Docker 镜像" />} />
          <Route path="/sandboxes" element={<ComingSoon title="仿真沙箱" />} />
          <Route path="/environments" element={<ComingSoon title="运行环境" />} />
          <Route
            path="/settings"
            element={
              <RequirePerm perm="settings:write">
                <ComingSoon title="参数配置" />
              </RequirePerm>
            }
          />
          <Route path="/runs/:id" element={<ComingSoon title="轨迹查看" />} />
        </Route>
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Shell />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
