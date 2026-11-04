# RSI-PG-DD-02 llm —— LLM 客户端 / 提示词库 / 卡带 / 记账

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-02 |
| 版本 | v1.0 |
| 上游 | 设计 3.12、接口 §13、配置 §2⑩；FR5.7、NFR2④；ADR-1/7/10 |
| 交付切片 | S1（cassette/记账先行，供全部 LLM 点测试与预算） |

## 1. 追溯与范围

**职责**：四个平台 LLM 智能点（attribution/hypothesis/critic/judge）+ sft 起草的统一调用设施——PromptLibrary 渲染 → LLMClient（cassette 命中回放 / provider 调用 / 结构化校验 / retry）→ Accountant 记账。

**不做什么**：不承载 SUT 自身 LLM 调用（其配置在 model.json，FR1.8；llm_calls 中 component='sut' 行由 tracing 回填）；不做流式（四个点全部非流式）；不做对话历史（无状态单轮调用，多轮语义由调用方在 prompt 内组装）。

## 2. 文件与类清单

```
rsi/llm/__init__.py
rsi/llm/client.py        # LLMClient / Provider 抽象与两实现
rsi/llm/prompt.py        # PromptLibrary
rsi/llm/cassette.py      # CassetteStore
rsi/llm/accounting.py    # Accountant（llm_calls 落库）
rsi/llm/prompts/         # Git 管理（platform-repo prompts/<component>/<name>.v<N>.md）
rsi/llm/cassettes/       # 录制资产（git 管理，测试用）
```

## 3. 数据结构

（LLMUsage/LLMResponse 见 DD-01 §3.7）

```python
Component = Literal["attribution", "hypothesis", "critic", "judge", "sft"]

class ProviderConfig(BaseModel):        # 配置 §2⑩ llm.providers 展开
    kind: Literal["anthropic", "openai_compat"]
    api_key_env: str                    # 环境变量名
    base_url_env: str | None = None     # openai_compat
    timeout_s: int = 120

class ComponentDefault(BaseModel):
    model: str; temperature: float = 0.0
    provider: str = "anthropic"         # providers 键
    backup_provider: str | None = None  # 故障转移（借鉴 nl2cli auto_switch）

class CassetteKey(NamedTuple):
    component: str; prompt_version: str; input_sha: str   # sha256(渲染后 messages 序列化)

class PromptTemplate:                   # PromptLibrary 加载产物
    component: str; name: str; version: int               # 文件名 <name>.v<N>.md
    body: str                                            # {{var}} 占位
    variables: set[str]                                  # 解析所得（渲染时校验全覆盖）
```

## 4. 对外接口

```python
class LLMClient:
    def __init__(self, providers: dict[str, ProviderConfig], defaults: dict[Component, ComponentDefault],
                 cassette: CassetteStore, accountant: Accountant, settings: LlmSettings): ...
    # v2.14：实例持有 asyncio.Semaphore(settings.max_concurrent_calls)（默认 4）
    # complete() 入口先 acquire——多批次并行时 SUT 调用+裁判判决并发受此信号量约束
    # 超时等待→按优先级排队（judge > hypothesis > attribution > critic > sft，优先级队列）
    async def complete(self, component: Component, prompt_name: str,  # 如 "classify"（版本自动取最新）
                       vars: dict[str, str],
                       schema: type[BaseModel] | None = None,
                       run_id: str | None = None, batch_id: str | None = None) -> LLMResponse:
        """流程（§5.1）；schema 提供时 .parsed 为校验后实例；校验 retry≤2 耗尽 → LLMOutputInvalid"""
    # judge 组件强制 temperature=0 且 provider/参数哈希进冻结清单（ADR-10；DD-09 verify_freeze 消费）

class PromptLibrary:
    def __init__(self, root: Path): ...                 # prompts/ 目录
    def render(self, component: str, name: str, version: int | None, vars: dict[str, str]) -> RenderedPrompt:
        """version=None 取最新；缺变量/多余变量 → ConfigError；RenderedPrompt{messages:[{role,content}], version}"""
    def latest(self, component: str, name: str) -> int
    def hash_of(self, component: str, name: str, version: int) -> str   # 冻结清单用

class CassetteStore:
    def __init__(self, mode: Literal["replay","record","off"], root: Path): ...
    def get(self, key: CassetteKey) -> LLMResponse | None
        # replay 模式未命中 → 直接 raise CassetteMissError（保证提示词演进必有回归资产，ADR-7）
    def put(self, key: CassetteKey, resp: LLMResponse) -> None           # record 模式；NDJSON 追加按组件分文件

class Accountant:
    async def record(self, *, component: str, model: str, prompt_version: str,
                     usage: LLMUsage, latency_ms: int, cost_usd: float | None,
                     run_id: str | None, batch_id: str | None) -> None   # llm_calls 插入（to_thread）
    async def sum_usage(self, scope: str) -> UsageSummary                # BudgetController 消费（DD-12）
```

## 5. 内部逻辑

### 5.1 complete() 主流程（伪代码）

