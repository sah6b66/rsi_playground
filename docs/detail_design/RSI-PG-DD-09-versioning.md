# RSI-PG-DD-09 versioning —— 版本管理

| 项目 | 内容 |
|------|------|
| 文档编号 | RSI-PG-DD-09 |
| 版本 | v1.0 |
| 上游 | 设计 3.8、2.2；FR8、FR7.6；ADR-2/15 |
| 交付切片 | S2（GitStore+仓库布局+冻结 hook） |

## 1. 追溯与范围

**职责**：GitStore 门面（快照/候选提交/晋升/回滚/diff/lineage/冻结校验）；裸仓+临时 worktree+常驻 staging（ADR-15）；pre-receive hook 安装与 freeze.manifest 双保险。

**不做什么**：不做 changeset 内容生成（optimizer.applier 产出，DD-11）；不做 merge 冲突解决（P0/P1 仅 fast-forward）。

## 2. 文件与类清单

```
rsi/versioning/__init__.py
rsi/versioning/gitstore.py     # GitStore 门面
rsi/versioning/worktrees.py    # _Worktrees（内部：临时/staging worktree 生命周期）
rsi/versioning/hooks.py        # _Hooks（pre-receive 脚本安装与更新）
rsi/versioning/freeze.py       # _Freeze（freeze.manifest 生成与校验）
rsi/versioning/changeset.py    # Changeset 构造/应用（文件级 create/edit/delete）
rsi/versioning/lineage.py      # 谱系查询（tag/branch 遍历→LineageGraph）
```

## 3. 数据结构

```python
class VersionId(str):                  # git commit sha（40 位 hex）
class Changeset(BaseModel):            # optimizer.applier 产出 → gitstore.commit_candidate 消费
    hypothesis_id: str
    base_version: VersionId
    files: list[FileChange]

class FileChange(BaseModel):
    path: str                          # suts/<sut_id>/ 内相对路径
    op: Literal["create", "edit", "delete"]
    content: str | None = None         # create/edit 全文；delete=None
    target: str                        # 优化项（TargetRegistry 键）
    declared: bool                     # 声明与实际 diff 一致性标记

class FreezeRecord(BaseModel):
    evalset_id: str; git_ref: str; content_hash: str; frozen_at: str

class LineageGraph(BaseModel):
    nodes: list[LineageNode]           # {version_id, label, kind, parent_ids, experiment_ids}
    edges: list[tuple[str, str]]       # (parent, child)
```

## 4. 对外接口

```python
class GitStore:
    def __init__(self, repo_path: Path, worktree_root: Path, audit: AuditLog): ...
    # 全部操作记 audit_log（actor=service 身份或用户名）

    # ── 版本操作（FR8）──
    def init(self) -> None
        """初始化裸仓（git init --bare）+ 安装 pre-receive hook + 创建空 staging worktree"""
    def export(self, version_id: VersionId, dest: Path) -> None
        """目标 commit 建临时 worktree → 复制文件树到 dest → 清理 worktree
        并发安全：临时目录用 new_id('wt') 命名（ULID 唯一）；只读操作无需锁"""
    def snapshot(self, sut_id: str) -> VersionId                    # FR6.5 强制快照
        """staging worktree 中 suts/<id>/ 子树 add+commit（路径过滤）
        → config_versions 登记 kind=baseline；无变更→返回当前基线 VersionId（幂等）"""
    def commit_candidate(self, base: VersionId, cs: Changeset,
                         hyp_id: str) -> VersionId
        """base commit 建临时 worktree → 逐文件应用 Changeset（create/edit/delete）
        → 路径域二次校验（TargetRegistry 正则+冻结域排除，双重校验与 applier 独立）
        → commit → 建分支 suts/<id>/cand/<hyp_id> → 登记 kind=candidate"""
    def promote(self, candidate: VersionId) -> None
        """校验 candidate 是当前 baseline 后代（fast-forward 条件）→ tag baseline/v<N+1> 前移
        → suts.baseline_version_id 更新 → staging worktree 同步 → audit version.promote
        非 fast-forward → NotFastForwardError"""
    def rollback(self, sut_id: str, version_id: VersionId) -> None
        """新建 tag baseline/v<N+1> 指向历史 commit（keep-all 不移旧 tag）→ 更新 DB+staging"""
    def diff(self, a: VersionId, b: VersionId) -> list[FileDiff]
    def lineage(self, sut_id: str) -> LineageGraph

    # ── 评估集冻结（FR3.3）──
    def freeze_evalset(self, evalset_id: str) -> FreezeRecord
        """三件套 canonical hash（排序后逐文件拼接）→ tag evalsets/<id>/freeze/v<N>
        → evalsets 表 git_ref/content_hash/frozen_at 更新"""
    def verify_freeze(self) -> bool
        """遍历 judge/freeze.manifest（文件→sha256 清单）与实际文件比对
        → 不一致→False+CRITICAL 日志；启动+每批次开跑前调用（FR7.6 双保险）"""

    # ── 并发控制 ──
    _write_lock: asyncio.Lock           # 全部变更操作（snapshot/commit/promote/rollback/freeze）经此锁串行
    # export/diff/lineage/verify_freeze 只读，无锁

    # ── 人工编辑（FR10.5 P0）──
    def list_files(self, sut_id: str, version: VersionId | None) -> FileTree
        """version=None 读 staging（工作区）；指定版本读临时 worktree"""
    def write_file(self, sut_id: str, path: str, content: str,
                   commit: bool, message: str) -> None
        """staging 写入；commit=True→路径校验（TargetRegistry）→commit→登记新版本"""
```

