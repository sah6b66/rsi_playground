#!/bin/sh
# /agent-config（平台 SUT config 版本注入点）→ opencode 配置桥接：
# - 挂载版 opencode.json / skills / AGENTS.md 优先，缺项回补镜像内置
# - 全局配置 /rsi-config/opencode/：provider 定义（镜像内置）+ skill 软链
# - 工程目录 /workspace/：opencode.json / AGENTS.md 软链到挂载版
set -e
SRC=/opt/rsi-defaults
AC=/agent-config
mkdir -p "$AC" /trace/wiretap /workspace

for f in opencode.json AGENTS.md; do
  [ -s "$AC/$f" ] || cp "$SRC/$f" "$AC/$f" 2>/dev/null || true
done
[ -d "$AC/skills" ] || cp -r "$SRC/skills" "$AC/skills"

CFG=/rsi-config/opencode
[ -f "$CFG/opencode.json" ] || cp "$SRC/opencode.json" "$CFG/opencode.json"
ln -sfn "$AC/skills" "$CFG/skill"

[ -e /workspace/opencode.json ] || ln -sf "$AC/opencode.json" /workspace/opencode.json
[ -e /workspace/AGENTS.md ] || ln -sf "$AC/AGENTS.md" /workspace/AGENTS.md

exec "$@"
