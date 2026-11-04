# RSI-PG-DD-06 instrumentor —— 插桩流水线

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-06 |
| 版本 | v1.0 |
| 上游 | 设计 3.3、接口 §8；FR2.3/FR5.3、I-8；ADR-4 |
| 交付切片 | S8（scanner/planner/injector+mcp-proxy，P0 配置级） |

## 1. 追溯与范围

**职责**：五阶段流水线（扫描→计划→overlay 注入→冒烟验证→移除）+ 覆盖报告 + pg-mcp-proxy（stdio 代理）+ pg_trace SDK。P0 交付配置级注入（OpenCode 插件 hook + mcp.json 包装）；源码级/kb_proxy P1。

**不做什么**：不动原始配置版本（ADR-4，只在 overlay 上注入）；不采集轨迹（tracing 消费其产物文件）；LLM 通道不插桩（I-8）。

## 2. 文件与类清单

```
rsi/instrumentor/__init__.py
rsi/instrumentor/facade.py     # Instrumentor 门面（BatchRunner 注入依赖）
rsi/instrumentor/scanner.py    # Scanner
rsi/instrumentor/planner.py    # Planner
rsi/instrumentor/injector.py   # Injector（在 overlay 上执行 plan）
rsi/instrumentor/verifier.py   # Verifier（冒烟两跑比较）
rsi/instrumentor/remover.py    # Remover
rsi/instrumentor/report.py     # CoverageReport 生成/落库
rsi/instrumentor/mcp_proxy/    # pg-mcp-proxy 独立 entrypoint（node/python? ——§7⑤）
rsi/instrumentor/sdk/pg_trace.py
```

## 3. 数据结构

```python
Aspect = Literal["llm", "tool", "skill", "script", "knowledge"]
Method = Literal["carrier_native", "hook", "mcp_wrap", "kb_proxy", "source"]

class ScanReport(BaseModel):
    skills: list[SkillInfo] = []          # {path, entry_hint}
    mcp_servers: list[McpServerInfo] = [] # {name, cmd, transport}
    scripts: list[ScriptInfo] = []        # {path, lang, entry_hint}
    knowledge: KnowledgeInfo | None       # {retrieval_cfg}
    carrier_hooks_supported: bool         # OpenCode=True（@opencode/plugin V2）

class PlanItem(BaseModel):
    target: Aspect
    method: Method
    file: str                            # overlay 内相对路径
    snippet_spec: dict[str, Any] | None  # 源码级 P1
    rollback: str                        # "overlay" | "marker"

class InstrumentPlan(BaseModel):
    items: list[PlanItem]
    smoke_task_source: Literal["smoke_set", "first_task", "fixture"]

class CoverageReport(BaseModel):          # I-8 契约（接口 §8/设计 3.3）
    covered: list[CoveredItem]            # {aspect, method, files}
    not_instrumentable: list[NotInstrumentable]  # {aspect, reason}
```

## 4. 对外接口

```python
class Instrumentor:                       # 门面（BatchRunner 调用）
    def __init__(self, scanner, planner, injector, verifier, remover, report, settings): ...
    async def instrument(self, config_dir: Path, carrier: str,
                         smoke: EvalTask) -> tuple[Path, CoverageReport]
        """scan→plan→injector.apply(经 OverlayBuilder)→verifier.verify：
        通过→(overlay_dir, report)；失败→自动回滚（remover）→降级返回原 config_dir+标注报告"""

class Scanner:
    def scan(self, config_dir: Path, carrier: str) -> ScanReport
        # 固定路径扫描：skills/（SKILL.md）、mcp.json（servers[].command）、scripts/、
        # knowledge/（检索配置文件）、载体配置（opencode.json/plugins）

class Planner:
    def plan(self, scan: ScanReport, policy: InstPolicy) -> InstrumentPlan
        # 优先级 policy.prefer（hooks>mcp_wrap>kb_proxy>source）逐对象选型：
        # tool→(carrier_hooks_supported? hook : mcp_wrap)；skill/script→source(P1，P0 记 not_instrumentable)
        # knowledge→kb_proxy(P1)；llm→carrier_native 恒定（显式入报告）

class Verifier:
    async def verify(self, base_dir: Path, overlay_dir: Path, smoke: EvalTask,
                     carrier_adapter: CarrierAdapter) -> VerifyResult
        # 同一冒烟任务两跑：基线副本 vs 注入副本（FakeCarrier 或目标载体）
        # 判定 ① TaskResult 语义等价（task_complete 与 root_cause 相等）
        #      ② 注入侧事件产出（/trace/events.ndjson 中 tool_call/tool_result ≥1 条）

class Remover:
    async def remove(self, overlay_dir: Path) -> None    # marker 幂等移除；配置级=删 overlay
```