## 5. 内部逻辑

### 5.1 worktree 模型（ADR-15）

```
var/git/platform-repo.git/         # 裸仓（全部对象）
var/git/wt/staging/                # 常驻 staging（人工编辑+snapshot 的 add/commit 源）
var/git/wt/<op-id>/                # 临时 worktree（export/commit_candidate 用，try/finally 清理）
```

### 5.2 pre-receive hook 脚本

```bash
#!/bin/bash
# 安装于裸仓 hooks/pre-receive（_Hooks.init 时写入）
# 读取环境变量 GIT_AUTHOR_NAME 判断提交者身份
# 拒绝：
#   1. optimizer 身份写 judge/** / prompts/**
#   2. 任何身份删除 tag refs/tags/suts/*/baseline/* 或 evalsets/*/freeze/*
#   3. optimizer 身份写已冻结 evalsets/**（freeze tag 存在时）
```

### 5.3 并发场景（v2.14 ADR-24 多实例）

| 场景 | 处理 |
|---|---|
| 多批次同时 export 同一版本 | 只读，各自临时 worktree（ULID 目录唯一），无锁并行 |
| 多批次同时 snapshot / commit_candidate | `_write_lock` 串行（asyncio.Lock） |
| 人工编辑 staging 与自动 snapshot 并发 | `_write_lock` 串行 |
| promote 与 commit_candidate 并发 | `_write_lock` 串行 |

### 5.4 快照幂等

```
snapshot(sut_id):
  async with _write_lock:
    staging_dir = wt/staging/suts/<sut_id>/
    if no diff(staging_dir): return current_baseline_version
    commit → 新 VersionId → config_versions insert(kind=baseline) → return
```

## 6. 依赖矩阵

- imports：gitpython（GitPython）、rsi/core、rsi/security（AuditLog）、rsi/settings。
- 注入：GitStore←settings（repo/worktree 路径）+AuditLog。
- DB：**写** config_versions、evalsets（freeze 字段）、suts（baseline_version_id）；**读** 无（版本谱系在 Git tag/branch）。
- 文件系统：var/git/**（裸仓+worktrees）。
- 被依赖：carrier（DD-05 export）、optimizer（DD-11 snapshot/commit_candidate）、orchestrator（DD-12 promote/rollback）、api（DD-13 全版本路由）、judge（DD-08 verify_freeze）。

## 7. 测试映射与实施注记

**测试**：TU-016（snapshot→commit→promote→rollback→lineage 一致+verify_freeze 篡改检出）、fake_gitstore（内存版，语义等价加速单测）、EE-004（任意版本一键回滚并重跑）。

**实施注记**：① GitPython 的 `git.Repo(bare_path)` 操作裸仓；worktree 用 `git worktree add <path> <commit>` 子命令；② export 的复制用 `shutil.copytree`（排除 .git）；③ freeze.manifest 格式：`{"<相对路径>": "<sha256>", …}` JSON 文件，存放于 `judge/freeze.manifest`（Git 管理的冻结域自身）；④ hook 脚本变更需同步 update（init 时比对旧脚本 hash 不一致则覆写+审计）；⑤ P1 升 PG 时无 DDL 变化（Git 与 DB 独立），仅把 SQLite→PG 影响的 config_versions 等表迁移。
