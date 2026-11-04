# RSI-PG-DD-07 tracing —— 轨迹子系统

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-07 |
| 版本 | v1.0 |
| 上游 | 设计 3.5、接口 §5/§8；FR2、ADR-3；ADR-22（OpenCode P0） |
| 交付切片 | S5（collector+store+seq） |

## 1. 追溯与范围

**职责**：trace-collector 独立进程——wiretap/插桩 NDJSON 尾随→解析为 TraceEvent→统一 seq→落盘+索引+WS 推流；finalize（EOF+宽限）；TraceStore 查询/统计/聚类（P1）；脱敏管道（P1）。

**不做什么**：OTLP 接收（Claude Code 载体 P2，§7②预留）；不修改载体原生帧（wiretap 原文永久保留）；不做归因（optimizer 消费其数据）。

## 2. 文件与类清单

```
rsi/tracing/__init__.py
rsi/tracing/collector.py     # TraceCollector（tail/ingest/finalize/WS hub）
rsi/tracing/parsers.py       # OpenCodeWiretapParser / NdjsonEventParser（P2: StreamJsonParser/OtelEvents）
rsi/tracing/seq.py           # SeqAllocator
rsi/tracing/store.py         # TraceStore（query/stats/cluster P1）+ meta.json
rsi/tracing/redact.py        # Redactor（P1）
rsi/tracing/differ.py        # TraceDiffer（P1，FR10.8）
```

## 3. 数据结构

（TraceEvent/WiretapFrame 见 DD-01）

```python
class TailCursor(BaseModel):
    file: str; offset: int; inode: int | None     # 断点续读位点（meta 持久）

class TraceFilter(BaseModel):                      # :query/:stats 请求体
    run_ids: list[str] | None; task_id: str | None
    event_types: list[EventType] | None; is_error: bool | None
    from_ts: str | None; to_ts: str | None

class CollectorSettings: ...                       # listen/grpc/http、grace_s、poll_interval_ms
```

## 4. 对外接口

```python
class TraceCollector:                              # 独立进程（uvicorn 旁路服务/compose 组件）
    def __init__(self, settings, store: TraceStore, redactor: Redactor | None, ws_hub: WsHub): ...
    async def tail_ndjson(self, path: Path, run_id: str) -> None
        # 目录 watch（/trace/events.ndjson、/trace/wiretap/<run>.ndjson）：
        # 轮询 stat（interval 500ms）→ 增量读→逐行解析→ingest；TailCursor 持久（崩溃续读）
    async def ingest(self, events: list[TraceEvent]) -> None
        # redact(P1)→seq 分配→events.ndjson 追加落盘→trace_events_index 批量插入→WS fan-out
    async def finalize(self, run_id: str) -> None
        # ① EOF 判定：所有尾随文件 offset 两轮采样不变 ② OTel 宽限 grace_s（默认 5）
        # ③ 写 meta.json（event_count/bytes/时间范围） ④ trace_files 更新 ⑤ 关闭该 run WS
        # 宽限后迟到事件照常入库（late=True，seq 续增）
    # P2：async def serve_otel(self) —— 见 §7②

class SeqAllocator:
    def next(self, run_id: str) -> int             # 进程内 dict[run_id]->计数；唯一权威（NFR2⑤）

class TraceStore:
    async def query(self, f: TraceFilter, cursor: str | None, limit: int) -> Page[TraceEventRef]
        # events.ndjson 读 + trace_events_index 预过滤；游标=(run_id, seq)
    async def stats(self, f: TraceFilter) -> TraceStats               # token/步数/耗时分布
    async def cluster_failures(self, f, group_by: FailureFeature) -> Clusters   # P1（FR2.5）

class TraceDiffer:   # P1
    def diff(self, run_a: str, run_b: str) -> TraceDiff
        # 事件级对齐：归一化步骤序（LLM 轮次 × tool 名）；输出新增/消失工具调用、
        # 命令序列 diff、system/messages 差异摘要、逐步时延与 token 对比

class Redactor:      # P1
    def redact(self, ev: TraceEvent, fingerprints: list[SecretFingerprint]) -> TraceEvent
        # 精确匹配（secret 原值+base64 → '***'，仅命中子串替换，其余逐字节保留）+ 规则表 regex
```

