# WEB 前端实施计划（DD-14 v2.0）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 RSI Playground 的 React SPA 前端——登录 + 10 个页面 + 全局 UX 规范 + 7 个后端配套端点，端到端打通 EE-007。

**Architecture:** 纯 SPA（Vite dev proxy → FastAPI；生产 build 由 FastAPI StaticFiles 挂载单进程交付）。全部数据经类型化 API 客户端（openapi-typescript 从后端 /openapi.json 生成）；SSE/WS 驱动 react-query 失效实现实时刷新；权限与后端 ROLE_PERMISSIONS 同构（菜单/路由/按钮三处消费）。

**Tech Stack:** React 18 + TypeScript + Vite 7；Ant Design 5；TanStack Query v5 + Zustand；ECharts（echarts-for-react）；Monaco（@monaco-editor/react）；@tanstack/virtual；openapi-typescript + openapi-fetch；vitest + Testing Library + MSW；Playwright。后端：FastAPI + docker-py（既有栈）。

**Spec:** `docs/detail_design/RSI-PG-DD-14-web.md`（v2.0，用户确认）——每任务实现者必读对应小节（§4 权限矩阵 / §5 页面 / §6 UX 规范 / §7 工程架构 / §8 端点）。

## Global Constraints

- 前端根目录 `platform/web/`；Node ≥20、npm；**不引入本清单外重依赖**（UI 一律 antd，不混用其他组件库）
- API 前缀 `/api/v1`；token 存 localStorage 键 `rsi_token`；401 → 清 token 跳 `/login`
- 界面中文；桌面优先（内容区适配 ≥1280px）
- 角色 5 种：`admin|agent_engineer|researcher|approver|service`（与 `platform/rsi/security/rbac.py:9` 完全一致）；前端权限表以后端为权威注释指向
- 状态色单源：`src/utils/constants.ts` 的 `STATUS_META`（运行=processing/待审批=warning/成功=success/失败=error/env_failed=purple/未知=default）
- 表单四件套：必选 `*`、placeholder=样例、extra=缺省值+范围、tooltip=说明+出处（antd Form item 四属性）
- 后端改动：新端点一律 `@audited` + `require_perms(...)` + 既有 errors 映射；**每任务后 `cd platform && uv run pytest -q` 全绿**（基线 1209 passed + 12 skipped）
- 前端每任务后 `cd platform/web && npm test -- --run`（vitest）全绿
- 分支 `feat/web-frontend`（自 main 切出）；每任务独立 commit；`platform/web/node_modules/`、`dist/` 入 .gitignore
- 危险操作（删除 SUT/评测集/镜像、终止会话）：Modal 输入对象名确认（公共组件 `DangerConfirm`）

---

### Task 1: 脚手架 + API 层 + 登录/布局/导航/权限骨架

**Files:**
- Create: `platform/web/package.json`、`vite.config.ts`、`tsconfig.json`、`index.html`、`src/main.tsx`、`src/App.tsx`、`src/api/client.ts`、`src/api/sse.ts`、`src/stores/ui.ts`、`src/utils/constants.ts`、`src/utils/permissions.ts`、`src/pages/login/LoginPage.tsx`、`src/components/Layout.tsx`、`src/components/AuthGuard.tsx`、`src/components/ErrorBoundary.tsx`、`src/components/StatusBadge.tsx`、`src/components/DangerConfirm.tsx`、`src/components/FieldWithHints.tsx`
- Create: `platform/tests/unit/test_web_serving.py`（FastAPI 静态挂载）
- Modify: `platform/rsi/api/app.py`（StaticFiles 挂载 dist，目录存在时）
- Modify: `platform/.gitignore`（web 构建产物）

**Interfaces（后续任务消费）:**
- `apiFetch<T>(method, path, body?) → Promise<T>`（client.ts，抛 `ApiError{status, message, detail}`）
- `useEventStream()`（全局挂载于 App；按 SSE_INVALIDATION 失效 react-query）
- `can(role, perm): boolean`、`MENU`（permissions.ts：菜单树含权限位）
- `STATUS_META: Record<string, {label, color, icon}>` + `<StatusBadge value=... />`
- query key 约定：`['suts']`、`['sessions']`、`['session', id]`、`['evalsets']`、`['labs']`、`['instances']`、`['dependencies']`、`['backends']`