```
tpl = prompt.render(component, name, vars)                 # ① 渲染
key = sha256(component, tpl.version, tpl.messages)
if cassette.mode != off:
    if resp := cassette.get(key): return resp              # ② 卡带回放（不记账？——记账，便于预算视角一致）
provider = defaults[component]
for attempt in 0..(2+1):                                   # ③ 真实调用：传输层 retry
    try: raw = await call_provider(provider, tpl, schema)  #    结构化策略 §5.2
         break
    except RetryableHTTP as e:                             # 429/5xx/网络/超时
        await sleep(min(2**attempt, 30) + uniform(0, 1))    # nl2cli 退避+抖动
    except ProviderDown:
        if provider.backup_provider: provider = switch; continue
        raise
if schema:
    parsed = validate(raw, schema)                         # ④ pydantic 校验
    if 失败 and schema_retry < 2: 将校验错误回注 messages 重试（attempt+=1）
    if 仍失败: salvage 解析（§5.3）→ 再校验 → 仍失败 raise LLMOutputInvalid
cassette.put(key, resp) ; accountant.record(...)           # ⑤ 记账（cost=usage×单价表，P0 可 None）
return resp
```

### 5.2 结构化输出策略（per-provider，网络核实 R1/R2）

| provider | 策略 | 说明 |
|---|---|---|
| anthropic | **forced tool_choice**：定义工具 `emit_json`（input_schema=目标 schema），`tool_choice={"type":"tool","name":"emit_json"}`；取 `content[0].input` | 千次零错路径；原生 `output_config/json_schema` 因 Opus 4.6 偶发空响应（anthropic-sdk#913）暂不为主 |
| openai_compat | `response_format={"type":"json_schema","json_schema":{...,"strict":True}}`（chat.completions）；SDK 新版可直接 `client.responses.parse(Model)` | strict 必需（openai-python#1733） |
| 兼容端点不支持时 | 降级：prompt 注入 schema + 指令"仅输出 JSON"（nl2cli 模式） | config `structured: auto\|off` 探测缓存 |

**统一兜底**：无论策略，返回内容一律过 pydantic 校验；失败进入 5.1 ④ 的重试与 salvage。

### 5.3 salvage 解析（借鉴 nl2cli 三策略，仅兜底）

```
1) 直接 json.loads
2) 提取 ```json ...``` 围栏
3) 平衡括号扫描首个完整 JSON 对象
```

### 5.4 种子派生（NFR2④，temperature>0 组件用）

```
seed_component = derive(seed.master, component)            # sha256 截断
seed_request   = derive(seed_component, cassette_key.input_sha)
→ anthropic 无 seed 参数：用于采样温度与调用顺序确定性记录；openai_compat 透传 seed（若支持）
```

## 6. 依赖矩阵

- imports：anthropic、openai（官方 SDK）、pydantic、rsi/core（models/errors）、rsi/settings；**不依赖业务模块**。
- 注入：LLMClient ← PromptLibrary + CassetteStore + Accountant + settings。
- DB 表：**写** llm_calls；读 无。
- 被依赖：optimizer（DD-11 attribution/hypothesis/critic/sft）、judge（DD-08 llm_judge）；测试全部 LLM 点（cassette）。

## 7. 测试映射与实施注记

**测试**：IN-006/007/018（cassette 回放链）、TU-032（judge 语义判定 + 温度 0 稳定性——同 key 重放 N 次输出一致天然成立；真实温度 0 稳定性 P0 用 record 模式录制 3 次对比）；卡带未命中 fail 单测。

**提示词模板骨架**（prompts/ 初始 8 个文件，body 详文随 S6b 编写评审）：

| 文件 | 结构骨架 |
|---|---|
| attribution/classify.v1.md | 角色（网络运维归因专家）/ 输入（簇样本轨迹摘要+SUT 原文证据段）/ 任务（失败模式分类+A/B 判定规则）/ 输出 schema 说明 / few-shot 1 例 |
| hypothesis/diagnose.v1.md | 问题单生成：输入=簇+证据 / 输出=ProblemStatement schema |
| hypothesis/propose.v1.md | 提案：输入=问题单+可改要素原文+失败假设摘要 / 约束（仅 enabled_targets、单点）/ 输出=Hypothesis schema |
| critic/review.v1.md（P2） | 六类审查清单逐项 / 输出=CriticVerdict schema |
| judge/path_quality.v1.md | 输入=轨迹摘要+critical_steps / 判定 pass/suspect 规则 / 输出 schema |
| judge/checklist.v1.md | 检查项语义命中判定 / 输出=漏检误报列表 schema |
| judge/root_cause.v1.md | 期望自然语言 ↔ SUT 结论语义比对 / 输出={match: bool, confidence, indeterminate_reason} |
| sft/draft.v1.md（P1） | 报告起草：输入=证据+尝试历史 / 输出=报告分节 schema |

**实施注记**：① provider 调用经 `asyncio.to_thread`（两 SDK 均同步客户端；AsyncClient 可选评估，P0 to_thread 统一）；② 卡带文件 `cassettes/<component>/<name>.v<N>/<input_sha>.json`（含 resp+usage，便于成本回放统计）；③ `structured: auto` 探测结果缓存于 provider 实例（首次调用试 json_schema，报 4xx 则降级并记日志）；④ anthropic forced-tool 响应取 `tool_use.input` 后仍跑 pydantic（schema 子集兼容）；⑤ 记账 cost_usd P0 允许 None（单价表 P1 完善），BudgetController 对 None 宽容。
