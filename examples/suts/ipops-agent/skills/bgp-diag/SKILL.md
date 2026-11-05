# BGP 故障排查技能

## 触发条件
当用户报告 BGP 邻居异常（Down/Idle/Active）时使用此技能。

## 排查步骤（必须按顺序执行）

### 第 1 步：检查接口状态
```
display interface brief
```
确认相关接口是否为 UP 状态。如果接口 DOWN，先排查物理层/链路层。

### 第 2 步：检查 BGP 邻居状态
```
display bgp peer
```
关注：
- State 列：Established=正常，Idle=未启动，Active=TCP 连接失败，OpenSent/OpenConfirm=协商中
- 如果 State=Idle，检查 BGP 是否被 shutdown
- 如果 State=Active，检查 TCP 179 端口连通性

### 第 3 步：检查 BGP 配置
```
display current-configuration configuration bgp
```
关注：
- peer AS 号是否正确
- peer 地址是否可达
- 是否有 route-policy import/export（策略误配是常见根因）

### 第 4 步：检查路由策略（如有）
```
display route-policy
```
确认策略的 if-match/apply 子句是否正确。

### 第 5 步：检查 IGP 底层
```
display ospf peer
display ip routing-table
```
确认底层 IGP 邻居正常，BGP peer 地址在路由表中可达。

## 常见根因

| 现象 | 可能根因 |
|------|---------|
| State=Active | TCP 179 不通：ACL 阻断 / 路由不可达 / AS 号不匹配 |
| State=Idle | BGP 被 shutdown / 没有配置 peer |
| Established 但路由不通 | route-policy 拒绝了路由 / next-hop 不可达 |
| 邻居频繁翻动 | keepalive/holdtime 超时 / 链路抖动 |