- [ ] **Step 1: 脚手架与依赖**

`platform/web/package.json`（关键 scripts 与依赖，版本固定主版本）：

```json
{
  "name": "rsi-web", "private": true, "type": "module",
  "scripts": {
    "dev": "vite", "build": "tsc -b && vite build",
    "test": "vitest", "e2e": "playwright test",
    "gen:api": "openapi-typescript http://127.0.0.1:8000/api/v1/openapi.json -o src/api/schema.d.ts"
  },
  "dependencies": {
    "react": "^18.3.0", "react-dom": "^18.3.0", "react-router-dom": "^6.26.0",
    "antd": "^5.21.0", "@ant-design/icons": "^5.5.0",
    "@tanstack/react-query": "^5.59.0", "zustand": "^4.5.0",
    "echarts": "^5.5.0", "echarts-for-react": "^3.0.0",
    "@monaco-editor/react": "^4.6.0", "@tanstack/virtual": "^3.10.0",
    "openapi-fetch": "^0.12.0"
  },
  "devDependencies": {
    "typescript": "^5.6.0", "vite": "^5.4.0", "@vitejs/plugin-react": "^4.3.0",
    "vitest": "^2.1.0", "@testing-library/react": "^16.0.0", "@testing-library/jest-dom": "^6.5.0",
    "jsdom": "^25.0.0", "msw": "^2.4.0", "@playwright/test": "^1.47.0",
    "openapi-typescript": "^7.4.0"
  }
}
```

`vite.config.ts`：react 插件 + `server.proxy {'/api': 'http://127.0.0.1:8000'}` + `test: {environment: 'jsdom', setupFiles: './tests/setup.ts'}`。`tests/setup.ts` 引 jest-dom 并启动 MSW server（`tests/msw/handlers.ts` 本任务先给空数组 + 登录 handler）。`npm install`。

- [ ] **Step 2: 写失败测试（骨架三件）**

`platform/web/tests/unit/skeleton.test.tsx`：

```tsx
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StatusBadge } from '../../src/components/StatusBadge';
import { can } from '../../src/utils/permissions';

test('StatusBadge 渲染状态文案', () => {
  render(<StatusBadge value="running" />);
  expect(screen.getByText('运行中')).toBeInTheDocument();
});
test('权限矩阵：agent_engineer 可建会话，researcher 不可', () => {
  expect(can('agent_engineer', 'session:create')).toBe(true);
  expect(can('researcher', 'session:create')).toBe(false);
});
test('admin 可管理用户', () => {
  expect(can('admin', 'user:manage')).toBe(true);
  expect(can('agent_engineer', 'user:manage')).toBe(false);
});
```

`platform/tests/unit/test_web_serving.py`：`create_app()`（无 dist 时）/ 有 `web/dist/index.html` 时 `GET /` 返回 200（stub 目录 tmp）——先写，验证 fail。

- [ ] **Step 3: 实现 utils/permissions + constants + 组件 + 客户端**

`src/utils/permissions.ts`（与 rbac.py 同构，注释"以后端 ROLE_PERMISSIONS 为权威"）：

```ts
export type Role = 'admin' | 'agent_engineer' | 'researcher' | 'approver' | 'service';
export const PERMS: Record<Role, string[]> = {
  admin: ['view:read', 'session:create', 'session:control', 'gate:decide', 'evalset:manage',
          'sut:manage', 'image:manage', 'sandbox:manage', 'env:manage', 'settings:write',
          'cred:manage', 'user:manage'],
  agent_engineer: ['view:read', 'session:create', 'session:control', 'gate:decide',
                   'evalset:manage', 'sut:manage'],
  researcher: ['view:read'],
  approver: ['view:read', 'gate:decide'],
  service: ['view:read'],
};
export const can = (role: Role | undefined, perm: string) =>
  !!role && PERMS[role].includes(perm);
```

