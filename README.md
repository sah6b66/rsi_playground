# RSI Playground

网络运维 Agent 的递归自我改进（RSI）演练场——为网络运维 Agent 提供可控、可复现、可度量的持续优化闭环基础设施。

## 快速开始

```bash
# 1. 前置条件：Python 3.12+ / uv / Docker Desktop
# 2. 克隆并安装
git clone https://github.com/sah6b66/rsi_playground.git
cd rsi_playground

# 3. 安装依赖
cd platform && uv sync

# 4. 复制配置样例并修改
cp ../examples/settings.yaml.example ../settings.yaml
# 编辑 settings.yaml，至少设置：
#   sandbox.backends.ensp-pro-1.host  → 你的 ENSP Pro 主机 IP
#   carrier.hosts.main.subnets       → 本机 Docker 网段

# 5. 设置环境变量（密钥类）
export RSI_JWT_SECRET=$(openssl rand -hex 32)
export RSI_MASTER_KEY=$(openssl rand -base64 32)
export ANTHROPIC_API_KEY=sk-ant-...

# 6. 启动
uv run uvicorn rsi.api.app:create_app --factory --port 8000

# 7. 访问
#   API: http://localhost:8000/api/v1/docs (Swagger)
#   健康检查: http://localhost:8000/api/v1/system/health
```

详细指南：[INSTALL.md](INSTALL.md) | [USER_GUIDE.md](USER_GUIDE.md) | [DEPLOYMENT.md](DEPLOYMENT.md)

## 核心能力

| 能力 | 说明 |
|------|------|
| 评测闭环 | 构建仿真环境 → 注入故障 → SUT 执行 → 三层判决 → 版本晋升 |
| 三层判决 | 确定性断言（终态/安全）+ LLM 语义判决（根因/路径/一致性）+ 一票否决 |
| 优化引擎 | 归因分析 → 假设生成 → 预审 → 应用 → 三级验证 → 决策 |
| 自优化模式 | P0 人工 / P1 半自动（人工门）/ P2 全自动，同一状态机可切换 |
| 多实例并行 | 多沙箱实例（不同 VM）+ 多 SUT 容器（本地 + 远端 Linux SSH） |
| 全依赖监控 | Docker/沙箱/SUT/LLM/Git/DB/Collector/API 8 类健康实时可视 |

## 项目结构

```
rsi_playground/
├── docs/                    # 设计文档（需求/概要/配置/接口/测试 + 15 个详细设计）
├── examples/                # 样例文件（settings/评测集/SUT 配置树/优化方案）
├── platform/                # Python 后端
│   ├── rsi/                 # 核心包（12 个模块）
│   │   ├── core/            # 领域模型/枚举/命名/异常
│   │   ├── db/              # 22 张 SQLModel 表
│   │   ├── security/        # JWT/RBAC/凭证/审计
│   │   ├── versioning/      # Git 版本管理
│   │   ├── sandbox/         # 仿真环境抽象（FakeSandbox + 真实后端）
│   │   ├── carrier/         # SUT 容器管理（本地 + 远端 SSH）
│   │   ├── tracing/         # 轨迹采集与查询
│   │   ├── llm/             # LLM 客户端（离线 mock / 真实 API）
│   │   ├── judge/           # 三层判决系统
│   │   ├── decision/        # 迭代决策（噪声带/显著性/规则序）
│   │   ├── optimizer/       # 优化引擎（归因/假设/预审/应用）
│   │   ├── orchestrator/    # 编排引擎（批次/迭代/预算/断点/事件）
│   │   ├── instrumentor/    # 插桩流水线
│   │   └── api/             # 19 组 REST Router + SSE/WS
│   └── tests/               # 655+ 测试（全离线可跑）
└── open_source/             # 参考实现（DGM/RRSI/OpenEvolve，仅阅读）
```

## 技术栈

Python 3.12 / FastAPI / Pydantic v2 / SQLModel (SQLite WAL → PostgreSQL) / GitPython / structlog / Docker (docker-py) / pytest

## 许可证

Private