## 5. 内部逻辑

### 5.1 P0 注入项实现（OpenCode 载体）

```
① hook（tool 事件）：overlay 内新增 plugins/rsi-trace-hook.ts（@opencode/plugin V2）：
   tool.execute.before → 追加 {"event_type":"tool_call", tool, input, run_id(环境变量 RSI_RUN_ID), ts}
   tool.execute.after → 追加 tool_result（output/duration/ok）
   落点：/trace/events.ndjson（pg_trace 行格式 §5.2）；opencode.json plugins 数组注册本插件
② mcp_wrap（工具服务级）：mcp.json 中每个 server：
   "command": "pg-mcp-proxy", "args": ["--", <原 command>, *原 args],
   "env": {...原, "PG_PROXY_SINK": "/trace/events.ndjson"}
③ 知识检索（P1 kb_proxy）：knowledge/retrieval 配置端点改写为 localhost 代理
④ 源码级（P1）：SKILL.md/脚本插 PG-TRACE-BEGIN/END 标记块
```

### 5.2 pg_trace 行格式（SDK 与 hook/proxy 共用）

```json
{"run_id":"…","task_id":"…","event_type":"tool_call|tool_result|skill_loaded|script_exec|knowledge_query",
 "ts":"…","data":{…原样},"duration_ms":null}
```
（无 seq/ts 由 collector 补齐——文件仅是中转，DD-07 ingest 统一编号）

### 5.3 pg-mcp-proxy 行为契约（接口 §8）

```
stdin→子进程 stdin 透传（JSON-RPC 逐行/分帧不解析、不改动）
拦截 tools/call 响应后旁路追加写 PG_PROXY_SINK：
  {"event_type":"tool_call","data":{"name","arguments"}}（请求侧）
  {"event_type":"tool_result","data":{"content","isError"},"duration_ms"}（响应侧）
崩溃语义：主循环 panic→立即 kill 子进程并退出非零（透传断开与原进程一致，不吞错误）
实现选型：Node.js 单文件（MCP stdio 生态同构、无 Python 环境依赖）——随镜像预装
```

## 6. 依赖矩阵

- imports：rsi/core；（verifier 依赖 carrier.FakeCarrier 接口与 EvalTask）。
- 注入：Instrumentor←Scanner/Planner/Injector/Verifier/Remover/report+settings；injector 调 carrier.OverlayBuilder。
- DB：**写** instrumentation_reports（plan_json+coverage_json，batch 级）。
- 被依赖：orchestrator（run_batch 前 instrument 决策）、judge/optimizer（读覆盖报告——经 tracing/DB 查询）。

## 7. 测试映射与实施注记

**测试**：IN-004（扫描→计划→overlay→冒烟→报告全线）、IN-005（pg-mcp-proxy：echo MCP 透传+事件+崩溃断开）、TU-017（overlay 不改原始目录）。

**实施注记**：① 扫描零对象→空计划+报告标注（不算失败）；② 冒烟任务来源=冒烟集（FR3.7 P0），未含时降级首个任务（配置注记）；③ Verifier 用 FakeCarrier 跑冒烟（快且确定；真实载体验证随 EE）；④ rsi-trace-hook.ts 为平台自带资产（instrumentor/templates/ 内置，注入=复制+注册）；⑤ pg-mcp-proxy 用 Node 单文件（deploy/vendor/pg-mcp-proxy.mjs，约 120 行：spawn 子进程、stdin 直通、stdout 按行解析 JSON-RPC、tools/call 双侧写 sink）；⑥ coverage 报告 instrumentation_reports 表落库并附 batch_id（接口 §9.15 端点数据源）。