`src/utils/constants.ts`：`STATUS_META = { running: {label:'运行中', color:'processing'}, paused_awaiting: {label:'待审批', color:'warning'}, finalized/succeeded: {label:'成功', color:'success'}, failed: {label:'失败', color:'error'}, aborted: {label:'已终止', color:'default'}, env_failed: {label:'环境失败', color:'purple'}, created: {label:'已创建', color:'default'} }`（未知值回落 default+原值）。

`src/api/client.ts`：`apiFetch`（fetch 封装：Bearer token、JSON、401→`localStorage.removeItem('rsi_token'); location.href='/login'`、非 2xx 抛 `ApiError` 含 detail）。`src/api/sse.ts`：`useEventStream()`——EventSource(`/api/v1/streams/events?token=...`)；`SSE_INVALIDATION: Record<string,string[]>` 按 DD-14 §3；`system.alert` → antd `notification`（critical 不自动关）。

`Layout.tsx`：antd Layout 侧边栏（MENU 三分组：总览/RSI 优化/资源管理/系统，每项 `{key, label, icon, perm}` 按当前角色过滤）+ 顶栏（用户名/角色 Tag/退出）+ `<Outlet/>`。`AuthGuard.tsx`：无 token→`/login`；`<RequirePerm perm>` 包裹受限路由。`ErrorBoundary.tsx`（antd Result 500+重载）。`StatusBadge`/`DangerConfirm`（Modal Input 等于 name 才 onOk）/`FieldWithHints`（封装 Form.Item 四件套：props `{label, required, example, extra, tooltip}`）。`LoginPage.tsx`：用户名+密码表单（placeholder `admin` / 密码提示见部署文档）→ `POST /auth/login` → 存 token → `navigate('/')`；401 行内错误。

`src/App.tsx`：QueryClientProvider + Router（路由表按 DD-14 §4，全部页面先放 `<div>施工中</div>` 占位组件统一从 `pages/placeholder.tsx` 导出，后续任务逐个替换）+ 全局 `useEventStream()`。`src/stores/ui.ts`：zustand `{user:{username,role}|null, setUser, sidebarCollapsed, toggle}`。

`app.py` 修改（静态挂载，放在所有 API 路由之后）：

```python
dist = Path(__file__).resolve().parents[2] / "web" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="web")
```

`.gitignore` 追加 `web/node_modules/`、`web/dist/`、`web/playwright-report/`。

- [ ] **Step 4: 跑测试全绿**

`cd platform/web && npm test -- --run`（3+ 用例过）；`cd platform && uv run pytest tests/unit/test_web_serving.py -q` 过；`uv run pytest -q` 全量 1209+12 不回归。

- [ ] **Step 5: Commit**

`git add platform/web platform/rsi/api/app.py platform/tests platform/.gitignore && git commit -m "feat(web): scaffold, typed client, SSE hook, auth-guarded layout with role-based nav (T1)"`

---

### Task 2: Dashboard 运行总览

**Files:**
- Create: `platform/web/src/pages/dashboard/DashboardPage.tsx`、`src/components/DependencyPanel.tsx`、`src/components/MetricCard.tsx`、`src/api/hooks.ts`（本任务起建：useSessions/useSuts/useLabs/useBackends/useInstances/useDependencies/useSystemHealth）
- Create: `platform/web/tests/unit/dashboard.test.tsx`、`tests/msw/handlers.ts` 扩展（各 GET handler 返回固定 fixture）

**Interfaces:**
- `src/api/hooks.ts` 导出：`useSessions(params?)`、`useSuts()`、`useLabs()`、`useBackends()`（GET /sandbox/backends）、`useInstances()`、`useDependencies()`（GET /system/dependencies）、`useSystemHealth()`——全部 react-query，staleTime 15s
- `MetricCard{title, value, sub?, status?}`；`DependencyPanel` 消费 `useDependencies`

