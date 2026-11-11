# RSI 双模式实施计划（CLI/API）— ADR-27

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RSI 会话支持 CLI 模式（现有）与 API 模式（新增——在线评估集+网络准备 API+无 DEV_* 注入），P0 交付。

**Architecture:** 会话创建选模式→CLI 走现有管道（离线评估集+SSH 环境准备）；API 走新管道（在线获取评估集+NetworkPrepClient 环境准备/恢复+SUT 用自身 skill/MCP 访问设备）。评估集双类型（CLI 类删除 config_apis / API 类在线获取）。两个系统级 API URL 进用户配置。

**Tech Stack:** Python 3.12 / FastAPI / httpx（NetworkPrepClient）；React 18 / antd 5（向导模式选择+在线预览）。

**Spec:** `docs/_rsi_playground_design.md` ADR-27 + §3.13；`docs/detail_design/RSI-PG-DD-04-sandbox.md` §5.8；`docs/detail_design/RSI-PG-DD-12-orchestrator.md` prep_mode；`docs/detail_design/RSI-PG-DD-14-web.md` §5.3/§5.5；`docs/_rsi_playground_test.md` TU-038/039 + IN-023。

## Global Constraints

- 后端基线：`uv run pytest -q` = 当前 main 全绿（合并前确认计数）；前端 `npm test -- --run` 全绿
- **config_apis / ConfigApi / call_api 全链路删除**——EnvSpec 模型/evalsets 端点/SandboxService.prepare/clone/前端展示
- NetworkPrepClient 不复用 ConfigApiCaller（独立新类，httpx 单客户端模式同 ConfigApiCaller 的容错风格）
- API 契约 v1（DD-04 §5.8 三端点 + DD-01 ApiEvalsetTask）——后续外部系统可调整
- 分支 `feat/dual-mode`，自 main 切出；每任务独立 commit
- settings.yaml.example 同步新增 evalset_api/network_prep_api 段

---

### Task D1: 核心模型变更 + settings + config_apis 全链路删除

**Files:**
- Modify: `platform/rsi/core/models.py`（删 ConfigApi/EnvSpec.config_apis/PrepareAction.call_api；增 NetworkEnvRef/ApiEvalsetTask/prep_mode 字段）
- Modify: `platform/rsi/settings.py`（增 EvalsetApiSettings/NetworkPrepApiSettings 段）
- Modify: `platform/rsi/api/routers/evalsets.py`（删 config_apis 相关——GET env_parsed/clone body/POST import 解析）
- Modify: `platform/rsi/sandbox/service.py`（删 call_api prepare 路径）
- Modify: `platform/rsi/sandbox/config_api.py`（标 deprecated docstring，不删文件）
- Modify: `platform/examples/settings.yaml.example`（增 evalset_api/network_prep_api 段）
- Test: `platform/tests/unit/test_d1_models.py`

**Interfaces（后续任务消费）:**
- `NetworkEnvRef(BaseModel)`: `{name: str, params: dict}`
- `ApiEvalsetTask(BaseModel)`: `{task_id: str, title: str, network_env: NetworkEnvRef, task: TaskSpec, expected: ExpectedAnswer}`
- `prep_mode: Literal["cli", "api"] = "cli"` — 添加到 BatchRunner 构造或 run_batch 签名（D4 消费）
- Settings 新段：`settings.evalset_api.url: str`、`settings.network_prep_api.url: str`、`settings.network_prep_api.timeout_s: int = 120`

- [ ] Step 1: 写失败测试 — models（NetworkEnvRef/ApiEvalsetTask 构造+校验）、Settings 新段存在、EnvSpec 无 config_apis 字段
- [ ] Step 2: 跑测试确认 fail
- [ ] Step 3: 实现 — models.py 删+增；settings.py 新段；evalsets.py 清理（grep config_apis 全部命中）；sandbox/service.py 删 call_api 路径；example 更新
- [ ] Step 4: 修复受影响的既有测试（grep config_apis/call_api in tests/ 逐个修正——删相关断言或 fixture 段）
- [ ] Step 5: `uv run pytest -q` 全绿 → commit `refactor(core): remove config_apis/call_api, add ApiEvalsetTask/NetworkEnvRef/evalset+netprep settings (D1)`

---

