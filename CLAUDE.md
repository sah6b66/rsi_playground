# RSI Playground — 项目操作规程

网络运维 Agent 的 RSI（递归自改进）演练场平台。文档为唯一权威，代码生成与审计都对照文档执行。

## 文档体系（上游权威，修改代码前先对照）

| 文档 | 内容 | 版本随修订记录滚动 |
|------|------|------|
| `docs/_rsi_playground_requirements.md` | FR/NFR/I 接口需求 | 修订记录为权威版本号 |
| `docs/_rsi_playground_design.md` | 12 子系统 + ADR-1~24 + 附 C 载体事实 | |
| `docs/_rsi_playground_interfaces.md` | §2 数据契约+DDL 22 表 / §9 19 组 Router 端点 | |
| `docs/_rsi_playground_configuration.md` | settings 键清单 | |
| `docs/_rsi_playground_test.md` | TU/IN/EE/SIM 用例（60+ 条） | |
| `docs/detail_design/RSI-PG-DD-00..14` | 各子系统详细设计（冲突时 DD 优先于概要设计） | |

代码在 `platform/rsi/`（Python 3.12 + uv + FastAPI + SQLModel/SQLite），测试在 `platform/tests/`。

## 常用命令

```bash
cd platform
uv run pytest -q                    # 全量回归（全离线，无需 Docker/网络/LLM）
uv run pytest <file> -q             # 单文件
uv run pytest -m "sim or e2e" -q   # 占位用例（全 skip 属正常）
```

## 已知噪音（不要浪费精力）

- **Pyright 报 `rsi.*` import 无法解析**是 IDE 配置问题（tests/uv run 恒通过）——一律忽略
- **编辑中途的 diagnostics 常为过期快照**（引用"未定义"的符号实际已定义）——先 grep 核实再信
- `Optional` 成员访问告警多为 `_require()` 类守卫后的类型收窄限制，运行时安全
- 个别 t-test 用例因随机样本偶发失败，重跑即过

## 审计任务执行规程（用户定期发起，无需再确认细节）

1. **T1 子系统审计**：4 组并行只读代理——A(core+db+security+versioning) B(sandbox+carrier+instrumentor) C(tracing+llm+judge) D(decision+optimizer+orchestrator+api)。每组对照对应 DD + 上游五文档，逐项 ✅/⚠️/❌ + file:line 证据，全文写 `platform/.auditN/T1-{a,b,c,d}.md`，返回 ≤300 字摘要
2. **T2 跨子系统审计**：可并行——跨模块接口对账、数据流贯通（run_id/config_version/lab_id）、IN/EE/SIM 逐条映射、集成测试质量，写 `.auditN/T2.md`
3. 审计代理只读（除报告文件），测试基线以 `uv run pytest -q` 为准
4. 汇总后按主题分修复批次（F1..Fn），**依次**派实现子代理（实现类绝不并行），每批：测试先行 → 全量回归零失败 → 独立 commit → 报告 `.fN-report.md`
5. 修复完成后：终审（opus，按风险域拆 1-2 个并行审查代理）→ 处理阻塞项 → 合并 → 推送

## Git 约定

- 修复分支 `feat/<topic>-fixes`，自 main 切出；完成后 `git checkout main && git merge feat/... --no-ff` 合并
- **合并后直接 `git push origin main`**（用户已授权此标准流程，无需每次确认）
- push 偶发 SSL 握手失败：等 10-15s 重试即可

## 项目特有约定（勿"纠正"）

- Python 模块名 `ensp_pro.py`（下划线），配置键/env.yaml 枚举 `ensp-pro`（连字符）——有意区分
- 评测全离线：FakeSandbox/FakeCarrier/mock LLMClient(handler 注入) 是设计的 P0 形态；真实 Docker/netmiko/载体 CLI 属 S11 切片，缺实现不算缺陷（除非本轮审计目标明确包含）
- SandboxService 池化设备会话必须经 `_run_sync` runner loop 执行（DeviceSession.lock 绑定事件循环——H1 教训）
- DB 写入（落库/审计）一律 try/except + warning，不得阻断主流程
- 缓存键 = ADR-5 双内容哈希（task_content_hash + env_content_hash），噪声标定批必须 bypass_cache
- `docs/superpowers/plans/`、`platform/.audit*/`、`platform/.f*-report.md`、`platform/.review-*.diff` 是工作产物，合并前清理未跟踪的报告文件

## 交付状态基线（2026-11-05 第二轮审计修复后）

1059 passed + 12 skipped（sim/e2e 占位）。遗留（记录在案，不阻塞）：
- ensp/pnetlab 真实后端、LLM provider 适配/退避重试/backup_provider → S11/P1
- runs/batches 落库的 FK 完整性（evalset_id/sut_id 空串占位）→ API 层补传 db_meta
- /system/doctor 响应字段与 DD 措辞的细微出入
