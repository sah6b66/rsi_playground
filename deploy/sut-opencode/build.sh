#!/usr/bin/env bash
# 构建 SUT 镜像（在能访问 docker 的机器上运行；token 经环境变量注入，不入库）
# 用法: ZAI_TOKEN=<bigmodel token> bash build.sh [image_tag]
set -euo pipefail
TAG="${1:-rsi/opencode-netops:1.0}"
: "${ZAI_TOKEN:?need ZAI_TOKEN env (bigmodel API token)}"
docker build --build-arg ZAI_TOKEN="$ZAI_TOKEN" -t "$TAG" "$(dirname "$0")"
