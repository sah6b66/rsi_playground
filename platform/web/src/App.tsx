// 应用根：QueryClientProvider + 路由表（DD-14 §4）+ 全局 SSE 失效
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { AuthGuard, RequirePerm } from './components/AuthGuard';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './pages/login/LoginPage';
import { ComingSoon } from './components/ComingSoon';
import { DashboardPage } from './pages/dashboard/DashboardPage';
import { SessionListPage } from './pages/sessions/SessionListPage';
import { CreateSessionWizard } from './pages/sessions/CreateSessionWizard';
import { SessionDetailPage } from './pages/sessions/detail/SessionDetailPage';
import { EvalsetListPage } from './pages/evalsets/EvalsetListPage';
import { ImagesPage } from './pages/images/ImagesPage';
import { SandboxesPage } from './pages/sandboxes/SandboxesPage';
import { EnvironmentsPage } from './pages/environments/EnvironmentsPage';
import { SutListPage } from './pages/suts/SutListPage';
import { SutDetailPage } from './pages/suts/SutDetailPage';
import { SettingsPage } from './pages/settings/SettingsPage';
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
          <Route path="/sessions" element={<SessionListPage />} />
          <Route path="/sessions/new" element={<CreateSessionWizard />} />
          <Route path="/sessions/:id" element={<SessionDetailPage />} />
          <Route path="/evalsets" element={<EvalsetListPage />} />
          <Route path="/suts" element={<SutListPage />} />
          <Route path="/suts/:id" element={<SutDetailPage />} />
          <Route path="/images" element={<ImagesPage />} />
          <Route path="/sandboxes" element={<SandboxesPage />} />
          <Route path="/environments" element={<EnvironmentsPage />} />
          <Route
            path="/settings"
            element={
              <RequirePerm perm="settings:write">
                <SettingsPage />
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