- [ ] **Step 1: MSW fixtures + 失败测试**

handlers 给 `/api/v1/iterations`（3 条：running/paused_awaiting/finalized）、`/suts`（5）、`/labs`（2 一在线）、`/sandbox/backends`（3，ensp-pro-1 available:true、pnetlab false detail 'connect timeout'、fake true）、`/instances`、`/system/dependencies`（platform:ensp-pro-1 ok、platform:pnetlab down、db ok、llm ok latency 211）、`/system/health`。

`dashboard.test.tsx` 断言：渲染"运行中 3"（或按 fixture 计数）、"待审批 1"、"仿真沙箱 3（2 在线）"、依赖面板出现 "platform:pnetlab" 与离线原因文案、MetricCard 数量 ≥6。

- [ ] **Step 2: 实现 Dashboard**

布局按 DD-14 §5.2：`Row[gutter]` 六张 MetricCard → 左右两 Panel（DependencyPanel：List 每项 状态灯+名称+latency/detail；系统面板：health+预算进度条 `GET /iterations` 聚合 budget_used/tokens vs 上限——数据缺失显示"未配置"）→ 最近会话迷你 Table（5 行：状态 StatusBadge+待审批高亮+Link `/sessions/:id`）。所有计数前端聚合（useSessions 按 status 分组；沙箱在线=backends available 计数）。空态：每 Panel Empty+引导。

- [ ] **Step 3: 跑测试 + Commit**

`npm test -- --run` 绿 → `git add … && git commit -m "feat(web): dashboard overview — metric cards, dependency panel, system status (T2)"`

---

### Task 3: 会话列表 + 创建向导

**Files:**
- Create: `src/pages/sessions/SessionListPage.tsx`、`src/pages/sessions/CreateSessionWizard.tsx`、`src/components/WeightEditor.tsx`（DD-14 §5.5 并入）、`src/components/ProfileForm.tsx`
- Modify: `src/api/hooks.ts`（+useEvalsets/useEnvironments（GET /carrier/hosts，Task 6 前用 MSW stub）/useCreateSession）
- Test: `tests/unit/sessions.test.tsx` + handlers 扩展（iterations POST 201/409、evalsets、suts、carrier/hosts stub）

**Interfaces:**
- `ProfileForm{value: ProfileDraft, onChange}`；`ProfileDraft = {mode:'p0'|'p1', goals: {metric, direction:'max'|'min', weight:number}[], guards:{missed_check_worsen,safety_any_worsen,high_risk_actions}, budget_tokens, evalsets:{iterate,smoke,holdout}}`
- `WeightEditor{goals, onChange}`（产出公式预览串 `S = Σ wᵢ·(±Δmᵢ/σᵢ)`，权重和≠1 显示警告不阻止）

- [ ] **Step 1: 失败测试**

`sessions.test.tsx`：
1. 列表渲染 fixture 会话：状态列含 Steps 迷你条与"待审批"Badge；行操作按状态启用（running→暂停/终止；paused→恢复）
2. 向导流程：mock 三组下拉数据 → step1 全选 → step2 加一指标（准确率 max 0.7 + token min 0.3）→ 公式预览文本含 "0.7" 与 "0.3"；权重和 1.0 校验提示出现于改为 0.9 时 → step3 摘要含所选 SUT 名 → 提交 → MSW 断言请求体 `{sut_id, evalset_id, mode, profile:{goals:[...]}}` → 201 后列表刷新
3. 提交 409（同 SUT 活跃）→ 表单顶部 alert 显示后端 detail

- [ ] **Step 2: 实现**

