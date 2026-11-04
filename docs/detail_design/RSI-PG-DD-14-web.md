# RSI-PG-DD-14 web —— 前端

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-14 |
| 版本 | v1.1（G1-G5 WEB 缺口修正：权重编辑器/安全边界页/导入 UI/镜像接入） |
| 上游 | 设计 3.10.2/3.10.3、接口 §9；FR10、ADR-9/18 |
| 交付切片 | S9（api+web 最小版：login/dashboard/suts/evalsets/runs/labs） |

## 1. 追溯与范围

**职责**：React SPA——9 个页面路由 + 7 个定制组件 + react-query 服务端状态 + zustand UI 状态 + SSE/WS 实时订阅。全部操作经 Platform API（DD-13），无直连 DB/文件。

**不做什么**：不做后端渲染（纯 SPA）；不做移动端适配；不做 SSR/SEO。

## 2. 文件与类清单

```
web/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── src/
│   ├── main.tsx                 # 入口 + Router
│   ├── App.tsx                  # Layout + AuthGuard
│   ├── api/                     # openapi-typescript 自动生成
│   │   ├── schema.d.ts          # 类型定义（由后端 /openapi.json 生成）
│   │   ├── client.ts            # fetch 封装（baseURL/token/错误统一处理）
│   │   └── hooks.ts             # react-query hooks（useSuts/useBatches/useRuns…）
│   ├── stores/
│   │   └── ui.ts                # zustand（当前用户/主题/侧边栏折叠）
│   ├── pages/
│   │   ├── login/
│   │   ├── dashboard/
│   │   ├── suts/
│   │   │   ├── SutList.tsx
│   │   │   ├── SutDetail.tsx    # 配置版本+lineage+diff+优化项勾选
│   │   │   └── FileEditor.tsx   # Monaco 编辑器（配置树 CRUD，P0 人工模式核心）
│   │   ├── evalsets/
│   │   │   ├── EvalsetList.tsx
│   │   │   ├── EvalsetDetail.tsx  # env.yaml + tasks 表 + topology + 冻结版本列表
│   │   │   └── EvalsetImport.tsx # G4：导入（拖拽 .zip→解析预览→确认）
│   │   ├── optimization/
│   │   │   ├── ProfileForm.tsx  # 优化配置方案表单（含 G1 权重编辑器）
│   │   │   ├── WeightEditor.tsx # G1：指标权重编辑（方向切换+滑块+实时公式预览）
│   │   │   ├── IterationList.tsx
│   │   │   ├── GateWorkbench.tsx # 人工门四 tab（方向/假设/预审/决策）
│   │   │   └── ManualWizard.tsx  # P0 向导（两批+人工晋升）
│   │   ├── labs/
│   │   ├── runs/
│   │   │   ├── RunDetail.tsx
│   │   │   └── TraceTimeline.tsx
│   │   ├── compare/
│   │   ├── approvals/           # P2
│   │   └── settings/
│   │       ├── ParamsTab.tsx    # 平台参数（动态可编辑/静态只读）
│   │       ├── LogsTab.tsx      # 日志查看器（按 run/experiment 检索）
│   │       ├── AuditTab.tsx     # 审计日志
│   │       ├── CredsTab.tsx     # 凭证管理
│   │       └── SecurityTab.tsx  # G3：安全边界只读（白名单 8 项+冻结域+ADR 依据）
│   ├── suts/
│   │   ├── SutList.tsx
│   │   ├── SutDetail.tsx
│   │   ├── SutCreate.tsx        # G5：创建 SUT（镜像接入：image_ref 输入+本地镜像验证）
│   │   └── FileEditor.tsx
│   ├── components/
│   │   ├── TopologyGraph/       # G6
│   │   ├── LineageDag/          # G6 dagre
│   │   ├── DiffViewer/          # Monaco DiffEditor
│   │   ├── TraceTimeline/       # @tanstack/virtual 虚拟滚动 + JSON 树
│   │   ├── MetricsCharts/       # ECharts
│   │   ├── LiveStatus/          # SSE 订阅徽章
│   │   └── DependencyPanel/     # H1-H8 v1.2：全外部依赖状态面板（8 类检查器实时结果）
│   └── utils/
│       ├── format.ts            # 时间/字节/token 格式化
│       └── constants.ts         # 状态枚举→颜色/文案映射
└── tests/
    ├── unit/                    # vitest（组件/hooks）
    └── e2e/                     # playwright（EE-007）
```

## 3. 数据结构

