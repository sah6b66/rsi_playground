# 使用指南

## 用户需要准备的环境与文件

### 环境准备清单

| # | 项目 | 说明 | 参考文档 |
|---|------|------|---------|
| 1 | Python 3.12 + uv | 平台运行环境 | [INSTALL.md](INSTALL.md) |
| 2 | Docker Desktop | 本地 SUT 容器运行 | [INSTALL.md](INSTALL.md) |
| 3 | 仿真后端 | ENSP Pro（Windows VM）或 PNETLab（Linux VM）或两者 | [DEPLOYMENT.md](DEPLOYMENT.md) |
| 4 | SSH 互通 | 主节点 → 各仿真后端/远端 Docker 主机 | [DEPLOYMENT.md](DEPLOYMENT.md) |
| 5 | 网段规划 | 各节点网段全网唯一 | [DEPLOYMENT.md](DEPLOYMENT.md#网络规划) |
| 6 | LLM API Key | Anthropic / OpenAI 兼容端点（settings.yaml `llm.providers.*.api_key`） | [INSTALL.md](INSTALL.md) |
| 7 | 密钥 | RSI_JWT_SECRET + RSI_MASTER_KEY | [INSTALL.md](INSTALL.md) |

### 文件准备清单

| # | 文件 | 来源 | 说明 |
|---|------|------|------|
| 1 | `settings.yaml` | 复制 `examples/settings.yaml.example` | 平台配置（修改后端 IP、网段等） |
| 2 | 评测集三件套 | 参照 `examples/evalsets/` | env.yaml + tasks.jsonl + configs/ |
| 3 | SUT Agent 配置树 | 参照 `examples/suts/ipops-agent/` | 系统提示词 + skills + mcp.json + model.json |
| 4 | SUT Agent Docker 镜像 | 自行构建 | 包含载体 CLI + /trace/ 目录 |
| 5 | 优化配置方案 | 参照 `examples/profiles/` | 目标权重 + 优化项 + 预算 |

## 日常操作

### 1. 登录

```bash
# 获取 Token（admin 密码来自安装时的 python -m rsi.init：
# --password 参数、RSI_ADMIN_PASSWORD 或随机生成打印的那一次）
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<你的密码>"}'
# → {"token": "eyJ...", "expires_at": 1762339200, "user_id": "...", "role": "admin"}

# 后续请求携带
export TOKEN="eyJ..."
```

### 2. 创建 SUT Agent

```bash
# 通过 API
curl -X POST http://localhost:8000/api/v1/suts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ipops-agent",
    "carrier": "opencode",
    "image_ref": "ipops-agent:v1.2",
    "description": "IP 运维 Agent（OpenCode 载体）"
  }'
# → 201 {"id": "sut-..."}
```

### 3. 导入评测集

**方式一：通过 WEB 界面**
1. 访问 http://localhost:8000 → 评测集页面
2. 点击"导入评测集"
3. 拖拽 .zip 文件（包含 env.yaml + tasks.jsonl + configs/）
4. 确认预览信息 → 点击"导入"

**方式二：通过 API**
```bash
curl -X POST http://localhost:8000/api/v1/evalsets:import \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@bgp-faults-holdout.zip"
```

### 4. 冻结主评测集

```bash
curl -X POST http://localhost:8000/api/v1/evalsets/{evalset_id}:freeze \
  -H "Authorization: Bearer $TOKEN"
# 冻结后不可修改（内容哈希锁定）
```

### 5. 创建优化配置方案

`POST /profiles` 接收 JSON body（`examples/profiles/bgp-accuracy-first.yaml`
是带注释的 YAML 样例，供阅读/复制，不能直接 `-d @` 发送——需按下例转成 JSON，
或经 WEB 优化页面导入）：

```bash
curl -X POST http://localhost:8000/api/v1/profiles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "bgp-accuracy-first",
    "evalsets": {
      "iterate": "bgp-faults-iterate",
      "holdout": "bgp-faults-holdout",
      "smoke": "core-smoke"
    },
    "goals": [
      {"metric": "root_cause_accuracy", "direction": "max", "weight": 0.7},
      {"metric": "avg_tokens", "direction": "min", "weight": 0.3}
    ],
    "enabled_targets": ["system_prompt", "skills", "knowledge", "model_config"],
    "guards": {"missed_check_worsen": "reject", "safety_any_worsen": "reject", "high_risk_worsen": "reject"},
    "stop": {"max_rounds": 20, "no_gain_rounds": 5},
    "budget": {"max_tokens": 50000000, "max_wall_hours": 8},
    "llm_compare": {"candidates": ["claude-sonnet-4-6", "glm-5"]}
  }'
```

### 6. 触发评测批次

```bash
curl -X POST http://localhost:8000/api/v1/batches \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "evalset_id": "bgp-faults-holdout",
    "sut_id": "sut-...",
    "purpose": "iterate"
  }'
# → 201 {"id": "batch-...", ...}
# purpose 可选值：iterate（日常迭代）| holdout（主评测）| smoke（冒烟）| compare（对比）
```

### 7. 查看批次进度

```bash
curl http://localhost:8000/api/v1/batches/{batch_id} \
  -H "Authorization: Bearer $TOKEN"
# → {"id": "batch-...", "status": "...", "progress": {"total": 10, "done": 3}, ...}
```

### 8. 启动优化迭代

**P0 人工模式（WEB 向导）：**
1. 访问优化页面 → "P0 人工模式"
2. 跑基线批 → 人工修改配置 → 跑候选批 → 对比 → 人工晋升

**P1 半自动模式（推荐）：**
```bash
curl -X POST http://localhost:8000/api/v1/iterations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sut_id": "sut-...",
    "profile_id": "prof-...",
    "mode": "p1"
  }'
```

P1 模式下，WEB 优化页面会显示四个人工门：
- **门 ① 方向确认**：查看归因报告 → 勾选要攻克的失败簇
- **门 ② 假设提交**：表单人工填写改进假设（单点修改 + 预期收益；LLM 自动起草属 P1 计划能力，当前未接入）
- **门 ③ 预审裁决**：查看确定性检查结果 + diff → 通过/拒绝
- **门 ④ 决策执行**：查看 Selector 建议 → 晋升/回退

### 9. 查看效果看板

访问 Dashboard 页面：
- 成功率/误报/漏检 率趋势
- Token / 步数 / 耗时 对比
- 版本晋升历史
- A/B 类缺陷分布
- 外部依赖健康状态

## 评测集编写规范

### env.yaml

```yaml
name: my-env-name                    # 环境名（唯一）
backend: ensp-pro                    # ensp-pro | pnetlab | physical | containerlab | fake
devices:
  - name: R1                        # 设备名（全局唯一）
    type: vrp                       # vrp | ce | ios
    config: configs/R1.cfg          # 初始配置文件
topology:
  links:
    - endpoints: [R1:GE0/0/1, R2:GE0/0/1]
      type: ethernet
ssh_access:
  R1:
    ip: 10.101.1.1
    username: admin
    password: Lab@123               # 或 password_ref: cred-name
```

### tasks.jsonl（每行一个任务）

```json
{
  "task_id": "fault/my-fault-01",
  "title": "任务标题",
  "prepare": [
    {"action": "exec_cli", "entries": [{"device": "R1", "cli_lines": ["命令1", "命令2"]}]}
  ],
  "task": {"prompt": "任务描述...以 <FINAL_JSON>{...}</FINAL_JSON> 输出。"},
  "expected": {
    "root_cause": {"expected": "期望的根因描述（自然语言）"},
    "final_state": [{"device": "R1", "cli": "display xx", "expect_contains": ["UP"]}],
    "critical_steps": [{"evidence": "display xx"}]
  },
  "metrics": ["root_cause_accuracy"]
}
```

### 关键规则

- `task_id` 格式：`<fault|inspection|cutover>/<现象>-<两位序号>`（如 `fault/bgp-peer-down-01`）
- 任务中设备以**名称**引用（不含 IP）
- `prepare` 仅支持 `exec_cli`（call_api 已随 ADR-27 删除；API 模式的环境准备走网络准备 API）
- `root_cause.expected` 是自然语言描述（LLM 语义比对），不是关键词
- 同一 evalset 的所有任务共享同一个 env.yaml 环境

## SUT Agent 镜像准备

### 镜像必须包含

| 路径 | 说明 |
|------|------|
| `/trace/` | 轨迹输出目录（卷挂载点） |
| 载体 CLI | OpenCode 或 Claude Code 可执行文件 |
| prompt-logger | OpenCode 载体需预装 opencode-prompt-logger |

### 配置通过挂载注入（不烧录在镜像内）

| 挂载点 | 内容 |
|--------|------|
| `/agent-config/system-prompt.md` | 系统提示词 |
| `/agent-config/skills/` | Skill 文件 |
| `/agent-config/mcp.json` | MCP 工具配置 |
| `/agent-config/model.json` | LLM 模型配置 |
| `/agent-config/memory/` | 记忆文件 |
| `/agent-config/scripts/` | 程序脚本 |
| `/agent-config/knowledge/` | 知识库 |

### 构建示例

```dockerfile
FROM node:20-slim

# 安装 OpenCode CLI（npm 包名是 opencode-ai，可执行文件叫 opencode）
RUN npm install -g opencode-ai

# 安装 prompt-logger 插件
RUN opencode plugin add opencode-prompt-logger

# 预留轨迹目录
RUN mkdir -p /trace

# 载体入口
ENTRYPOINT ["opencode"]
```

## 故障排查

| 问题 | 排查 |
|------|------|
| 登录 401 | 未初始化或密码不对：`uv run python -m rsi.init`；已有 admin 不会被覆盖，可在 WEB 参数→用户页改密 |
| 批次一直 building | 检查仿真后端可达：`GET /api/v1/sandbox/backends`（预建模式为 URL 探活） |
| SUT 容器启动失败 | 检查 Docker：`GET /api/v1/system/dependencies` 看 docker 状态 |
| LLM 调用失败 | 检查 settings.yaml 的 `llm.providers.*.api_key` + `GET /api/v1/system/dependencies` 看 llm 状态 |
| 评测结果全 0 | 查看 `GET /api/v1/runs/{id}/judgment` 的 safety.violations |
| 迭代一直等待 | P1 模式检查四个人工门是否已提交 |
| 轨迹为空 | 检查 SUT 容器 /trace/ 目录挂载 + collector 4317/4318 端口 |
