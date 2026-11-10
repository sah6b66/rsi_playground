# RSI-PG-DD-14 web —— 前端

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-14 |
| 版本 | v2.0（用户 9 条需求 + 补充，brainstorming 逐节确认 2026-11-09；取代 v1.x 页面级设计，v1.x 既有组件设计——WeightEditor/SecurityTab/EvalsetImport/SutCreate/GateWorkbench——已并入本文）。v2.0 实施完成（T1-T8，feat/web-frontend） |
| 上游 | 设计 3.10.2/3.10.3、接口 §9；FR10、NFR8、ADR-9/18/25 |
| 交付切片 | S9（web 全量：10 页 + 登录 + 全局 UX 规范） |
| 关联决策 | RSI 会话=iterations 实体；沙箱页聚合展示设备端点（凭证编辑留评测集页）；镜像管理新增后端端点 |

## 1. 背景与目标

WEB 是用户唯一操作入口（体验优先）。后端 19 组 Router + SSE/WS + 定制 openapi.json 已完备，前端零实现。本设计覆盖全部页面的组件级细化、全局 UX 规范、工程架构、后端配套与测试策略。

**不做**：移动端适配、SSR/SEO、后端渲染（纯 SPA）；P2 审批页（approver 角色端点已留）。

## 2. 需求清单

用户 9 条 + 补充（B 列）：

