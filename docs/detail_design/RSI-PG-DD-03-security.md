# RSI-PG-DD-03 security —— 认证 / RBAC / 凭证 / 审计 / 密钥卫生

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-03 |
| 版本 | v1.0 |
| 上游 | 设计 3.11、6.1~6.4；NFR1④/NFR8；ADR-14/19 |
| 交付切片 | S1（security 骨架） |

## 1. 追溯与范围

**职责**：JWT 认证与每请求 disabled 校验（ADR-19）、RBAC 依赖工厂、凭证加密存取与注入解析、凭证指纹登记（Redactor 联动）、审计装饰器与记录、密钥卫生自检与日志脱密兜底。

**不做什么**：不做网络隔离（carrier.network，DD-05）；不做 refresh token / mTLS（P2 评估）；不审计运行日志内容（audit_log 与 logs 分流）。

## 2. 文件与类清单

```
rsi/security/__init__.py
rsi/security/auth.py         # AuthService（JWT+argon2）
rsi/security/rbac.py         # RbacGuard：require(...) 依赖工厂 + 权限矩阵
rsi/security/credentials.py  # CredentialStore / CredentialResolver / Fingerprinter
rsi/security/audit.py        # AuditLog + @audited 装饰器
rsi/security/hygiene.py      # SecretHygiene（启动自检 + 日志脱密过滤器）
```

## 3. 数据结构

```python
Role = Literal["admin", "agent_engineer", "researcher", "approver", "service"]
Permission = Literal[                        # 与设计 6.1 矩阵一一对应
  "sut:manage", "evalset:manage", "profile:manage",
  "batch:trigger", "version:promote_rollback", "approval:decide",
  "credential:manage", "user:manage", "system:stop",
  "view:read"]                               # 轨迹/看板/实验记录（全角色）

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
  "admin": 全部,
  "agent_engineer": {sut:manage, evalset:manage, profile:manage, batch:trigger,
                     version:promote_rollback, view:read},
  "researcher": {view:read},
  "approver":  {view:read, approval:decide},
  "service":   {batch:trigger, view:read},
}

class Claims(BaseModel):
    sub: str            # user.id
    role: Role
    exp: int; iat: int; jti: str

class ResolvedCreds(BaseModel):            # CredentialResolver 产物（DD-05 消费）
    device_env: dict[str, dict[str, str]]  # 设备名 → {DEV_<NAME>_HOST/PORT/USER/PASS}
    nms_token: str | None = None           # NMS_API_TOKEN
    fingerprints: list[SecretFingerprint]  # 供 Redactor

class SecretFingerprint(BaseModel):
    sha8: str                    # sha256(secret)[:8]，入审计 detail
    patterns: list[str]          # [secret, base64(secret)] —— 精确匹配掩码用
```

## 4. 对外接口

```python
class AuthService:
    def __init__(self, secret_env="RSI_JWT_SECRET", expire_hours=12, session_fn): ...
    def hash_password(self, raw: str) -> str            # argon2-cffi PasswordHasher
    def verify_password(self, raw: str, hashed: str) -> bool
    def login(self, username: str, password: str) -> tuple[str, Claims]
        """查 users（username 唯一）→ argon2 校验 → 签发 HS256 JWT（exp=12h, jti=uuid4）
        失败统一 AuthError（不区分用户不存在/密码错误）；成功记 user.login 审计"""
    def verify(self, token: str) -> Claims              # jwt.decode（exp 校验）→ AuthError
    async def get_current_user(self, token, session) -> User
        """verify → 查 users.disabled（每请求一次本地索引查询，ADR-19）→ 禁用即 401"""

class require:                              # rbac.py：FastAPI 依赖工厂
    def __init__(self, *perms: Permission): ...
    async def __call__(self, user=Depends(get_current_user)) -> User:
        # 缺权限 → AuthError(403)；矩阵单元测试逐格断言（IN-008）

class CredentialStore:
    def __init__(self, fernet: Fernet, session_fn): ...  # 主密钥来自 RSI_MASTER_KEY
    async def put(self, name: str, kind: Literal["device_ssh","nms_api","llm_api","backend_ssh"],
                  secret: str) -> str        # 返回 id；重名 → RsiError（不覆盖，rotate 专用）
    async def resolve(self, name_or_ref: str) -> str
    async def rotate(self, cred_id: str, new_secret: str) -> None   # 审计 credential.rotate
    # 无 delete：仅经 user 管理停用（保证凭证可追溯）
    async def fingerprints_of(self, cred_id: str) -> SecretFingerprint

class CredentialResolver:
    async def resolve_env(self, env: EnvSpec, permission: str) -> ResolvedCreds
        """逐设备解析 ssh_access（明文 password 或 password_ref→Store）→
        生成环境变量名（设备名大写、非字母数字→'_'）：DEV_R1_HOST/PORT/USER/PASS
        permission=read-write 时附 DEV_RW=1（SUT 侧可感知）；nms deps→NMS_API_TOKEN"""

class AuditLog:
    async def record(self, actor: str, action: str, object_type: str, object_id: str,
                     detail: dict | None = None) -> None          # audit_log 插入
    def audited(self, action: str, object_type: str = "path_param"):
        """装饰器：包裹 api 写端点（DD-13 全量应用）
        actor=当前用户（Depends 注入）；object_id 取路径参数首段或 body.id；
        detail={请求体摘要(截断 2KB, 剔除 secret 字段), ip}"""

class SecretHygiene:
    def startup_check(self) -> None
        # RSI_MASTER_KEY / RSI_JWT_SECRET 存在且字节数≥32 → 否则 CRITICAL 日志+SystemExit
    def install_log_filter(self, registry: FingerprintRegistry) -> None
        # structlog processor：对已登记 patterns 做精确替换 '***'（防泄漏兜底，非主脱敏）
```

