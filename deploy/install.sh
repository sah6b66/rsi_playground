#!/usr/bin/env bash
# RSI Playground 一键安装脚本
# 用法: bash install.sh [--offline DIR]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLATFORM_DIR="$PROJECT_ROOT/platform"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 前置检查 ──
log_info "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || log_error "Python 3 not found. Install Python 3.12+ first."
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log_info "  Python: $PYTHON_VERSION"

command -v uv >/dev/null 2>&1 || {
    log_warn "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || log_error "uv installation failed"
}
UV_VERSION=$(uv --version | cut -d' ' -f2)
log_info "  uv: $UV_VERSION"

command -v docker >/dev/null 2>&1 && {
    DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "not-running")
    log_info "  Docker: $DOCKER_VERSION"
} || log_warn "Docker not found (required for SUT containers)"

command -v git >/dev/null 2>&1 && log_info "  Git: $(git --version | cut -d' ' -f3)" || log_warn "Git not found"

# ── Python 3.12 ──
if [[ "$PYTHON_VERSION" != "3.12" && "$PYTHON_VERSION" != "3.13" ]]; then
    log_info "Installing Python 3.12 via uv..."
    uv python install 3.12
fi

# ── 安装依赖 ──
log_info "Installing dependencies..."
cd "$PLATFORM_DIR"

OFFLINE_DIR=""
[[ "${1:-}" == "--offline" ]] && OFFLINE_DIR="${2:-}"

if [[ -n "$OFFLINE_DIR" ]]; then
    log_info "  Offline mode: installing from $OFFLINE_DIR"
    uv sync --offline --find-links "$OFFLINE_DIR"
else
    uv sync
fi

log_info "  Dependencies installed: $(uv pip list 2>/dev/null | wc -l) packages"

# ── 验证 ──
log_info "Running test suite..."
uv run --no-sync pytest tests/ -q --tb=no 2>/dev/null | tail -1
TEST_RESULT=$?
[[ $TEST_RESULT -eq 0 ]] && log_info "  Tests: PASSED" || log_warn "  Some tests failed (check with: uv run pytest tests/ -v)"

# ── 配置文件 ──
SETTINGS_FILE="$PROJECT_ROOT/settings.yaml"
if [[ ! -f "$SETTINGS_FILE" ]]; then
    if [[ -f "$PROJECT_ROOT/examples/settings.yaml.example" ]]; then
        cp "$PROJECT_ROOT/examples/settings.yaml.example" "$SETTINGS_FILE"
        log_info "  Created settings.yaml from example (edit before first run!)"
    fi
fi

# ── 环境变量检查 ──
log_info "Checking required environment variables..."
[[ -z "${RSI_JWT_SECRET:-}" ]] && log_warn "  RSI_JWT_SECRET not set (generate: openssl rand -hex 32)"
[[ -z "${RSI_MASTER_KEY:-}" ]] && log_warn "  RSI_MASTER_KEY not set (generate: openssl rand -base64 32)"
[[ -z "${ANTHROPIC_API_KEY:-}" ]] && log_warn "  ANTHROPIC_API_KEY not set (required for LLM features)"

echo ""
log_info "=========================================="
log_info "  Installation complete!"
log_info "=========================================="
echo ""
echo "  Next steps:"
echo "  1. Edit settings.yaml (sandbox hosts, carrier subnets)"
echo "  2. Set environment variables:"
echo "     export RSI_JWT_SECRET=\$(openssl rand -hex 32)"
echo "     export RSI_MASTER_KEY=\$(openssl rand -base64 32)"
echo "     export ANTHROPIC_API_KEY=sk-ant-..."
echo "  3. Start the server:"
echo "     cd platform && uv run uvicorn rsi.api.app:create_app --factory --port 8000"
echo "  4. Open http://localhost:8000/api/v1/docs"
echo ""