SessionListPage：Table（列按 DD-14 §5.3）+ 顶部"创建会话"按钮（perm session:create）+ 全局待审批横幅（`useSessions` 存在 paused_awaiting 时）。向导 antd Steps 3 步：Step1 三个 Select（options 来自 useSuts/useEnvironments/useEvalsets，option sub 文案显示 image_ref/在线状态/任务数）；Step2 ProfileForm（WeightEditor：指标 Select 8 项+方向 Switch+Slider 0-1 步进 0.05+实时预览行；guards 三 Checkbox；budget InputNumber extra "缺省 1,000,000 token"；mode Radio——P2 禁用 label 加"未开放"；evalsets 三绑定 Select 可选）；Step3 Descriptions 摘要。提交 `POST /iterations`（profile 嵌入 params——按后端 iterations POST 实际字段名，实现时读 `platform/rsi/api/routers/iterations.py` 对齐）。操作按钮：pause/resume/abort（DangerConfirm）/archive 各调对应端点。

- [ ] **Step 3: 跑测试 + Commit**（`feat(web): session list + 3-step creation wizard with weight editor (T3)`）

---

### Task 4: 会话详情（上）：进展 + 门工作台 + 效果图 + 后端 timeline 端点

**Files:**
- Create: `src/pages/sessions/detail/SessionDetailPage.tsx`（Tab 容器，五 Tab 骨架，本任务实现前两 Tab）、`tabs/ProgressTab.tsx`、`tabs/EffectTab.tsx`、`src/components/GateWorkbench.tsx`（DD-14 §5.2 四门）、`src/components/MetricsCharts.tsx`
- Modify 后端：`platform/rsi/api/routers/iterations.py`（新增 `GET /iterations/{id}/timeline`）
- Test: `platform/web/tests/unit/session-detail.test.tsx`；`platform/tests/unit/test_web_timeline.py`

**Interfaces:**
- `GET /iterations/{id}/timeline` → `{iteration_id, rounds: [{round, status, batch_ids, config_version_before/after, files_changed: [{path, target}], tasks: [{task_id, batch_id, run_id, status, tokens, latency_ms, root_cause_match}], scores: {accuracy, noise_band: [lo,hi]}}], summary: {accuracy_series: [{round, value}], tokens_series: [...], latency_series: [...]}}`——聚合自 batches/runs/experiments/judgments（DB 读走既有 db_reads 模式；P0 内存 store 数据源，miss 查 DB）
- `MetricsCharts{series: {accuracy_series, tokens_series, latency_series, noise_band?}}`（三个 ECharts：折线/折线/箱线近似散点）

- [ ] **Step 1: 后端 timeline 失败测试（pytest）**

`test_web_timeline.py`：内存 store 造 1 迭代 + 2 轮（batches/runs/judgments fixture）→ GET timeline → rounds 数 2、tasks 计数正确、accuracy_series 长度 2；未知 id → 404。**先跑 fail。**

- [ ] **Step 2: 实现后端端点**

iterations.py 新增（@router.get("/{iteration_id}/timeline")，`get_current_user` + 组装：从 stores（batches/runs/judgments/experiments）读该迭代关联数据 → 按上述 shape 聚合；try/except 语义同既有端点；对齐既有代码风格）。

- [ ] **Step 3: 前端失败测试**

`session-detail.test.tsx`：fixture PAUSED 于 hypothesis 门 → ProgressTab 显示 Steps 高亮"假设提交"+顶部橙色横幅"等待您审批：假设提交"；门②提交表单 → POST `/iterations/:id/hypothesis` 201 `{hypothesis_id}` → 断言请求体含 hypothesis 文本；409（step 不符）→ 错误提示+自动 invalidate `['session', id]`。EffectTab：fixture 三序列 → 断言三个图表容器渲染、tooltip 内容含轮次。

- [ ] **Step 4: 实现前端**

ProgressTab：Steps（归因→假设→门审→应用→验证→决策，current 由 API current_step 映射）+ 轮次 Timeline + GateWorkbench（四门卡片，仅当前门可操作：门① 方向 Select 多选 cluster、门② hypothesis 文本域+`GET /suts/{id}/files` 侧栏 Monaco 只读原文（DD-14 §5.2 内嵌编辑视图——可编辑副本随提交）、门③ verdict Radio pass/reject+reasons、门④ action Radio promote/revert/advise_hold+reason；提交走四端点，成功后 invalidate）。EffectTab：`useSessionTimeline(id)` → MetricsCharts。

