// Playwright 配置（T8 / EE-007）：离线 FastAPI 后端（生产挂载模式）。
// globalSetup 先 npm run build（dist 由 create_app 挂载为 SPA），
// webServer 由 uv 拉起 tests/e2e/start_server.py（离线装配 + 预置数据）。
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8765',
    trace: 'retain-on-failure',
    locale: 'zh-CN',
  },
  globalSetup: './tests/e2e/global-setup.ts',
  webServer: {
    command: 'uv run python tests/e2e/start_server.py',
    url: 'http://127.0.0.1:8765/api/v1/health',
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