| # | 需求 | 页面落点 |
|---|------|---------|
| U1 | Dashboard 总览（会话运行/暂停/完成数、镜像数、环境数含在线、沙箱数含在线、外部依赖在线状态、系统状态） | `/` |
| U2 | RSI 会话管理（创建：镜像+环境+评测集+目标/策略参数；启停/中断恢复；待审批提示） | `/sessions` |
| U3 | 评测集管理（导入/查询/删除/**复制修改成新集**——改设备 IP/API 配置等环境参数） | `/evalsets` |
| U4 | SUT Docker 镜像管理（创建/查询/删除；外部导入或指定路径） | `/images` |
| U5 | 仿真沙箱管理（创建/查询/删除；平台监控 URL；设备 SSH 端点聚合展示+探测） | `/sandboxes` |
| U6 | SUT 运行环境管理（创建/查询/删除；本地/远端区分，远端配 SSH） | `/environments` |
| U7 | 会话详情（进展+审批、优化效果图形、每轮文件修改/对比、每轮任务成功/失败/token/时延、轨迹查看与跨轮对比、修改清单含 git 路径） | `/sessions/:id` |
| U8 | 系统参数配置（settings.yaml 全量） | `/settings` |
| U9 | 全局输入提示规范（必选/可选、样例、缺省值、取值范围） | 全局规范 §6 |
| B1 | 登录页（用户名+密码，无角色选择——角色随 JWT） | `/login` |
| B2 | 权限差异化（5 角色：admin/agent_engineer/researcher/approver/service；菜单显隐+路由守卫+按钮级） | 全局 |
| B3 | 用户管理 Tab（admin：创建/角色分配/禁用/重置密码） | `/settings` 第 6 Tab |
| B4 | SSE 实时（system.alert→toast、状态变更→列表自动刷新） | 全局 |
| B5 | 危险操作二次确认（删除 SUT/评测集/镜像、终止会话——输入对象名确认） | 全局规范 |
| B6 | 加载/空态规范（骨架屏、空态引导按钮） | 全局规范 |
| B7 | 中文界面、桌面优先（≥1280px） | 全局 |

## 3. 技术选型

React 18 + TypeScript + Vite；**Ant Design 5**（Table/Form/Steps/Upload/Timeline/Descriptions 全覆盖）；TanStack Query v5（服务端状态）+ Zustand（UI 状态）；ECharts（效果图形）；Monaco Editor（配置树编辑/DiffViewer）；@tanstack/virtual（轨迹虚拟滚动）；openapi-typescript + openapi-fetch（从 `/openapi.json` 生成类型化客户端——契约零手工）；测试 vitest + Testing Library + MSW + Playwright。

生产部署：`vite build` 产物由 FastAPI StaticFiles 挂载（单进程，无 nginx）；dev 用 Vite proxy。

## 4. 信息架构与权限矩阵

```
/login 登录（无侧边栏）
├── 总览      /                       Dashboard
├── RSI 优化  /sessions               会话管理
│             /sessions/:id           会话详情
├── 资源管理  /evalsets               评测集
│             /suts  /suts/:id        SUT 管理（配置树编辑器，DD-14 既有）
│             /images                 Docker 镜像
│             /sandboxes              仿真沙箱
│             /environments           运行环境
├── 系统      /settings               参数/日志/审计/凭证/安全边界/用户 6 Tab
└── 嵌套路由  /runs/:id               轨迹查看（详情内嵌+独立）
```

权限矩阵（前端与后端 `ROLE_PERMISSIONS` 同构；`researcher`=只读，`service` 不登录 WEB）：

| 页面/操作 | admin | agent_engineer | researcher | approver |
|-----------|:--:|:--:|:--:|:--:|
| Dashboard/查询类全部 | ✓ | ✓ | ✓ | ✓ |
| 会话创建/启停/门审批 | ✓ | ✓ | — | 门审批（P2） |
| 评测集导入/克隆/删除 | ✓ | ✓ | — | — |
| SUT 配置树编辑 | ✓ | ✓ | — | — |
| 镜像/沙箱/环境管理 | ✓ | —（查看） | — | — |
| 参数热更/凭证/用户管理 | ✓ | — | — | — |

## 5. 页面细化

### 5.1 登录 `/login`
卡片居中：用户名+密码+登录；`POST /auth/login` → token 存 localStorage（`rsi_token`）；401 行内错误；登录后按角色跳 `/`。首次部署提示语（admin 初始账号由部署方配置——见 INSTALL）。

### 5.2 Dashboard `/`（U1）
- **指标卡片行**（SSE 实时）：会话（运行中/待审批/已完成本周）、SUT 镜像数、运行环境数（●在线/●离线）、仿真沙箱数（在线/总数）、SUT 实例运行数
- **外部依赖面板**（复用 DD-14 DependencyPanel）：LLM/数据库/Git/Docker（各主机）/`platform:<key>`（探活 detail）/Collector/Config API——逐项状态灯+延迟+失败原因
- **系统状态面板**：版本、运行时长、全局预算消耗（进度条+STOP 状态）、最近告警流（点击跳转）
- **最近会话迷你列表**：状态徽章+待审批高亮
- 数据：react-query 并行聚合 `/iterations` `/suts` `/labs` `/sandbox/backends` `/instances` `/system/dependencies` `/system/health`——**无新增端点**；SSE 失效映射（DD-14 既有 SSE_INVALIDATION 表）

### 5.3 会话管理 `/sessions`（U2）
- **列表**：ID/SUT/评测集/mode/状态（Steps 迷你条：归因→假设→门审→应用→验证→决策）/当前阶段/预算消耗/待审批 Badge/操作（启动·暂停·终止·恢复·归档——按状态启用）
- **创建向导**（Steps 3 步）：
  1. 基础：SUT 下拉（含 image_ref+验证状态徽章）→ 运行环境下拉（含在线状态）→ 评测集下拉（含 iterate/smoke/holdout 绑定概要）
  2. 目标与策略：ProfileForm（DD-14 WeightEditor 权重编辑器——方向切换+滑块+实时公式预览+总和校验；守卫三开关；预算上限；mode 选择 P0人工/P1半自动，P2 置灰标"未开放"）
  3. 确认：参数摘要卡 → `POST /iterations`；同 SUT 已有活跃会话 → 后端 409 → 表单行内提示
- 待审批提示：列表行橙色 Badge + 顶部全局横幅（SSE 驱动）
- "终端恢复会话"：中断（进程重启遗留 PAUSED/running 停滞）会话行显示"恢复"入口 → `:resume`

### 5.4 会话详情 `/sessions/:id`（U7，五 Tab）
- **进展**：状态机 Steps 大图（PAUSED 高亮当前门）+ 轮次时间线 + **GateWorkbench 四门**（方向确认/假设提交——内嵌 SUT 原文 Monaco 编辑视图/预审裁决/决策执行；PAUSED 顶部横幅"等待您审批：×"）；门操作经 `POST /iterations/{id}:confirm-direction|hypothesis|review|decision`（current_step 不符 409 → 自动刷新状态）
- **效果**（ECharts）：迭代集准确率按轮折线（噪声带区间背景）、token 消耗折线、时延箱线；悬浮 verdict 摘要
- **修改对比**：轮次选择器 + Monaco DiffViewer 逐文件 diff + 该轮修改清单表（文件路径/优化项类型/关联 git ref 只读——U7 的 skill/mcp/程序清单与库路径）；数据 `/suts/{id}/versions/diff`
- **任务明细**：每轮×每任务表（成功/失败/env_failed 徽章/token/时延/root_cause 命中）；行点击→轨迹
- **轨迹**：TraceTimeline（虚拟滚动+JSON 树展开，DD-14 既有）+ **跨轮对比**（选两轮同任务→双栏并排）
- 数据：`GET /iterations/{id}`（budget_used/current_step）、`GET /iterations/{id}/timeline`（**新增**，轮次聚合防 N+1）、`/batches?iteration_id=`、`/experiments`、`/runs`（DB 读已通）、`/traces`

### 5.5 评测集 `/evalsets`（U3）
列表（名称/厂商/任务数/设备数/冻结徽章/绑定用途）+ 导入（DD-14 拖拽向导保留：Dragger→解析预览卡→确认）+ 删除（确认）+ **克隆/修改**（新增）：选源集→表单只开放环境覆盖项（设备 ip/port、config_apis 端点、站点覆盖参数）→差异预览（变更项高亮）→另存新集。任务集只读引用（保证跨集可比性）。数据：既有 CRUD/`:import`/`:export` + **新增 `POST /evalsets/{id}:clone`**（body=覆盖项；后端基于 env.<site>.yaml 覆盖机制生成新 env）

### 5.6 SUT 管理 `/suts`（DD-14 既有保留）
列表/创建（G5 镜像接入：image_ref+验证镜像按钮）/详情（版本 lineage+diff+优化项勾选）/FileEditor（Monaco 配置树 CRUD——P0 人工模式核心；冻结路径保存 409 → 行内提示）

### 5.7 镜像 `/images`（U4）
按执行主机分组的镜像表（名称:tag/大小/创建时间/**被哪些 SUT 引用**）；操作：路径导入（输入宿主机 tar 路径→`POST /docker/images:load`→轮询结果）、删除（被引用禁用+提示）、创建 SUT 快捷入口（预填 image_ref）、验证镜像（既有 `/suts/{id}/verify-image`）
数据：**新增** `GET /docker/images`（按主机，docker-py images 列表+离线返回空标 offline）、`POST /docker/images:load`（body={host, tar_path}→to_thread docker load）、`DELETE /docker/images/{ref}?host=`（引用检查）

### 5.8 沙箱 `/sandboxes`（U5，ADR-25 形态）
- 平台卡片：kind（ensp-pro/pnetlab/fake 图标）/URL/探活状态灯（probe detail）/手动刷新（绕 TTL）/关联评测集数
- 新增/编辑平台：kind 下拉+url（placeholder `https://192.168.56.101:8443`）+verify_tls+subnets——**静态配置**：写 settings.yaml+提示"重启生效"（或纳入动态化评估，见 §8）
- **设备端点聚合面板**（每平台展开）：该平台被哪些评测集使用→各设备 ip:port→"测试连通"（逐设备 TCP 探测，显示可达/不可达+耗时）；凭证编辑跳转评测集页（用户确认的归属）
- Fake 后端只读展示（离线开发标识）
- 数据：`GET /sandbox/backends`（真实探活已通）+ 编辑经 settings 通道

### 5.9 运行环境 `/environments`（U6）
执行主机列表：本地（npipe）/远端（ssh://host:port）——在线状态（HostPool）/活跃实例数/上限/网段池；新增/编辑（远端：host/port/SSH 用户/凭证 ref——隧道参数）；连通性测试按钮
数据：**新增** `GET /carrier/hosts`（聚合 HostPool 状态）、`PUT /carrier/hosts`（写 settings.yaml carrier 段+返回"需重启生效"）——静态项语义，页面明示

### 5.10 参数配置 `/settings`（U8 + B3，6 Tab）
1. **参数**：按 settings.yaml.example 分组树（动态项行内编辑→`PUT /system/settings/{path}`→"已热生效"；静态项锁标+悬浮"改 settings.yaml 后重启"）；列：键/值/类型/**缺省值/取值范围/需求出处**（U9 落地；openapi example 为主前端补为辅）
2. 日志（按 run/experiment 检索）、3. 审计（admin，过滤 actor/action/时间）、4. 凭证（admin：创建/轮换——指纹显示无明文）、5. 安全边界（G3 只读：白名单 8 项+冻结域+ADR 依据）、6. **用户管理**（admin：列表/创建/角色下拉 5 选/禁用/重置密码——既有 users 端点）

## 6. 全局 UX 规范（U9 + B5/B6/B7）

- **表单项四件套**：label+必选 `*`；placeholder=样例值；extra=缺省值与范围（"缺省 600s，范围 30–86400"）；tooltip=说明+需求出处
- **状态色/文案常量**（utils/constants.ts 单源）：运行=processing 蓝/待审批=warning 橙/成功=success 绿/失败=error 红/env_failed=purple/未知=default 灰——全部枚举→{文案,颜色,图标}
- **危险操作**：红色按钮+Modal 输入对象名确认
- **反馈**：mutation 统一 loading/错误 toast（ApiError 显示后端 detail）；SSE critical 不自动关闭；乐观更新不用（简单优先）
- **加载/空态**：首屏骨架屏；空态插画+引导按钮（"尚无评测集→去导入"）
- **错误处理**：全局 ErrorBoundary；401→登录页；403→无权页；网络断→顶部重连横幅

## 7. 工程架构

```
platform/web/
├── package.json  vite.config.ts  tsconfig.json  playwright.config.ts
├── src/
│   ├── main.tsx  App.tsx（Layout+AuthGuard+ErrorBoundary）
│   ├── api/（openapi 生成 schema.d.ts + client.ts + hooks.ts + sse.ts/ws.ts）
│   ├── stores/ui.ts
│   ├── pages/{login,dashboard,sessions,sessions/detail,evalsets,suts,images,sandboxes,environments,settings}/
│   ├── components/{TopologyGraph,LineageDag,DiffViewer,TraceTimeline,MetricsCharts,LiveStatus,DependencyPanel,StatusBadge,FieldWithHints}/
│   └── utils/{format,constants,permissions}
└── tests/{unit（vitest+MSW）, e2e（playwright）}
```

- **API 层**：`npm run gen:api`（openapi-typescript 拉 `/openapi.json`）；client 统一 token/401/错误映射；SSE hook（query token）驱动 SSE_INVALIDATION；WS hook（append/ping 帧）
- **dev 模式**：Vite proxy→FastAPI；MSW 全端点 mock 供无后端开发/组件测试
- **permissions.ts**：与后端 ROLE_PERMISSIONS 同构的前端权限表（菜单/路由/按钮三处消费，注释注明以后端为权威）

## 8. 后端配套端点（新增 7 个 + 评估 1 项）

| 端点 | 用途 | 备注 |
|------|------|------|
| `GET /iterations/{id}/timeline` | 会话详情轮次聚合 | 防 N+1，S |
| `POST /evalsets/{id}:clone` | 评测集克隆（环境覆盖项） | 基于站点覆盖机制，M |
| `GET /docker/images` | 镜像列表（按主机） | docker-py to_thread，离线返空标 offline，S |
| `POST /docker/images:load` | 宿主机 tar 路径导入 | {host, tar_path}，S |
| `DELETE /docker/images/{ref}` | 删除（引用检查） | S |
| `GET /carrier/hosts` | 执行主机列表+状态 | M |
| `PUT /carrier/hosts` | 写 settings（静态，提示重启） | M |
| `PUT /sandbox/backends/{key}` | 沙箱平台编辑（或并入 settings 动态化） | 与上面 PUT 同模式，实施时定 |

全部端点走既有 @audited+require_perms 惯例；镜像/主机属 infra 权限（admin/operator 级——按 RBAC 现有权限粒度落位）。

## 9. 测试策略

- **组件/hook**：vitest+Testing Library——表单四件套渲染、创建会话向导三步校验、门工作台状态流转（409 重试）、权重编辑器公式预览、SSE→query 失效、权限矩阵渲染
- **API mock**：MSW（handlers 按端点生成；错误路径 409/403/503 覆盖）
- **e2e（EE-007）**：Playwright golden path：登录→创建会话向导→PAUSED 门审批→效果 Tab 图表渲染→任务明细→轨迹对比；CI 可离线跑（后端 build_services 离线装配 + Fake）
- **回归**：后端全量 pytest 不受影响（新增端点配 pytest 单测）

## 10. 交付任务序（单分支内）

1. 脚手架+API 生成+登录/布局/导航/权限骨架
2. Dashboard
3. 会话列表+创建向导
4. 会话详情五 Tab（最大单任务，可再拆）
5. 资源四页（评测集克隆/SUT 既有/镜像/沙箱/环境）+ 后端配套端点
6. 参数六 Tab
7. e2e + 文档（DD-14 更新为指向本 spec、README 快速上手）

---

## 附：自审记录（v2.0 self-review）

- 占位符扫描：无 TBD/TODO；§8 表末"实施时定"为明确的实施期决策点（沙箱编辑通道二选一），非含糊
- 一致性：角色名与后端 rbac.py 一致（5 角色，无 viewer/operator）；会话=iterations 全文一致；镜像/环境端点与页面描述一一对应
- 范围：单实施计划可承载（7 任务序）；后端配套已隔离为任务 5 内子项
- 歧义：克隆表单"环境覆盖项"已收敛为设备 ip/port+config_apis+站点参数三类
