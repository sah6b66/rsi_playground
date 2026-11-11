#!/usr/bin/env bash
# RSI Playground one-shot installer
# Usage: bash install.sh [--offline DIR]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLATFORM_DIR="$PROJECT_ROOT/platform"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── args ──
OFFLINE_DIR=""
if [[ "${1:-}" == "--offline" ]]; then
    OFFLINE_DIR="${2:-}"
    [[ -n "$OFFLINE_DIR" ]] || log_error "--offline requires a directory argument"
elif [[ -n "${1:-}" ]]; then
    log_error "Unknown argument: $1 (usage: bash install.sh [--offline DIR])"
fi

# ── prerequisites ──
log_info "Checking prerequisites..."

# Defensive system-python probe: on Windows the WindowsApps python3 alias
# is a Store stub that exits non-zero; any usable interpreter is fine since
# uv manages the actual 3.12 toolchain (uv sync / uv python install).
SYS_PYTHON=""
PYTHON_VERSION=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if VER="$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"; then
            SYS_PYTHON="$cand"
            PYTHON_VERSION="$VER"
            break
        fi
    fi
done
if [[ -n "$SYS_PYTHON" ]]; then
    log_info "  Python: $PYTHON_VERSION ($SYS_PYTHON)"
else
    log_warn "No usable system python - uv will provision Python 3.12 itself"
fi

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
} || log_warn "Docker not found (only needed for real SUT containers; offline eval uses fakes)"

command -v git >/dev/null 2>&1 && log_info "  Git: $(git --version | cut -d' ' -f3)" || log_warn "Git not found"

command -v npm >/dev/null 2>&1 \
    && log_info "  npm: $(npm --version)" \
    || log_warn "npm not found (web frontend will be skipped -> API-only install)"

# ── Python 3.12 ──
if [[ -n "$PYTHON_VERSION" ]] && [[ "$PYTHON_VERSION" != 3.12* && "$PYTHON_VERSION" != 3.13* ]]; then
    log_info "Installing Python 3.12 via uv..."
    uv python install 3.12
fi

# ── dependencies ──
log_info "Installing dependencies..."
cd "$PLATFORM_DIR"

if [[ -n "$OFFLINE_DIR" ]]; then
    log_info "  Offline mode: installing from $OFFLINE_DIR"
    uv sync --extra dev --offline --find-links "$OFFLINE_DIR"
else
    # --extra dev: pytest & friends live in the "dev" extra; a bare exact
    # `uv sync` would REMOVE them and the test step below could not run.
    uv sync --extra dev
fi
log_info "  Dependencies installed: $(uv pip list 2>/dev/null | wc -l) packages"

# ── test suite (failures warn; they do not abort the install) ──
log_info "Running test suite (a few minutes, fully offline)..."
TEST_LOG="$(mktemp)"
if uv run --no-sync pytest tests/ -q --tb=no >"$TEST_LOG" 2>&1; then
    tail -1 "$TEST_LOG"
    log_info "  Tests: PASSED"
else
    tail -5 "$TEST_LOG"
    log_warn "  Some tests failed (rerun with: uv run pytest tests/ -v)"
fi
rm -f "$TEST_LOG"

# ── settings.yaml -> the location the runtime actually reads ──
# Settings loads <RSI_HOME>/settings.yaml; RSI_HOME defaults to ./var
# relative to the server CWD (platform/ when started as documented).
VAR_DIR="$PLATFORM_DIR/var"
mkdir -p "$VAR_DIR"
SETTINGS_FILE="$VAR_DIR/settings.yaml"
if [[ ! -f "$SETTINGS_FILE" && -f "$PROJECT_ROOT/examples/settings.yaml.example" ]]; then
    cp "$PROJECT_ROOT/examples/settings.yaml.example" "$SETTINGS_FILE"
    log_info "  Created platform/var/settings.yaml from example (edit before use)"
fi

# ── deployment secrets ──
log_info "Checking deployment env vars..."
[[ -z "${RSI_JWT_SECRET:-}" ]] && log_warn "  RSI_JWT_SECRET not set (generate: openssl rand -hex 32)"
[[ -z "${RSI_MASTER_KEY:-}" ]] && log_warn "  RSI_MASTER_KEY not set (generate: openssl rand -base64 32)"
# NOTE: LLM api keys are NOT env vars - configure them in
# platform/var/settings.yaml under llm.providers.*.api_key.

# ── first-boot init: tables + admin + git bare repo (idempotent) ──
log_info "Initializing database + admin account + git repo (python -m rsi.init)..."
if [[ -z "${RSI_ADMIN_PASSWORD:-}" ]]; then
    log_warn "  RSI_ADMIN_PASSWORD not set - a random admin password will be generated and printed once"
fi
RSI_HOME="$VAR_DIR" uv run --no-sync python -m rsi.init \
    || log_error "init failed - run manually: cd platform && uv run python -m rsi.init"

# ── web frontend (optional; served at / once built) ──
WEB_BUILT="no"
if command -v npm >/dev/null 2>&1; then
    log_info "Building web frontend (platform/web)..."
    if (cd "$PLATFORM_DIR/web" && npm install --no-fund --no-audit >/dev/null 2>&1 && npm run build >/dev/null 2>&1); then
        WEB_BUILT="yes"
        log_info "  Web UI built"
    else
        log_warn "  Web build failed - API still available at http://localhost:8000/docs"
    fi
fi

echo ""
log_info "=========================================="
log_info "  Installation complete!"
log_info "=========================================="
echo ""
echo "  Data root (RSI_HOME): $VAR_DIR"
echo "  Next steps:"
echo "    1. Edit platform/var/settings.yaml"
echo "       (llm.providers.*.api_key, carrier.hosts, sandbox.backends as needed)"
echo "    2. Set deployment secrets:"
echo "         export RSI_JWT_SECRET=\$(openssl rand -hex 32)"
echo "         export RSI_MASTER_KEY=\$(openssl rand -base64 32)"
echo "    3. Start the server:"
echo "         cd platform && uv run uvicorn rsi.api.app:create_app --factory --port 8000"
echo "    4. Open:"
echo "         API docs:  http://localhost:8000/docs"
if [[ "$WEB_BUILT" == "yes" ]]; then
echo "         Web UI:    http://localhost:8000/"
fi
echo "         Health:    http://localhost:8000/api/v1/system/health"
echo "       Login: admin + the password printed by 'python -m rsi.init' above"
echo "              (or your RSI_ADMIN_PASSWORD; manage accounts in Web UI -> 参数 -> 用户)"
echo "    5. Optional self-check: cd platform && uv run python -m rsi.doctor"
echo ""