## 5. 内部逻辑

### 5.1 OpenCode wiretap 解析（P0 主通道，TU-031）

```
for line in wiretap.ndjson:
    frame = json.loads(line)
    if frame["type"]=="assembled" and frame["kind"]=="context":        # 主循环请求
        → llm_request：data={"system": [s.text for s in frame["system"]],
                            "messages": frame["messages"], "tools": frame["tools"],
                            "usage": {}, "carrier_meta": {sessionID, model}}
    elif frame["type"]=="http" and frame["kind"]=="primary":           # 原始 provider body
        → llm_request 补充帧：data.body=frame["body"]（原文）；
          协议探测：body JSON 含 "messages"→chat_completions（messages[0].content=system）
                   含 "input"→responses（instructions=system）——记录 protocol 字段
    at=frame["at"]（ISO 时间戳→ts）
kind 命名差异：assembled=context ↔ http=primary（同一请求两视角，附 C.3）
title/compaction/generate 帧过滤（非主循环，不计 llm_request）
```

### 5.2 token 用量（附 C.5⑥ 降级路径）

P0 OpenCode：prompt-logger 无响应 usage → **字符数近似估算**：`usage.input_tokens ≈ len(system+messages)//4`、`estimated=True`（Verdict.efficiency.estimated 透传）；`opencode run --format json` 末帧若含 usage 字段（PoC 项）则以实测覆盖估算。

### 5.3 ingest 管道

```
parse(§5.1/5.2 或 events.ndjson 原生 TraceEvent 行)
 → redactor（P1）
 → ev.seq = seqalloc.next(ev.run_id)   # 文件内顺序=到达序=seq 序（NFR2⑤）
 → 追加 var/traces/<run_id>/events.ndjson（最终轨迹正文，与中转文件分离）
 → trace_events_index 批量 upsert（每 50 条或 500ms flush）
 → ws_hub.publish(run_id, ev)          # 尾部订阅者
```

## 6. 依赖矩阵

- imports：rsi/core、rsi/settings、httpx（无）；watchfiles（目录轮询可选）或纯 stat 轮询。
- 注入：TraceCollector←TraceStore+SeqAllocator+Redactor?(P1)+WsHub。
- DB：**写** trace_files、trace_events_index；文件=var/traces/**（正文）+ labs 内中转文件（只读）。
- 被依赖：orchestrator（finalize 调用，DD-12 步骤 6）、api（WS /streams/runs、traces:query/stats/diff）、judge/optimizer（经 store 查询）。

## 7. 测试映射与实施注记

**测试**：TU-031（wiretap 解析：assembled/http 帧、kind 差异、协议差异、过滤 title/compaction）、IN-014（P0 断言：llm_request 含完整 system+messages+tools、per-run 文件归属、多源统一 seq 单调）、EE-008（>1MB 输出全量入库、逐事件 ts、SUT 日志归档）、TU-018（Redactor P1）、IN-016（query/stats+轨迹-版本反查）。

**实施注记**：① **OTLP 接收为 P2**（Claude Code 载体启用时）：实现路径=grpc.aio 挂 opentelemetry-proto 的 `LogsService` 服务定义（opentelemetry-proto 包含 pb2/grpc_py 生成物），事件映射按附 C.2（api_request→usage 维度、user_prompt→任务输入…）——P0 不引入该依赖；② collector 与 platform-api 同进程不同 Task（P0 单机简化）或独立进程（compose 部署形态）——接口不变；③ wiretap 原文不删不改动（审计原文），最终轨迹 events.ndjson 是独立产物；④ 归档老化（P1）：zstd 压缩 var/traces 中已 finalize 超过 hot_days 的 run 目录，在线数据不动；⑤ WS hub 有界队列（每订阅者 256，满则丢旧保新，与 EventBus 语义一致）。
