---
name: network-ops
description: 华为 VRP 设备 SSH 运维技能——登录凭据取自 DEV_* 环境变量，统一脚本执行 display/system-view 命令
---

# 华为设备 SSH 运维

凭据环境变量命名：`DEV_$(echo <设备名> | tr 'a-z-' 'A-Z_')_HOST/_PORT/_USER/_PASS`。

单条命令：
    bash /agent-config/skills/network-ops/huawei-cli.sh <设备名> "display interface brief"

进入配置视图多行（逐条 -c）：
    bash /agent-config/skills/network-ops/huawei-cli.sh Leaf-01 -c "interface GE1/0/1" -c "description To-Spine-01"

常用诊断序列：display version / display interface brief / display lldp neighbor /
display ip routing-table / display current-configuration | include <关键字>
