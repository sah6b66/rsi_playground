# 部署指南

## 部署架构

### 单机部署（P0 最简）

```
┌─────────────────────────────────────────────────────────┐
│                    Windows 主节点                        │
│                                                          │
│  ┌──────────────┐   ┌──────────────────────────────┐   │
│  │ Platform API │   │     Docker Desktop            │   │
│  │  :8000       │   │  ┌────┐ ┌────┐ ┌────┐       │   │
│  │  ├ FastAPI   │   │  │SUT1│ │SUT2│ │SUT3│       │   │
│  │  ├ SQLite    │   │  └────┘ └────┘ └────┘       │   │
│  │  ├ Git bare  │   │  lab-net  trace-net          │   │
│  │  └ Collector │   └──────────────────────────────┘   │
│  └──────────────┘                                        │
│           │ HTTPS 探活（平台网页入口）+ 设备级 SSH          │
│           ▼                                              │
│  ┌──────────────┐                                       │
│  │ ENSP Pro VM  │  仿真设备（R1, R2, ...）              │
│  │ (Windows)    │  10.101.1.0/24 网段                   │
│  └──────────────┘                                       │
└─────────────────────────────────────────────────────────┘
```

> ADR-25：eNSP Pro / PNETLab 是**网页虚机/平台，不可 SSH 编排**。平台对它们只做
> URL 探活（任何 HTTP 响应含 401/403 = 在线）；命令走**设备级 SSH 会话池**
> （netmiko，设备 IP:端口来自评估集 env.yaml 的 ssh_access，与平台 IP 不同）。

### 多机并行部署（P0 多实例）

```
┌────────────────────────────────────────────────────────────────┐
│                      Windows 主节点                             │
│                                                                 │
│  ┌──────────────┐   ┌─────────────────────────────────────┐   │
│  │ Platform API │   │        Docker Desktop (main)        │   │
│  │  :8000       │   │  ┌────┐ ┌────┐                    │   │
│  │  ├ SQLite    │   │  │SUT1│ │SUT2│  max=10            │   │
│  │  ├ Git bare  │   │  └────┘ └────┘                    │   │
│  │  └ Collector │   └─────────────────────────────────────┘   │
│  └──────┬───────┘                                              │
│         │ 探活+设备 SSH          │ SSH（Docker over SSH）        │
│         ▼                        ▼                             │
│  ┌──────────────┐       ┌──────────────┐                      │
│  │  ENSP Pro    │       │  PNETLab     │                      │
│  │  VM (Win)    │       │  VM (Linux)  │                      │
│  │  10.101.x.x  │       │  10.102.x.x  │                      │
│  └──────────────┘       └──────────────┘                      │
│                                                                 │
│         │ SSH (Docker Remote)                                   │
│         ▼                                                       │
│  ┌──────────────────┐  ┌──────────────────┐                   │
│  │  Linux GPU-01    │  │  Linux GPU-02    │                   │
│  │  Docker Daemon   │  │  Docker Daemon   │                   │
│  │  ┌────┐ ┌────┐  │  │  ┌────┐ ┌────┐  │                   │
│  │  │SUT3│ │SUT4│  │  │  │SUT5│ │SUT6│  │                   │
│  │  └────┘ └────┘  │  │  └────┘ └────┘  │                   │
│  │  max_instances=10│  │  max_instances=10│                   │
│  └──────────────────┘  └──────────────────┘                   │
└────────────────────────────────────────────────────────────────┘
```

### 网络规划

| 网段 | 用途 | 分配给 |
|------|------|--------|
| 10.100.0.0/16 | 主节点 lab 网段池 | carrier.hosts.main.subnets |
| 10.101.0.0/16 | ENSP Pro 设备管理网段 | sandbox.backends.ensp-pro-1.subnets |
| 10.102.0.0/16 | PNETLab 设备管理网段 | sandbox.backends.pnetlab-1.subnets |
| 10.103.0.0/16 | GPU-01 lab 网段池 | carrier.hosts.gpu-01.subnets |
| 10.104.0.0/16 | GPU-02 lab 网段池 | carrier.hosts.gpu-02.subnets |

