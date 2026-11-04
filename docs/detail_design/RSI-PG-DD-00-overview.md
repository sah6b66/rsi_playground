# RSI-PG-DD-00 详细设计总览与规范

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-00 |
| 版本 | v1.0 |
| 日期 | 2026-11-03 |
| 上游 | 需求 RSI-PG-REQ-001 v0.25；设计 RSI-PG-DES-001 v2.12；接口 RSI-PG-DES-003 v2.4；配置 RSI-PG-DES-002 v1.3；测试 RSI-PG-DES-004 v2.2 |
| 定位 | 代码生成级详细设计索引：每模块一个文件（DD-01~14），本文档定义统一模板、全局约定与交叉引用矩阵 |

## 1. 文件清单与追溯矩阵

| 文件 | 模块 | 主设计章节 | 核心 FR/ADR | 关联 ADR |
|------|------|-----------|------------|----------|
| DD-01 | core（领域模型/枚举/命名/异常/DB） | 2.4/2.5、接口 §2 | FR 全部基础 | ADR-5 |
| DD-02 | llm（客户端/提示词/卡带/记账） | 3.12、接口 §13 | FR5.7、NFR2④ | ADR-1/7/10 |
| DD-03 | security（认证/RBAC/凭证/审计） | 3.11 | NFR1④/NFR8 | ADR-14/19 |
| DD-04 | sandbox（服务/SSH池/就绪/后端） | 3.4、接口 §4 | FR3/FR4、ADR-20 | ADR-6/12/13 |
| DD-05 | carrier（主机/服务/网络/overlay/适配器） | 3.2、接口 §3/§10 | FR1、ADR-22 | ADR-4/22 |
| DD-06 | instrumentor（五阶段/proxy/SDK/报告） | 3.3、接口 §8 | FR2.3/FR5.3 | ADR-4 |
| DD-07 | tracing（collector/seq/store/解析/脱敏） | 3.5、接口 §5 | FR2、ADR-22 | ADR-3/22 |
| DD-08 | judge（det/safety/llm/antihack/aggregate） | 3.6 | FR7、ADR-23 | ADR-10/23 |
| DD-09 | versioning（gitstore/worktree/hook/freeze） | 3.8、接口 §7 | FR8、FR7.6 | ADR-2/15 |
| DD-10 | decision（聚合/噪声/检验/选择器） | 3.9、接口 §12 | FR9 | ADR-17 |
| DD-11 | optimizer（归因/假设/批评/应用/SFT） | 3.7、接口 §12 | FR6 | ADR-16/21 |
| DD-12 | orchestrator（batch/iteration/budget/ckpt/bus） | 3.1、接口 §11 | FR5、ADR-21 | ADR-8/21 |
| DD-13 | api（路由/DI/SSE/WS） | 3.10、接口 §9 | FR10、I-9 | ADR-18 |
| DD-14 | web（页面/组件/hooks/状态） | 3.10.2/3 | FR10 | ADR-9/18 |

阅读顺序 = 依赖顺序 = 编号顺序（01→14）；DD-12/13 为组合根，最后写。

## 2. 统一模板（DD-01~14 各文件七段结构）

```
1 追溯与范围   —— 上游章节/FR/ADR/交付阶段；职责一句话；明确"不做什么"
2 文件与类清单 —— 目录树到文件级；每文件一句话职责；类/函数总表
3 数据结构     —— 完整字段签名（类型/约束/默认值），可直接翻译为代码
4 对外接口     —— 方法完整签名：参数/返回/抛出异常/语义/主要调用方
5 内部逻辑     —— 状态转换表、关键算法伪代码、时序（仅复杂逻辑）
6 依赖矩阵     —— imports 上限约定 / 注入服务 / 读写的 DB 表 / 发布订阅的事件
7 测试映射与实施注记 —— TU/IN → 被测点；参考实现借鉴点；网络核实结论；已知坑
```

## 3. 全局工程约定（所有 DD 文件共同遵守）