- [ ] **Step 5: 双侧测试 + Commit**（`feat(web+api): session detail progress/gate/effect tabs + timeline endpoint (T4a)`）

---

### Task 5: 会话详情（下）：修改对比 + 任务明细 + 轨迹/跨轮对比

**Files:**
- Create: `tabs/DiffTab.tsx`、`tabs/TasksTab.tsx`、`tabs/TraceTab.tsx`、`src/components/DiffViewer.tsx`（Monaco DiffEditor 封装）、`src/components/TraceTimeline.tsx`（虚拟滚动+JSON 树，DD-14 既有设计）
- Modify: `src/api/hooks.ts`（+useVersionDiff(sutId, from, to)、useRuns(batchId)、useTrace(runId)、useTaskTraces(roundA, roundB, taskId)）
- Test: `tests/unit/session-detail-2.test.tsx` + handlers（versions/diff、runs、traces fixtures）

**Interfaces:**
- `DiffViewer{oldPath, newPath, oldText, newText, language}`；`TraceTimeline{events: TraceEventDto[], height?}`；TraceTab 跨轮对比模式 props `{roundA, roundB, taskId}` 内部双栏

- [ ] **Step 1: 失败测试**

1. DiffTab：轮次选择器选 R1→R2 → 加载 `GET /suts/:id/versions/diff?from=..&to=..` → 文件列表 + 首文件 Monaco diff 容器 + 修改清单表（path/target/git ref 列）
2. TasksTab：fixture 轮×任务 → 徽章（成功/env_failed）、token/时延列；行点击切换到 TraceTab 并带 run_id
3. TraceTab：两轮同任务并排 → 双 TraceTimeline 各自事件数渲染；展开一条 tool_call 事件的 JSON 树

- [ ] **Step 2: 实现 + 测试 + Commit**（`feat(web): session detail diff/tasks/trace tabs with cross-round compare (T4b)`）

---

### Task 6: 资源页 + 后端配套端点（镜像/环境/沙箱/评测集克隆）

**Files:**
- Modify 后端：`platform/rsi/api/routers/`——新增 `images.py`（GET /docker/images、POST /docker/images:load、DELETE /docker/images/{ref}）、`hosts.py`（GET/PUT /carrier/hosts）、`evalsets.py`（+POST /{id}:clone）、`system.py` 或 `labs.py`（PUT /sandbox/backends/{key}——写 settings.yaml 沙箱段，模式同 carrier PUT）；`bootstrap.py`（HostPool/镜像操作所需的既有服务暴露——images 端点经 services 取 host_pool）
- Create 前端：`src/pages/evalsets/EvalsetListPage.tsx`（含导入向导迁移 DD-14 §5.7 + 克隆表单）、`src/pages/images/ImagesPage.tsx`、`src/pages/sandboxes/SandboxesPage.tsx`、`src/pages/environments/EnvironmentsPage.tsx`
- Test: `platform/tests/unit/test_web_resources_api.py`（后端 7 端点）；`platform/web/tests/unit/resources.test.tsx`

**Interfaces:**
- `GET /docker/images` → `{items: [{host, ref, size_mb, created_at, used_by: [sut_id]}], offline_hosts: [host]}`（docker-py images 经 to_thread；HostPool 离线主机列入 offline）
- `POST /docker/images:load` body `{host, tar_path}` → 202 `{status:'loading'}`（to_thread docker load；tar_path 必须存在于该主机——本地场景校验 Path 存在）
- `DELETE /docker/images/{ref}?host=` → 被 suts.image_ref 引用时 409
- `GET /carrier/hosts` → `{items:[{key, mode:'local'|'remote', endpoint, online, active_instances, max_instances, subnets}]}`；`PUT` body 同 items → 写 settings.yaml `carrier.hosts` 段 → `{restart_required: true}`
- `POST /evalsets/{id}:clone` body `{new_name, overrides: {device_endpoints?: {dev:{ip,port}}, config_apis?: [...], site_name?}}` → 201 新 evalset（env 基于源 env 应用覆盖生成；tasks/configs 复制引用）
- `PUT /sandbox/backends/{key}` body `{kind,url,verify_tls,subnets}` → 写 settings.yaml → `{restart_required:true}`
- 前端页面组件 props 无外部消费（页面自治），数据全走 hooks