> **重要**：各网段必须全网唯一（含嵌套重叠，如 10.101.1.0/24 与 10.101.0.0/16 视为冲突）。
> 部署前在仓库根运行校验：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\check-subnets.ps1
# 读取 <RSI_HOME>/settings.yaml（默认 platform/var/），重叠/格式错误时退出码 1
```

### 节点间路由要求

- 主节点 ↔ 执行节点：SSH 可达（22 端口）
- 主节点 ↔ 远端 Docker：SSH 可达（Docker over SSH）
- 主节点 → 仿真平台：HTTPS 可达（URL 探活，自签证书默认不校验）
- SUT 容器 → 仿真设备：设备管理网段路由可达（22 端口，netmiko SSH）
- SUT 容器 → Collector：主节点 4317/4318 端口可达

## 多实例并行配置

### 本地 + 远端混合

```yaml
# platform/var/settings.yaml（RSI_HOME 下）
carrier:
  hosts:
    main:
      docker: local
      subnets: [10.100.0.0/16]
    gpu-01:
      docker: ssh://ops@192.168.1.100
      subnets: [10.103.0.0/16]
      max_instances: 10
    gpu-02:
      docker: ssh://ops@192.168.1.101
      subnets: [10.104.0.0/16]
      max_instances: 10
  max_instances_per_host: 10

sandbox:
  backends:
    ensp-pro-1:
      kind: ensp-pro              # ensp-pro | pnetlab | physical | fake
      url: https://192.168.1.50:8443   # 平台网页入口（探活目标，不可 SSH）
      verify_tls: false           # 自签证书默认不校验
      subnets: [10.101.0.0/16]
    pnetlab-1:
      kind: pnetlab
      url: http://192.168.1.60
      verify_tls: false
      subnets: [10.102.0.0/16]

orchestrator:
  max_parallel_batches: 2
```

> 设备 SSH 凭证/IP 不在 backends 配置——每个评测集 env.yaml 的 `ssh_access`
> 按设备声明（设备 IP ≠ 平台 IP）。

### 远端 Linux 服务器准备

每台远端 Linux 服务器需要：

```bash
# 1. 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ops

# 2. 配置 SSH 免密登录（从主节点）
# 在主节点执行：
ssh-keygen -t ed25519
ssh-copy-id ops@192.168.1.100

# 3. 验证 Docker 可达（从主节点）
ssh ops@192.168.1.100 docker version

# 4. 开启 Docker TCP over SSH（通常已默认支持）
# docker-py 会自动通过 SSH 隧道连接，无需额外配置
```

## Docker 网络隔离

每个 SUT 容器加入两个网络：

| 网络 | 用途 | 可达目标 |
|------|------|---------|
| `lab-<id>` | 评测环境通信 | 同 lab 的仿真设备 |
| `trace` | 轨迹上报 | Trace Collector (4317/4318) |

**不加入 `mgmt` 网络** → 平台管理面对 SUT 不可达（安全边界）。

## 仿真后端部署（ADR-25 预建模式）

平台不经任何编排 API 操作仿真平台——拓扑**人工预建**，平台只做探活监控与设备级 SSH。
两个后端步骤一致：

### ENSP Pro（Windows VM）

1. 安装 ENSP Pro 到 Windows 虚拟机，确认网页入口可访问（如 `https://<VM IP>:8443`）
2. **在平台网页上人工预建拓扑**（节点/布线/启动设备），按评测集 env.yaml 对齐设备名
3. 在每台设备上配置 SSH（VRP：stelnet/AAA/local-user），与 env.yaml `ssh_access` 一致
4. 在 settings.yaml 配置后端（上例 `ensp-pro-1` 形态），或经 WEB 参数页在线配置
5. 验证：

```bash
# 平台侧：URL 探活（任何 HTTP 响应含 401/403 = 在线）
curl -k https://192.168.1.50:8443 -o /dev/null -w "%{http_code}\n"
# 平台侧：自检（含 backends 配置/旧字段告警/子网重叠检查）
cd platform && uv run python -m rsi.doctor
# 设备侧：从主节点直接验证设备 SSH（IP 来自 env.yaml ssh_access，非平台 IP）
ssh -p 22 admin@10.101.1.1 "display version"
```