- **语言/运行时**：Python 3.12；`platform/pyproject.toml`（uv 管理，`uv sync --frozen`）；全模块 asyncio（ADR-8）；同步库（netmiko/GitPython/docker-py）经 `asyncio.to_thread` 包裹，每处标注。
- **模型层**：Pydantic v2（领域模型，`core/models.py`）+ SQLModel（ORM 表，`db/tables.py`）；二者同源对照（接口 §2.1↔§2.2）。
- **日志**：唯一入口 `rsi/log.py`（structlog），logger 命名 `rsi.<模块>.<子模块>`；关联 ID 经 contextvars 自动注入（NFR5）；禁止裸 print/logging。
- **命名**：运行态 ID `<前缀>-<YYYYMMDDHHmmss>-<8位随机>`（UTC），唯一生成入口 `core/naming.py`；静态对象命名见主设计 2.5。
- **时间**：库内与文件内统一 UTC ISO8601 毫秒字符串（`datetime.utcnow()` 序列化）；显示层本地化在前端。
- **错误**：统一异常体系 `core/errors.py`（§DD-01.3.4）；对外映射错误码表（接口 §9.0）；服务内不吞异常——要么处理要么上抛带上下文。
- **测试**：pytest + pytest-asyncio + respx；LLM 一律 cassette（ADR-7）；Fake 全家桶（测试文档 §2）；用例 ID 映射在各 DD 文件第 7 段。
- **配置**：`rsi/settings.py` 读环境变量 + settings.yaml（优先级 env>yaml>默认）；动态/静态分区见配置文档 §2 注记。

## 4. 参考实现借鉴总表（nl2cli_light，模式级）

| 借鉴模式 | 来源 | 去向 | 适配说明 |
|---|---|---|---|
| 长连接缓存 + 每设备锁 + keepalive（重试≤3 + 1800s 冷却） | DeviceManager | DD-04 sshpool | netmiko 同步 → `asyncio.to_thread` 包裹；asyncio.Lock 而非 threading.Lock |
| 逐行锚定错误模式判定命令成败 | core/command_error.py | DD-04 命令结果判定 | 错误模式表进设计（Error:/Invalid input/% 等） |
| 视图归位（多命令后回用户视图） | navigate_to_user_view | DD-04 prepare/restore_task 收尾 | |
| VendorProfile SSOT + 封闭枚举 | core/vendor_profile.py | DD-04 device_type 映射 | P0 仅 vrp/ios；netmiko_device_type 直查表 |
| 指数退避+抖动、provider 故障转移、usage 记录 | external_llm | DD-02 llm | 退避 `min(2^n,30s)+jitter`；故障转移按组件可配 |
| ~~自研 HTTP 客户端~~ / ~~正则三策略解析 JSON~~ | external_llm | **不采纳** | 官方 SDK + 结构化输出 + pydantic 校验；"平衡括号提取"仅作 salvage 兜底 |
| ~~netmiko monkey-patch~~ | device_manager | **不采纳** | 差异在 VendorProfile 表内消化 |
| ~~明文凭证配置~~ | device_config.json | **不采纳** | CredentialStore（Fernet）已定 |

## 5. 网络核实结论登记（撰写期调研，持续追加）

| # | 主题 | 结论 | 影响文件 | 日期 |
|---|------|------|---------|------|
| R1 | OpenAI 结构化输出 | `client.responses.parse(pydantic_model)`（新）或 `beta.chat.completions.parse(response_format=Model)`（旧）；`response_format={"type":"json_schema", strict:True}` 为底层形式 | DD-02 | 2026-11-03 |
| R2 | Anthropic 结构化输出 | 原生 `output_config/json_schema`（2025-11 beta）对 Opus 4.6 偶发空响应（anthropic-sdk#913）；**forced tool_choice**（tool_choice={"type":"tool"}）为千次零错的稳妥路径 | DD-02 采"per-provider 策略 + pydantic 校验兜底" | 2026-11-03 |
| R3 | Claude Code OTel | metrics+logs 协议、无 span/消息正文（主设计附 C 已全文落档） | DD-07 | 2026-11-03 |
| R4 | opencode-prompt-logger | V2 插件 `@opencode/plugin`，assembled/http 帧格式（附 C.3） | DD-05/07 | 2026-11-03 |
| R5 | netmiko 异步能力 | 4.x **无稳定原生 asyncio API**（AsyncConnHandler 实验性）——采用同步 netmiko + `asyncio.to_thread` + 每会话 asyncio.Lock（备选 scrapli 异步库不引入，保持 netmiko 已验证性） | DD-04 §5.1 | 2026-11-04 |

## 6. 跨文件一致性规则

1. 任何 DD 文件与上游五文档冲突时，**以上游为准并回改 DD**（上游变更走修订记录联动）。
2. 跨模块调用只允许经 DD 文件第 4 段登记的"对外接口"；禁止直接 import 他模块内部类（api 装配层除外）。
3. DB 表读写必须登记在第 6 段依赖矩阵（写入需额外注明理由，如 decision 回填 hypotheses.predicted_hit_rate）。
4. 每个文件头部标注对应交付切片（S1~S11，主设计 §8），实施计划按切片聚合 DD 内容。

> 维护：新增核实结论追加 §5；模块间接口变更需同步对方 DD 第 4/6 段与接口文档。
