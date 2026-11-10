// e2e globalSetup：先构建生产产物（web/dist），供 FastAPI SPA 挂载。
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const webRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export default function globalSetup(): void {
  execSync('npm run build', { cwd: webRoot, stdio: 'inherit' });
}