### PNETLab（Linux VM）

1. 安装 PNETLab 到 Linux 虚拟机，导入设备镜像，确认网页入口可访问
2. **在 PNETLab 网页上人工预建 lab 拓扑**，按评测集 env.yaml 对齐设备名
3. 在每台设备上配置 SSH，与 env.yaml `ssh_access` 一致
4. 在 settings.yaml 配置后端（上例 `pnetlab-1` 形态）
5. 验证同上（`curl http://192.168.1.60 -o /dev/null -w "%{http_code}\n"` + doctor）

> 快照语义：预建模式下 snapshot/restore 为 no-op，reset 经设备 SSH 重放初始配置实现
> （DD-04 §5.7）。设备可达性由就绪探测（SSH 端口轮询 + 登录探测）把关。

## 升级

```bash
cd rsi_playground
git pull origin main
cd platform && uv sync --extra dev
# 表结构由 create_all 幂等补建（无 alembic）；重跑初始化安全（不覆盖既有 admin）：
uv run python -m rsi.init
# 回归确认
uv run pytest -q
```

## 附录：实战运维 Runbook（S11 真实环境沉淀，2026-11）

### A. 服务器重启后恢复

服务重启后以下状态**自动从磁盘水合**，无需人工重建：

- 用户（DB users 表）与运行记录（runs/judgments/llm_calls 等）
- 对象文件库：suts / evalsets / iterations 的 store 记录
  （`<RSI_HOME>/storage/object-store/`）
- Git 裸仓与 SUT 配置树、评测集冻结版本

验证顺序：

```bash
curl http://localhost:8000/api/v1/system/health        # 服务存活
# 登录取 token → GET /suts、/evalsets 确认对象在
cd platform && uv run python -m rsi.doctor            # 32 项自检
```

仅以下情况需人工操作：评测集内容更新（重新 `POST /evalsets:import`）；
SUT 配置树大改（重新提交配置版本）；SUT VM 重装（见 B）。

### B. SUT VM Docker/镜像重建

SUT Agent 镜像源在仓库 `deploy/sut-opencode/`（Dockerfile + entrypoint +
defaults：网络运维 skill + GLM 配置注入）。重建流程：

```bash
# 在能访问 Docker 的机器上（或直接在 VM 内）：
cd deploy/sut-opencode
ZAI_TOKEN=<bigmodel token> bash build.sh rsi/opencode-netops:1.0

# 远端 VM（经 SSH 推送源码后构建；VM 需开 tcp://0.0.0.0:2375 供平台驱动，
# 国内网络建议配 registry-mirrors 以拉取 node 基础镜像）
# 平台侧重启实例即可用新镜像（instance destroy → 批次重建）
```

> token 经 build-arg 注入，不落库不入 git；`defaults/opencode.json` 内为占位符。

### C. 设备勘察（survey）——env.yaml 的事实来源

新环境接入前，先对每台设备采集事实再编写 env.yaml（切忌凭记忆写拓扑）：

```text
display version            → 设备型号/软件版本（决定 devices[].type：
                             VRP8/CloudEngine 用 ce，S 系用 vrp）
display interface brief    → 接口清单与状态
display lldp neighbor brief → 邻接关系 → topology.links 布线
display current-configuration | include ^#|interface|description|ip address
                             → configs/*.cfg 初始配置底稿
```

勘察结论固化：设备名/IP 对应 `ssh_access`、LLD 邻接对应 `links`、
配置摘录存 `configs/`。注意带外管理口（如 GE1/0/10）禁止注入故障。

### D. 日常运维提示

- **SQLite 定期 checkpoint**：WAL 文件（`platform.db-wal`）长时间不回收会
  持续增长，低峰期执行 `sqlite3 platform.db 'PRAGMA wal_checkpoint(TRUNCATE);'`
- 轮转文件日志在 `<RSI_HOME>/logs/`（app.jsonl / error.jsonl，
  `GET /api/v1/system/logs` 可查）；stdout 重定向的 server.log 仅兜底
- `<RSI_HOME>/storage/labs/` 按批累积 lab 目录，过期批可整目录归档清理