```typescript
// react-query key 约定
const queryKeys = {
  suts: ['suts'],
  sut: (id: string) => ['suts', id],
  sutFiles: (id: string, version?: string) => ['suts', id, 'files', version],
  batches: ['batches'],
  batch: (id: string) => ['batches', id],
  run: (id: string) => ['runs', id],
  iteration: (id: string) => ['iterations', id],
  evalset: (id: string) => ['evalsets', id],
  experiments: (sutId: string) => ['experiments', sutId],
};

// SSE 事件→react-query 失效映射
const SSE_INVALIDATION: Record<string, string[]> = {
  'batch.status_changed': ['batches'],
  'run.status_changed':   ['batches', 'runs'],
  'lab.status_changed':   ['labs'],
  'iteration.status_changed': ['iterations'],
  'instance.status_changed': [],
};

// F4 v1.1：system.alert → toast + 可选 invalidate
function handleSSEMessage(msg: EventBusMessage) {
  if (msg.event === 'system.alert') {
    const { severity, message } = msg.data;
    if (severity === 'critical') notification.error({ message, duration: 0 }); // 不自动关闭
    else if (severity === 'warning') notification.warning({ message, duration: 8 });
    else notification.info({ message, duration: 5 });
    return;
  }
  const keys = SSE_INVALIDATION[msg.event];
  if (keys) keys.forEach(k => queryClient.invalidateQueries({ queryKey: [k] }));
}

// zustand UI store
interface UIStore {
  currentUser: User | null;
  theme: 'light' | 'dark';
  sidebarCollapsed: boolean;
}
```

## 4. 对外接口

### 4.1 API 客户端（api/client.ts）

```typescript
class ApiClient {
  private baseURL: string;
  private token: string | null;      // localStorage 'rsi_token'

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    // 统一：Authorization Bearer + Content-Type + 错误处理
    // 401 → 跳转 /login；429 → toast 预算超限；其他 → throw ApiError
  }
  setToken(t: string) { localStorage.setItem('rsi_token', t); }
}
export const api = new ApiClient();
```

### 4.2 SSE 订阅 Hook

```typescript
function useEventStream(onMessage: (msg: EventBusMessage) => void) {
  // EventSource(`/api/v1/streams/events`)，token 经 query 参数（EventSource 不支持 header）
  // 收到消息 → 根据 SSE_INVALIDATION 映射 invalidateQueries
  // 断线重连（EventSource 自动）
}
```

### 5.5 G1 权重编辑器（WeightEditor.tsx）

```
交互流程：
┌─────────────────────────────────────────────────────┐
│ 优化目标与权重                                       │
├─────────────────────────────────────────────────────┤
│ 指标          方向        权重                       │
│ ┌──────────┐ ┌────────┐ ┌──────────────────────┐   │
│ │准确率     │ │最大 ↑  │ │██████████░░░ 0.7     │   │
│ └──────────┘ └────────┘ └──────────────────────┘   │
│ ┌──────────┐ ┌────────┐ ┌──────────────────────┐   │
│ │token 消耗│ │最小 ↓  │ │████░░░░░░░░░ 0.3     │   │
│ └──────────┘ └────────┘ └──────────────────────┘   │
│ [+ 添加指标]  [− 移除]                              │
├─────────────────────────────────────────────────────┤
│ 实时预览：S = 0.7×(Δaccuracy/σ) + 0.3×(−Δtokens/σ) │
│ 权重总和：1.0 ✓                                     │
└─────────────────────────────────────────────────────┘

组件：
  - 指标下拉（8 种可选 MetricDef）
  - 方向 Switch（max↔min，图标 ↑↓）
  - 权重 Slider（0-1，步进 0.05，Ant Design Slider）
  - 实时公式预览（权重变更立即更新）
  - 权重总和校验（≠1.0 → 红色警告，不阻止保存——允许用户自定义相对权重）
```

### 5.6 G3 安全边界只读页（SecurityTab.tsx）

```
┌─────────────────────────────────────────────────────┐
│ 安全边界（只读——代码级常量，不可经 WEB 修改）         │
├─────────────────────────────────────────────────────┤
│ 🟢 可优化域（白名单 8 项）                           │
│ ┌─────────────────────────────────────────────────┐ │
│ │ system_prompt  suts/{id}/system-prompt(.md)?   │ │
│ │ skills         suts/{id}/skills/.*             │ │
│ │ scripts        suts/{id}/scripts/.*            │ │
│ │ mcp_config     suts/{id}/mcp.json              │ │
│ │ mcp_src        suts/{id}/mcp-src/.*            │ │
│ │ memory         suts/{id}/memory/.*             │ │
│ │ knowledge      suts/{id}/knowledge/.*          │ │
│ │ model_config   suts/{id}/model.json            │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ 🔒 冻结域（绝对禁止修改）                            │
│ ┌─────────────────────────────────────────────────┐ │
│ │ judge/.*     裁判提示词与安全规则                │ │
│ │ prompts/.*   平台 LLM 提示词                    │ │
│ │ evalsets/.*  评估集（含期望答案）                │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ 执行层保障：TargetRegistry + Critic + Git hook 三层  │
│ 详见 ADR-20 / FR6.4 / FR7.6                         │
└─────────────────────────────────────────────────────┘

数据源：GET /system/freeze（扩展返回 TargetRegistry 路径域清单）
无编辑功能——安全边界修改需改代码+发版（防止运行时开后门）
```