- [ ] **Step 1: 后端失败测试（pytest）**

`test_web_resources_api.py` 逐端点：images 列表（stub host_pool 返回固定 images+一台 offline）；load（tmp tar 文件 + stub client 记录 load 调用；路径不存在 404）；delete 被引用 409/未引用 204（stub）；carrier hosts GET（fixture HostPool statuses）/PUT 写 settings.yaml 后文件内容断言 + restart_required；evalsets clone（源 evalset 内存 store fixture → clone 改 R1 ip:port → 新记录 env.ssh_access 更新、tasks 引用不变、名字冲突 409）；sandbox PUT 同 PUT 模式。**先 fail。**

- [ ] **Step 2: 实现后端**（读 `bootstrap.py` 的 host_pool/sandbox_service 获取方式，端点经 `request.app.state.services` 取；写 settings.yaml 复用一个 `_write_settings_section(path, section)` 辅助——放 `rsi/settings.py` 或 api/deps，yaml round-trip 保守策略：整段重写 carrier/sandbox 键）

- [ ] **Step 3: 前端失败测试 + 实现**

`resources.test.tsx`：①镜像页按主机分组表 + "路径导入"Modal（host Select+tar_path Input placeholder `/home/images/ipops-agent.tar` extra "宿主机上的 tar 文件路径"）→ POST 202 → 列表刷新；删除被引用按钮禁用 tooltip 列出引用 SUT ②环境页列表含 local/remote 徽章+在线灯；编辑远端表单（endpoint placeholder `ssh://user@192.168.1.50`、port extra "缺省 22"）→ PUT → 顶部 alert "已写入配置，重启平台后生效" ③沙箱页卡片+探活 detail+设备端点聚合面板（数据=useEvalsets 过滤 backend==key 的 ssh_access 聚合）+"测试连通"（对每端点 `POST /labs/{lab}/devices/{dev}/exec` 白名单命令 display clock？——连通测试用既有 readiness 语义：调 `GET /evalsets` 聚合仅展示，探测按钮 P0 显示端点并提示"S11 真实探测"——**按 DD-14 §5.8 落地：聚合展示+跳转凭证编辑；探测按钮仅对 fixture lab 可用**）④克隆向导：源→覆盖项表单（设备 ip/port 动态行）→差异预览高亮 → 提交 201 跳列表。

- [ ] **Step 4: 双侧测试 + Commit**（`feat(web+api): resource pages — images/hosts/sandboxes/evalset-clone + 7 companion endpoints (T6)`）

---

### Task 7: 参数六 Tab + SUT 管理页迁移

**Files:**
- Create: `src/pages/settings/SettingsPage.tsx`（六 Tab 容器）、`tabs/ParamsTab.tsx`、`tabs/LogsTab.tsx`、`tabs/AuditTab.tsx`、`tabs/CredsTab.tsx`、`tabs/SecurityTab.tsx`（DD-14 §5.6）、`tabs/UsersTab.tsx`
- Create: `src/pages/suts/SutListPage.tsx`、`SutDetailPage.tsx`、`FileEditorTab.tsx`（DD-14 既有 SutCreate §5.8 并入列表页创建 Modal）
- Test: `tests/unit/settings.test.tsx`、`tests/unit/suts.test.tsx` + handlers（settings/audit/credentials/users/files/diff）

**Interfaces:**
- `GET /system/settings`（既有）→ ParamsTab 树形分组：分组键映射表本地维护（按 settings.yaml.example 章节分组）；动态项判据=响应内 `dynamic: true`（无则按 DD-14 §2 动态清单常量表 `DYNAMIC_KEYS`）
- UsersTab：`GET/POST /users`、`PUT /users/{id}`（role 下拉 5 选/disabled Switch/重置密码 POST :reset-password——按 users.py 实际端点对齐）

