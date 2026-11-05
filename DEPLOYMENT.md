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
│           │ SSH                                          │
│           ▼                                              │
│  ┌──────────────┐                                       │
│  │ ENSP Pro VM  │  仿真设备（R1, R2, ...）              │
│  │ (Windows)    │  10.101.1.0/24 网段                   │
│  └──────────────┘                                       │
└─────────────────────────────────────────────────────────┘
```

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
│         │ SSH                    │ SSH                          │
│         ▼                        ▼                              │
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
| 10.103.0.0/24 | GPU-01 lab 网段池 | carrier.hosts.gpu-01.subnets |
| 10.104.0.0/24 | GPU-02 lab 网段池 | carrier.hosts.gpu-02.subnets |

> **重要**：各网段必须全网唯一，部署前运行 `deploy/check-subnets.ps1` 校验。

### 节点间路由要求

- 主节点 ↔ 执行节点：SSH 可达（22 端口）
- 主节点 ↔ 远端 Docker：SSH 可达（Docker over SSH）
- SUT 容器 → 仿真设备：设备管理网段路由可达（22/830 端口）
- SUT 容器 → Collector：主节点 4317/4318 端口可达

## 多实例并行配置

### 本地 + 远端混合

```yaml
# settings.yaml
carrier:
  hosts:
    main:
      docker: local
      subnets: [10.100.0.0/16]
    gpu-01:
      docker: ssh://ops@192.168.1.100
      subnets: [10.101.0.0/16]
      max_instances: 10
    gpu-02:
      docker: ssh://ops@192.168.1.101
      subnets: [10.102.0.0/16]
      max_instances: 10
  max_instances_per_host: 10

sandbox:
  backends:
    ensp-pro-1:
      host: 192.168.1.50
      ssh_port: 22
      ssh_user: administrator
      subnets: [10.101.1.0/24]
    pnetlab-1:
      host: 192.168.1.60
      ssh_port: 22
      ssh_user: root
      subnets: [10.102.1.0/24]

orchestrator:
  max_parallel_batches: 2
```

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

## 仿真后端部署

### ENSP Pro（Windows VM）

1. 安装 ENSP Pro 到 Windows 虚拟机
2. 创建仿真设备（或准备 .topo 模板）
3. 配置 SSH 登录
4. 安装 host-agent（pg-enspctl）

```powershell
# 在 ENSP Pro 主机上验证 SSH 可达
ssh administrator@192.168.1.50 "echo connected"
```

### PNETLab（Linux VM）

1. 安装 PNETLab 到 Linux 虚拟机
2. 导入设备镜像
3. 配置 SSH 登录

```bash
# 验证
ssh root@192.168.1.60 "echo connected"
```

## 升级

```bash
cd rsi_playground
git pull origin main
cd platform && uv sync
# 数据库迁移自动执行（alembic）
uv run pytest tests/  # 确认全部通过
```
