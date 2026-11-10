import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './tests/setup.ts',
    globals: true,
    // e2e specs 走 playwright（见 playwright.config.ts），vitest 不收
    exclude: ['tests/e2e/**', 'node_modules/**'],
  },
});