### 5.7 G4 评测集导入（EvalsetImport.tsx）

```
交互流程：
① 点击"导入评测集" → 弹出 Ant Design Upload.Dragger
② 拖拽或选择 .zip 文件 → 前端校验格式（zip 大小 ≤ 50MB）
③ 上传 → POST /evalsets:import (multipart/form-data)
④ 后端解析 → 返回导入预览：
   { name: "bgp-faults-holdout",
     env_summary: {backend: "ensp-pro", devices: 2, links: 1},
     task_count: 12,
     configs_count: 2,
     site_overrides: ["site-a"],
     warnings: ["task fault/bgp-03 缺少 critical_steps"] }
⑤ 前端展示预览卡片 → 用户确认名称/用途 → POST /evalsets（正式入库）
⑥ 失败场景：zip 解压失败/缺少 env.yaml/tasks.jsonl → 红色错误提示

组件：Upload.Dragger + PreviewCard + confirm Modal
```

### 5.8 G5 SUT Agent 镜像接入（SutCreate.tsx）

```
P0 方案（本地镜像）：
┌─────────────────────────────────────────────────────┐
│ 创建 SUT Agent                                       │
├─────────────────────────────────────────────────────┤
│ 名称：[ipops-agent          ]                       │
│ 载体：(●) OpenCode  ( ) Claude Code (P2)            │
│                                                     │
│ Docker 镜像：                                        │
│ image_ref: [ipops-agent:v1.2          ]             │
│                                                     │
│ ┌─ 镜像规范检查 ──────────────────────────────────┐ │
│ │ ✓ 载体 CLI 可执行（opencode --version）          │ │
│ │ ✓ /trace/ 目录存在                              │ │
│ │ ✓ prompt-logger 插件已预装                      │ │
│ │ ✗ /agent-config 挂载点约定：见手册 §3           │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ [验证镜像]  [创建]                                   │
└─────────────────────────────────────────────────────┘

"验证镜像"按钮：
  POST /suts/{id}/verify-image → 后端 docker inspect + docker run --rm <ref> --version
  → 返回检查清单（通过/失败逐项）

镜像准备流程（P0）：
  ① SUT 开发者本地 docker build -t ipops-agent:v1.2 .
  ② docker save ipops-agent:v1.2 > ipops-agent.tar
  ③ 拷贝到主节点 → docker load < ipops-agent.tar
  ④ WEB 创建 SUT → 填 image_ref="ipops-agent:v1.2"
  ⑤ 点击"验证镜像" → 通过后可用

P1 增强：私有 registry（Harbor/Nexus）
  → image_ref 改为 registry.example.com/ipops-agent:v1.2
  → docker pull 自动拉取；WEB 增加"从 registry 浏览"功能
```

### 5.1 路由守卫

```typescript
<Route element={<AuthGuard />}>     // 未登录→/login；已登录检查 role
  <Route path="/suts" element={<SutList />} />
  <Route path="/suts/:id" element={<SutDetail />} />
  ...
  <Route path="/approvals" element={<RequireRole role="approver"><Approvals /></RequireRole>} />
  <Route path="/settings" element={<RequireRole role="admin"><Settings /></RequireRole>} />
</Route>
```

### 5.2 人工门工作台（GateWorkbench，核心交互组件）

```
状态：iteration.state == 'PAUSED' && current_step == 'awaiting_*' → 显示对应 tab

Tab 1 方向确认（awaiting_direction）：
  展示 AttributionReport（簇列表，每簇含 failure_mode / A/B / 原文证据 / 历史尝试）
  → 用户勾选 1-N 个簇 → POST /iterations/{id}/confirm-direction

Tab 2 假设提交（awaiting_hypothesis）：
  展示 ProblemStatement 草稿 + 相关要素原文（Monaco 只读）+ 可改要素清单
  → 用户在 Hypothesis 表单编辑 diff_content / expected_improve / predicted_affected_tasks
  → POST /iterations/{id}/hypothesis

Tab 3 预审裁决（awaiting_review）：
  展示 DeterministicCritic 结果（通过/拒绝+原因）+ diff（Monaco DiffViewer）+ 假设摘要
  → 用户选择 pass/reject + 填写理由 → POST /iterations/{id}/review

Tab 4 决策执行（awaiting_decision）：
  展示 Selector 建议卡片（命中规则号+理由+指标 delta 表）+ 两批对比视图
  → 用户选择 promote/revert/advise_hold + 填写理由 → POST /iterations/{id}/decision
```

