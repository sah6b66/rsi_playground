# 安装指南

## 前置条件

### 必需

| 组件 | 版本 | 检查命令 | 说明 |
|------|------|---------|------|
| Python | ≥3.12 | `python --version` | 平台后端语言 |
| uv | ≥0.4 | `uv --version` | Python 包管理器（替代 pip） |
| Docker Desktop | ≥4.0 | `docker version` | 运行 SUT Agent 容器 |
| Git | ≥2.30 | `git --version` | 版本管理（GitStore 依赖） |

### 可选（按需）

| 组件 | 版本 | 用途 |
|------|------|------|
| ENSP Pro | 最新 | 华为 VRP 仿真后端（Windows VM） |
| PNETLab | 最新 | 多厂商仿真后端（Linux VM） |
| PostgreSQL | ≥16 | P1 阶段替代 SQLite |
| Node.js | ≥18 | pg-mcp-proxy（MCP 插桩代理） |

## 安装步骤

### 1. 获取代码

```bash
git clone https://github.com/sah6b66/rsi_playground.git
cd rsi_playground
```

### 2. 安装 Python 依赖

```bash
cd platform

# 安装 Python 3.12（如系统版本较低）
uv python install 3.12

# 创建虚拟环境并安装全部依赖
uv venv --python 3.12
uv sync

# 验证
uv run pytest tests/ --co -q | tail -1
# 预期输出：655 tests collected
```

### 3. 准备配置文件

```bash
# 复制样例配置
cp ../examples/settings.yaml.example ../settings.yaml

# 编辑配置（必须修改的项）
# 必改项列表：
#   sandbox.backends.ensp-pro-1.host   → ENSP Pro 主机 IP
#   sandbox.backends.pnetlab-1.host    → PNETLab 主机 IP（可选）
#   llm.providers.anthropic            → API Key 环境变量名
```

### 4. 设置环境变量

```bash
# Linux / macOS
export RSI_JWT_SECRET=$(openssl rand -hex 32)
export RSI_MASTER_KEY=$(openssl rand -base64 32)
export ANTHROPIC_API_KEY=sk-ant-your-key-here

# Windows (PowerShell)
$env:RSI_JWT_SECRET = -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
$env:RSI_MASTER_KEY = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Max 256 }))
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
```

> **安全提示**：密钥永远不要写入 settings.yaml 或代码仓库。建议使用 `.env` 文件（加入 .gitignore）或系统密钥管理服务。

### 5. 初始化数据库和 Git 仓库

```bash
# 首次启动时自动完成：
#   - SQLite 数据库创建（22 张表）
#   - Git 裸仓初始化
#   - 管理员账号种子（admin/admin，首次登录后修改）
uv run python -c "from rsi.api.app import create_app; app = create_app(); print('✓ App created')"
```

### 6. 启动服务

```bash
# 开发模式（热重载）
uv run uvicorn rsi.api.app:create_app --factory --reload --port 8000

# 生产模式
uv run gunicorn rsi.api.app:create_app --factory -w 4 -b 0.0.0.0:8000
```

### 7. 验证安装

```bash
# 健康检查
curl http://localhost:8000/api/v1/system/health

# 登录获取 Token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin"}'

# Swagger UI
open http://localhost:8000/api/v1/docs
```

## 常见安装问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `uv: command not found` | uv 未安装 | `pip install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python 3.12 不可用 | 系统版本低 | `uv python install 3.12` |
| Docker 连接失败 | Docker Desktop 未启动 | 启动 Docker Desktop，等待 `docker version` 成功 |
| Git 操作报错 | Git 未配置 | `git config --global user.name/email` |
| pytest 失败 | 依赖缺失 | `uv sync --dev` 安装开发依赖 |
| 端口冲突 | 8000 被占用 | 修改 settings.yaml 中 `server.port` |

## 离线安装

无外网环境下：

```bash
# 1. 在有网机器上导出离线包
uv export --format requirements-txt > requirements.txt
uv sync --no-dev
mkdir vendor && uv pip download -r requirements.txt -d vendor/

# 2. 拷贝到目标机器
scp -r platform/ vendor/ target:/opt/rsi_playground/

# 3. 在目标机器上离线安装
cd /opt/rsi_playground/platform
uv sync --offline --find-links ../vendor/
```