**审计 action 分类表**（设计 3.11.3 落码为常量）：

```python
AUDIT_ACTIONS = {  # <对象>.<动作>
 "user.login","user.create","user.update",
 "credential.put","credential.rotate",
 "sut.create","sut.update","sut.delete",
 "evalset.create","evalset.update","evalset.freeze","evalset.assign","evalset.import","evalset.export",
 "batch.start","batch.pause","batch.resume","batch.abort","batch.rerun",
 "run.re-judge",
 "version.snapshot","version.promote","version.rollback","version.files_edit",
 "hypothesis.submit","hypothesis.apply",
 "iteration.start","iteration.pause","iteration.resume","iteration.abort","iteration.delete",
 "gate.confirm_direction","gate.hypothesis","gate.review","gate.decision",
 "system.stop","system.stop_clear","system.settings_update",
 "approval.approve","approval.reject"}
```

## 5. 内部逻辑

### 5.1 请求鉴权流（每请求）

```
Authorization: Bearer <jwt>
 → verify()（签名+exp）→ Claims
 → SELECT disabled FROM users WHERE id=claims.sub   （本地索引查询，微秒级；ADR-19）
 → require(perms)：ROLE_PERMISSIONS[role] ⊇ perms ?
 → handler(…)，写操作经 @audited
```

### 5.2 凭证注入流（run_task 时，DD-12 调用）

```
env.ssh_access[dev].password_ref? ──是→ Store.resolve(ref)（Fernet 解密）
                                    └否→ password（明文，仅实验网）
→ ResolvedCreds.device_env[dev] = {DEV_<NAME>_HOST/PORT/USER/PASS}
→ Fingerprinter 登记 patterns（原值+base64）→ tracing Redactor（P1 消费）+ 日志过滤器
→ HostService 注入容器 env（不落盘、不进轨迹；FingerprintRegistry 进程内随 run 生命周期）
```

## 6. 依赖矩阵

- imports：pyjwt、argon2-cffi、cryptography（Fernet）、rsi/core、rsi/db、rsi/settings。
- DB 表：**写** audit_log、credentials（put/rotate）；**读写** users（login/disabled）。
- 被依赖：api（DD-13 全路由鉴权/审计）、carrier（ResolvedCreds）、tracing（FingerprintRegistry→Redactor，P1）、versioning（record）、decision（record）。

## 7. 测试映射与实施注记

**测试**：TU-024（Store 加解密 round-trip / password_ref 解析 / 指纹登记）、IN-008（RBAC 矩阵逐格 + 写操作进 audit_log）；建议新增：TU-037（AuthService：过期 token 401、disabled 即时生效、错误密码不泄漏原因）。

**实施注记**：① argon2-cffi 直接使用 `PasswordHasher`（不引 passlib，避免依赖面）；② JWT 用 pyjwt（`jwt.encode/decode(algorithms=["HS256"])`）；③ Fernet 密钥=`RSI_MASTER_KEY` 环境变量 base64 解码（32 字节）；④ `@audited` 用 functools.wraps + FastAPI 依赖注入取用户（非 request.state，保持可测）；⑤ audit_log 不老化（区别轨迹 90 天），随 NFR7 备份；⑥ 敏感字段剔除表：`{"password","secret","token","api_key","password_ref 解析值"}`（body 摘要时递归剔除）。