### Task D2: NetworkPrepClient（DD-04 §5.8）

**Files:**
- Create: `platform/rsi/sandbox/netprep.py`
- Test: `platform/tests/unit/test_netprep.py`（TU-038）

**Interfaces:**
- `NetworkPrepClient(base_url: str, timeout_s: int = 120)` — httpx.AsyncClient 单实例（懒创建+aclose 同 ConfigApiCaller 模式）
- `async def prepare(self, env_name: str, params: dict, session_id: str = "") -> PrepResult` — POST `{base}/environments/{env_name}/prepare` body `{params, session_id}` → `{status, env_id}`；异常包装 EnvError
- `async def restore(self, env_name: str, env_id: str, params: dict) -> RestoreResult` — POST `{base}/environments/{env_name}/restore` body `{env_id, params}`
- `async def status(self, env_name: str) -> bool` — GET `{base}/environments/{env_name}/status` → `{ready}`；探测失败不抛（返回 False）
- `PrepResult(BaseModel)`: `{env_id: str, status: str}`

- [ ] Step 1: 写失败测试（TU-038）— respx mock 三端点：prepare 200/500/超时；restore 200/非 200；status ready/not-ready/连接失败→False
- [ ] Step 2: fail → 实现 → pass
- [ ] Step 3: commit `feat(sandbox): NetworkPrepClient — prepare/restore/status with single-client pattern (D2/TU-038)`

---

### Task D3: 评估集在线获取端点 + ApiEvalsetParser

**Files:**
- Create: `platform/rsi/sandbox/evalset_fetcher.py`（在线 API 客户端 + 解析）
- Modify: `platform/rsi/api/routers/evalsets.py`（新增 `GET /evalsets/online/{name}` 端点）
- Test: `platform/tests/unit/test_evalset_fetcher.py`（TU-039）

**Interfaces:**
- `EvalsetFetcher(base_url: str, timeout_s: int = 30)` — `async def fetch(self, name: str) -> list[ApiEvalsetTask]`（GET `{base}/evalsets/{name}` → 解析 tasks JSON → ApiEvalsetTask 列表；无效响应抛 ValidationError）
- API 端点 `GET /evalsets/online/{name}` → `{name, task_count, tasks: [{task_id, title, network_env_name}], api_url}` — view:read；url 空→409 提示"评估集在线 API 未配置"

- [ ] Step 1: 失败测试（TU-039）— mock 在线 API 响应→fetch→ApiEvalsetTask 转换正确；坏 JSON→ValidationError；端点 200/409
- [ ] Step 2: fail → 实现 → pass
- [ ] Step 3: commit `feat(evalsets): online fetcher + GET /evalsets/online/{name} preview endpoint (D3/TU-039)`

---

### Task D4: BatchRunner prep_mode 路由（DD-12 §5.1/5.2）

**Files:**
- Modify: `platform/rsi/orchestrator/batch.py`（run_batch 增 prep_mode；run_task 内 prepare/restore 分支）
- Modify: `platform/rsi/orchestrator/resolver.py`（batch_resolver 输出增 evalset_type/prep_mode/api_evalset_name）
- Modify: `platform/rsi/api/routers/iterations.py`（POST body 增 prep_mode/api_evalset_name 可选字段）
- Modify: `platform/rsi/bootstrap.py`（组装 NetworkPrepClient/EvalsetFetcher 注入 BatchRunner）
- Test: `platform/tests/unit/test_d4_prep_mode.py`

**Interfaces:**
- `run_batch(..., prep_mode: Literal["cli","api"] = "cli")`
- CLI 路径（prep_mode="cli"）：完全不变——build_lab→sandbox.prepare→SUT(inject DEV_*)→sandbox.restore_task
- API 路径（prep_mode="api"）：**不调 build_lab/wait_ready/SandboxService**；每任务循环：netprep.prepare(task.network_env)→carrier.create_instance(不注入 DEV_*)→SUT→collect→judge→netprep.restore；失败降级 env_failed 同 CLI
- SUT 实例创建在 API 模式下仍走 HostService（容器编排不变），但 `cred=None`（不注入设备凭证）