- [ ] **Step 1: 失败测试**

①ParamsTab：fixture settings → 树渲染；动态项 InputNumber 可编辑、保存 → PUT `/system/settings/{path}` body 值 → toast "已热生效"；静态项只读锁标 tooltip "修改 settings.yaml 后重启"；列含缺省值/范围 ②UsersTab：列表+创建 Modal（用户名/角色 Select 含 5 角色/密码）→ POST；禁用 Switch → PUT ③SecurityTab 只读：白名单 8 项路径与冻结域 3 项渲染（fixture `/system/freeze`）④SUT 列表创建 Modal（image_ref 输入+验证按钮 → verify-image 结果清单渲染 ✓/✗）；FileEditor：fileTree（GET files）+ Monaco 编辑 → PUT commit=false → staging 提示；冻结路径 PUT commit=true → 409 → 行内错误（fixture）

- [ ] **Step 2: 实现 + 测试 + Commit**（`feat(web): settings six tabs + SUT management with file editor (T7)`）

---

### Task 8: Playwright e2e（EE-007）+ 生产挂载验证 + 文档收尾

**Files:**
- Create: `platform/web/playwright.config.ts`、`tests/e2e/golden-path.spec.ts`
- Modify: `platform/tests/e2e/test_ee_placeholder.py`（EE-007 由 skip 转条件跳过：`skipif` web/e2e 未构建或后端不可达——标注由 playwright 承接）
- Modify: `README.md`（web 快速上手：npm install/gen:api/dev/build + 访问方式）、`docs/detail_design/RSI-PG-DD-14-web.md`（版本行追加 v2.0 实施完成注记）
- Delete: `platform/web/src/pages/placeholder.tsx`（若仍有残留占位引用则同步清理）

**Interfaces:** 无（终端任务）

- [ ] **Step 1: e2e golden path 脚本**

`golden-path.spec.ts`（`webServer: uv run uvicorn rsi.api.app:create_app --factory --port 8000` 不起真进程——改用既有离线装配：启动脚本 `platform/web/tests/e2e/server.mjs` 以 `build_services()` 等价方式由 Python 侧 `uv run python -m uvicorn` 起离线 app（env 预置 admin/密码）；流程）：登录（admin）→ 断言 Dashboard 指标卡 → 创建会话向导三步（fixture 数据）→ 断言跳转详情 → 详情五 Tab 逐一渲染（PAUSED fixture 走门②提交）→ 效果 Tab 图表 canvas 存在 → 退出。`playwright.config.ts`：baseURL http://127.0.0.1:8000（生产挂载模式，`npm run build` 后由 FastAPI 服务）。

- [ ] **Step 2: 跑通 e2e**（`npm run build && npx playwright test`；CI/本机均离线可跑——后端 build_services 离线安全）；后端全量回归；**Step 3: 文档更新 + Commit**（`feat(web): playwright golden path e2e + static serving verified + docs (T8)`）

---

## Self-Review 记录

1. **Spec 覆盖**：U1→T2；U2→T3；U3→T6；U4→T6；U5→T6；U6→T6；U7→T4/T5；U8→T7；U9→T1（FieldWithHints/四件套）+T7 ParamsTab 列；B1→T1；B2→T1；B3→T7；B4→T1(sse)；B5→T1(DangerConfirm)+各页；B6/B7→T1 骨架/全局样式；§8 端点 7 个→T4(timeline)/T6(其余 6)；EE-007→T8。无缺口。
2. **占位符扫描**：页面占位组件仅 T1 过渡用途且 T8 清理；无 TBD/"适当处理"类表述；每步含具体代码/断言。
3. **类型一致性**：`apiFetch`/`can`/`STATUS_META`/hooks 命名在 T1 定义后各任务一致引用；后端 shape（timeline/images/hosts/clone）在 T4/T6 定义并有测试锚定。
