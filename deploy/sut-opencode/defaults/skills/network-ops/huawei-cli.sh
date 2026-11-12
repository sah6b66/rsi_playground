#!/usr/bin/env bash
# 用法: huawei-cli.sh <设备名> "display ..." | huawei-cli.sh <设备名> -c "line1" -c "line2"
set -euo pipefail
dev="$1"; shift
norm=$(echo "$dev" | tr 'a-z-' 'A-Z_')
host_var="DEV_${norm}_HOST"; port_var="DEV_${norm}_PORT"
user_var="DEV_${norm}_USER"; pass_var="DEV_${norm}_PASS"
for v in "$host_var" "$port_var" "$user_var" "$pass_var"; do
  [ -n "${!v:-}" ] || { echo "missing env $v" >&2; exit 2; }
done
if [ "${1:-}" = "-c" ]; then
  shift
  printf '%s\n' system-view "$@" return
else
  printf '%s\n' "$@"
fi | sshpass -p "${!pass_var}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    -p "${!port_var}" "${!user_var}@${!host_var}"