- [ ] Step 1: 失败测试 — prep_mode="api" 走 netprep.prepare/restore（mock NetworkPrepClient）；不走 sandbox.build_lab/snapshot；不注入 DEV_*；CLI 模式回归不变
- [ ] Step 2: fail → 实现 → pass → 全量回归
- [ ] Step 3: commit `feat(orchestrator): prep_mode routing — API path via NetworkPrepClient, no DEV_* injection (D4)`

---

### Task D5: 健康监控 + Dashboard 新增依赖项

**Files:**
- Modify: `platform/rsi/orchestrator/health.py`（build_checkers 增 evalset_api/network_prep_api URL 探活——同 config_api checker 模式）
- Modify: `platform/web/src/utils/labels.ts`（DEP_LABELS 增 evalset_api→评估集在线 API / network_prep→网络环境准备API——**替换现有 config_api 条目**）
- Test: `platform/tests/unit/test_health_monitor.py`（扩展）

- [ ] Step 1: 失败测试 — 两 URL 配置时 checker ok / 未配置→not_configured / URL 空→down
- [ ] Step 2: fail → 实现（后端+前端 labels）→ pass
- [ ] Step 3: commit `feat(health): evalset_api + network_prep_api dependency checkers (D5)`

---

### Task D6: 前端——向导模式选择 + API 评估集输入 + Dashboard + 清理

**Files:**
- Modify: `platform/web/src/pages/sessions/CreateSessionWizard.tsx`（第 1 步首字段"工作模式"Radio：CLI 模式/API 模式；API 模式下评估集 Select 替换为 Input+预览按钮）
- Modify: `platform/web/src/pages/evalsets/EvalsetListPage.tsx`（删 config_apis 克隆覆盖项；删详情 config_apis 展示）
- Modify: `platform/web/src/pages/evalsets/EvalsetDetailPage.tsx`（删 config_apis 卡片）
- Modify: `platform/web/src/api/hooks.ts`（useOnlineEvalset(name)）
- Test: `platform/web/tests/unit/dual-mode.test.tsx`

- [ ] Step 1: 失败测试 — 向导 CLI/API Radio 切换→API 模式显示名称 Input+预览按钮→预览展示任务数/环境名；config_apis 在克隆表单/详情页不出现
- [ ] Step 2: fail → 实现 → pass → `npm test -- --run` 全绿
- [ ] Step 3: commit `feat(web): dual-mode wizard + online evalset preview + config_apis cleanup (D6)`

---

### Task D7: 集成测试（IN-023）+ 全量回归 + 合并

**Files:**
- Create: `platform/tests/integration/test_dual_mode.py`（IN-023）
- Test: 全量 `uv run pytest -q` + `npm test -- --run` + `npm run build`

**IN-023 场景：**
- stub EvalsetFetcher（返回 2 个 ApiEvalsetTask）+ stub NetworkPrepClient（prepare→env_id / restore→ok）
- build_services 组装 BatchRunner（注入 stub）
- run_batch(prep_mode="api") → 2 任务完成 → netprep.prepare 被调 2 次 / restore 被调 2 次 / sandbox.build_lab **零调用** / DEV_* 环境变量 **不出现** / judgments 落库有行
- CLI 模式回归（同 rig prep_mode="cli" 走现有管道正常）

- [ ] Step 1: 写集成测试 → pass
- [ ] Step 2: 全量双侧回归零失败
- [ ] Step 3: commit `test: IN-023 dual-mode end-to-end — API path via stub netprep/fetcher, CLI regression (D7)`
- [ ] Step 4: merge --no-ff + push

---

## Self-Review

1. **Spec coverage**: ADR-27 决策①双模式→D4/D6；②评估集双类型→D1/D3/D6；③网络准备双类型→D2/D4；④settings→D1；⑤Dashboard→D5；⑥SUT 不注入→D4；⑦config_apis 删除→D1/D6；TU-038→D2；TU-039→D3；IN-023→D7。✅ 无缺口。
2. **Placeholder scan**: 无 TBD；每任务含具体测试断言/文件路径/接口签名。
3. **Type consistency**: `prep_mode` 在 D1(模型)/D4(BatchRunner)/D6(前端)一致；`NetworkPrepClient.prepare(env_name, params, session_id)` 在 D2 定义/D4 消费一致；`ApiEvalsetTask` 在 D1 定义/D3/D7 消费一致。
