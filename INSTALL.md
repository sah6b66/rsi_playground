# 安装指南

## 前置条件

### 必需

| 组件 | 版本 | 检查命令 | 说明 |
|------|------|---------|------|
| uv | ≥0.4 | `uv --version` | Python 包管理器（自动托管 Python 3.12，无需预装） |
| Git | ≥2.30 | `git --version` | 版本管理（GitStore 依赖） |

### 可选（按需）

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | ≥3.12 | 系统解释器（缺失时 `uv python install 3.12` 自动托管） |
| Node.js + npm | ≥20 | WEB 前端构建（缺失则为 API-only 安装） |
| Docker Desktop | ≥4.0 | 真实 SUT Agent 容器（离线评测用 Fake，不需要） |
| ENSP Pro | 最新 | 华为 VRP 仿真后端（Windows VM，见 DEPLOYMENT.md） |
| PNETLab | 最新 | 多厂商仿真后端（Linux VM，见 DEPLOYMENT.md） |
| PostgreSQL | ≥16 | P1 阶段替代 SQLite |

## 安装步骤

### 1. 获取代码

```bash
git clone https://github.com/sah6b66/rsi_playground.git
cd rsi_playground
```

### 2. 安装 Python 依赖

```bash
cd platform

# 可选：确保 3.12 工具链（uv 也会按 pyproject 自动托管）
uv python install 3.12

# 安装全部依赖（含 pytest/ruff 等 dev extra——裸 `uv sync` 会移除它们）
uv sync --extra dev

# 验证（全离线，无需 Docker/网络/LLM）
uv run pytest tests/ --co -q | tail -1
```

### 3. 准备配置文件

运行时读取的数据根目录由 `RSI_HOME` 决定，**默认 `./var`（相对启动命令的 CWD）**——按下方方式从 `platform/` 启动时即 `platform/var/`：

```bash
# 在 platform/ 目录下执行
mkdir -p var
cp ../examples/settings.yaml.example var/settings.yaml
```

编辑 `var/settings.yaml`，常用项：

- `llm.providers.<name>.api_key` —— 模型密钥（**yaml 明文直配**，内网约定；不是环境变量）
- `llm.defaults.judge.model` —— 裁判模型名称
- `sandbox.backends.*` / `carrier.hosts.*` —— 仿真后端与容器宿主（推荐装好后经 WEB 参数页在线管理）

> **内部层键——settings-internal.yaml**：`carrier.adapters.*`（真实载体镜像基址）、
> `instrumentor.*`、`scheduler.*`、`security.*`、`git.*`、`tracing.collector.*` 等
> 运维级旋钮**只**从 `<RSI_HOME>/settings-internal.yaml` 读取（可用
> `RSI_INTERNAL_SETTINGS` 指定路径）——写在用户 settings.yaml 里的同名段会被
> **静默忽略**（无报错）。配置真实载体（OpenCode/Claude Code 镜像基址）前先建
> `var/settings-internal.yaml`。

> 提示：也可用 `RSI_HOME=/path/to/var` 指定任意数据根（DB/Git/轨迹/日志均在其下），
> 或用 `RSI_SETTINGS` 指定配置文件绝对路径。**放在仓库根的 settings.yaml 不会被读取。**

### 4. 设置环境变量（部署级密钥）

```bash
# Linux / macOS
export RSI_JWT_SECRET=$(openssl rand -hex 32)
export RSI_MASTER_KEY=$(openssl rand -base64 32)

# Windows (PowerShell)
$env:RSI_JWT_SECRET = -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
$env:RSI_MASTER_KEY = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Max 256 }))
```

> 未设置的降级行为：`RSI_JWT_SECRET` 缺失回落进程内固定值（token 可被伪造，仅限本机试验）；
> `RSI_MASTER_KEY` 缺失则每进程随机（凭证存储重启即失）。生产必须设置。
> 密钥永远不要写入 settings.yaml 或代码仓库。

### 5. 初始化（建表 + 管理员 + Git 裸仓，幂等）

```bash
# 在 platform/ 目录下执行；密码来源：--password > RSI_ADMIN_PASSWORD > 随机生成（打印一次）
uv run python -m rsi.init
# ✓ 数据库已就绪（22 张表）
# ✓ 管理员账号 admin 已就绪
# ✓ Git 裸仓已就绪
```

> 首次启动服务**不会**自动建表/种管理员——`python -m rsi.init` 是唯一入口，重复执行安全
> （不会覆盖既有管理员密码）。

### 6. 构建 WEB 前端（可选，推荐）

```bash
cd web
npm install
npm run build          # 产物 web/dist/，由后端在生产模式直接服务
```

（开发模式可跳过构建，用 `npm run dev` 起独立 5173 端口。）

### 7. 启动服务

```bash
# 在 platform/ 目录下执行
# 开发模式（热重载）
uv run uvicorn rsi.api.app:create_app --factory --reload --port 8000

# 生产模式（单进程即同时服务 API 与 WEB SPA）
uv run uvicorn rsi.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

### 8. 验证安装

```bash
# 健康检查
curl http://localhost:8000/api/v1/system/health

# 登录获取 Token（密码 = 初始化时设置/生成的 admin 密码）
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<你的密码>"}'

# Swagger UI（注意路径是 /docs）
open http://localhost:8000/docs

# WEB 界面（已构建前端时）
open http://localhost:8000/
```

也可运行自检：`uv run python -m rsi.doctor`。

## 一键安装

以上 2–7 步可由脚本代劳（依赖/测试/配置样例/init/前端构建，幂等）：

```bash
bash deploy/install.sh            # 或 deploy/install.sh --offline <离线包目录>
```

## 常见安装问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `uv: command not found` | uv 未安装 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| 改了 settings.yaml 不生效 | 文件位置不对 | 必须在 `<RSI_HOME>/settings.yaml`（默认 platform/var/，见步骤 3） |
| `pytest: command not found` / 依赖缺失 | 裸 `uv sync` 不含 dev | `uv sync --extra dev` |
| `python3` 命令异常退出（Windows） | WindowsApps Store 桩别名 | 无需系统 Python，uv 自管 3.12；install.sh 已防御 |
| 登录 401 | 未初始化或密码不对 | `uv run python -m rsi.init`（已有 admin 时不覆盖，可在 WEB 用户页改密） |
| Docker 连接失败 | Docker Desktop 未启动 | 仅真实 SUT 容器需要；离线评测用 Fake 不受影响 |
| Git 操作报错 | Git 未配置 | `git config --global user.name/email` |
| 端口冲突 | 8000 被占用 | 修改 settings.yaml 中 `server.port` |

## 离线安装

无外网环境下：

```bash
# 1. 在有网机器上导出并下载离线包（uv 没有 download 子命令，用 pip）
cd platform
uv export --format requirements-txt --all-extras > requirements.txt
python -m pip download -r requirements.txt -d ../vendor/

# 2. 拷贝到目标机器
scp -r platform/ ../vendor/ target:/opt/rsi_playground/

# 3. 在目标机器上离线安装
cd /opt/rsi_playground/platform
uv sync --extra dev --offline --find-links ../vendor/
# 之后同上：步骤 3 起照常执行
```