### 5.3 版本对比页（compare/:expId）

```
三段布局：
┌───────────────────────────────────┐
│ ① 改动内容（DiffViewer）           │  GET /experiments/{id}/compare → files_diff
│    Monaco DiffEditor 并排/行内     │
├───────────────────────────────────┤
│ ② 任务结果对照表                   │  → task_table [{task_id, base, cand, delta, change_flag}]
│    改善↑ 恶化↓ 持平→ 颜色标注     │
├───────────────────────────────────┤
│ ③ 轨迹并排（可展开）               │  → trace_diff_refs [{task_id, run_a, run_b}]
│    点击行 → 跳转 /runs/:id?compare │  → /traces/diff?run_a=&run_b=
└───────────────────────────────────┘
```

## 6. 依赖矩阵

- packages：react 18、react-router-dom 6、@tanstack/react-query 5、zustand 4、antd 5 + procomponents、@ant-design/charts 或 echarts-for-react、@antv/g6 5、monaco-editor、@tanstack/react-virtual、dayjs。
- API：全部经 `api/client.ts`（无直连后端以外的数据源）。
- 类型：`api/schema.d.ts` 由后端 `/openapi.json` 自动生成（DD-13 openapi.py）。

## 7. 测试映射与实施注记

**测试**：EE-007（playwright：登录→批次详情→拓扑/轨迹/diff 三组件渲染）；vitest 单测（hooks/格式化/路由守卫）。

**实施注记**：① openapi-typescript 每次 API 变更后重新生成（CI 步骤）；② react-query 的 `staleTime` 默认 30s（SSE 事件主动失效，轮询兜底 60s）；③ Monaco Editor 用 `@monaco-editor/react`（懒加载 worker）；④ G6 拓扑图节点类型映射：vrp→方形/ios→圆形，status→边框色（ready=绿/building=黄/error=红）；⑤ FileEditor 组件：路径树（Ant Tree）+ 编辑区（Monaco）+ 保存按钮（PUT /suts/{id}/files/{path}）——含"commit as new version"复选框（勾选→POST versions:snapshot 自动创建新版本）；⑥ 人工门工作台的 SSE 联动：iteration.status_changed 事件触发 react-query invalidate → 组件自动刷新显示新状态/下一个 tab；⑦ SSE token 经 query 参数传递（EventSource API 不支持自定义 header）——URL 形如 `/api/v1/streams/events?token=<jwt>`；⑧ P0 最小版页面：login/dashboard(简+依赖面板)/suts(列表+文件编辑)/evalsets/runs(批次+run)/optimization(向导+迭代列表+人工门)/labs/settings(凭证+参数+安全边界)——compare/approvals P1/P2 增量；⑨ DependencyPanel 每 30s 轮询 `GET /system/dependencies` + SSE system.alert 实时刷新。

### 5.9 H1-H8 依赖状态面板（DependencyPanel，v1.2）

```
┌─────────────────────────────────────────────────────────────┐
│ 外部依赖状态（30s 刷新，SSE system.alert 实时推送）           │
├──────────┬────────┬──────────┬──────────┬───────────────────┤
│ 依赖      │ 类型   │ 状态      │ 延迟     │ 详情              │
├──────────┼────────┼──────────┼──────────┼───────────────────┤
│ main     │ docker │ 🟢 up    │ 12ms     │ Docker Desktop    │
│ lab-xxx  │ lab    │ 🟢 up    │ 45ms     │ 2/3 devices online│
│ lab-yyy  │ lab    │ 🔴 down  │ —        │ 0/2 devices online│
│ sut-r1   │ sut    │ 🟢 up    │ —        │ running           │
│ sut-r2   │ sut    │ ⚪ exited│ —        │ exit_code=137     │
│ anthropic│ llm    │ 🟢 up    │ 230ms    │ 0% error rate     │
│ openai   │ llm    │ 🟡 degr  │ 5.2s     │ 30% error rate    │
│ git-repo │ git    │ 🟢 up    │ 5ms      │ disk 23%          │
│ sqlite   │ db     │ 🟢 up    │ 1ms      │ disk 45%          │
│ collector│ proc   │ 🟢 up    │ —        │ queue 12          │
│ cfg-api-1│ api    │ 🟢 up    │ 180ms    │ —                 │
└──────────┴────────┴──────────┴──────────┴───────────────────┘

数据源：GET /system/dependencies（30s 轮询 + SSE 事件主动失效）
位置：
  ① Dashboard 页顶部（全局状态一览）
  ② 全局 Header 右侧徽章（全部 up=🟢 / 任一 down=🔴 / 任一 degraded=🟡）
     → 点击徽章跳转 Dashboard 依赖面板
```
