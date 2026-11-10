// 参数配置中文名映射（U2 用户第 6 条）。
// 覆盖后端 default_stores（rsi/api/deps.py）暴露的全部键 +
// settings.py Settings 各字段（含 dict 段的 * 通配键，如 carrier.hosts.*.docker）。
// def 为与代码缺省一致的静态快照（rsi/settings.py 各字段 default），
// 后端将来补充缺省值接口后可替换。
export interface SettingLabel {
  name: string;
  desc?: string;
  def?: string | number | boolean | null | unknown[];
}

export const SETTINGS_LABELS: Record<string, SettingLabel> = {
  // ── 服务 / 数据库 / 代码库 ──
  'server.host': { name: '服务监听地址', desc: '平台 API 绑定的网络地址', def: '0.0.0.0' },
  'server.port': { name: '服务监听端口', desc: '平台 API 的 HTTP 端口', def: 8000 },
  'database.url': { name: '数据库连接串', desc: 'P0 为 SQLite，P1 可切换 PostgreSQL', def: 'sqlite:///platform.db' },
  'git.repo.path': { name: '代码库路径', desc: '存放全部版本变更的裸仓库位置', def: 'git/platform-repo.git' },
  'git.repo.bare': { name: '使用裸仓库', desc: '代码库以裸仓库形式存储', def: true },
  'git.worktrees': { name: '工作区目录', desc: '检出各版本文件的工作区根目录', def: 'git/wt' },
  'git.author.name': { name: '提交者名称', desc: '平台代提交变更时使用的 Git 身份', def: 'rsi-platform' },
  'git.author.email': { name: '提交者邮箱', desc: '平台代提交变更时使用的 Git 邮箱', def: 'rsi-platform@local' },
  // ── 存储 / 日志 ──
  'storage.traces_dir': { name: '轨迹目录', desc: '运行轨迹归档的存放位置', def: 'traces' },
  'storage.artifacts_dir': { name: '产物目录', desc: '运行产物的存放位置', def: 'artifacts' },
  'storage.labs_dir': { name: '实验环境目录', desc: '实验环境数据的存放位置', def: 'labs' },
  'storage.recordings_dir': { name: '录像目录', desc: '操作录像的存放位置', def: 'recordings' },
  'storage.stop_file': { name: '停止标记文件', desc: '出现该文件时全局预算熔断生效', def: 'STOP' },
  'logging.dir': { name: '日志目录', desc: '平台日志的存放位置', def: 'logs' },
  'logging.level': { name: '日志级别', desc: 'debug/info/warning/error 四级', def: 'info' },
  'logging.retention_days': { name: '日志保留天数', desc: '超期日志自动清理', def: 30 },
  'logging.error_split': { name: '错误日志单列', desc: '错误级别日志单独落盘便于排查', def: true },
  // ── 网络仿真（sandbox） ──
  'sandbox.build_retry': { name: '环境构建重试次数', desc: '构建实验环境失败后的重试次数', def: 2 },
  'sandbox.ready_timeout_s': { name: '环境就绪超时（秒）', desc: '等待仿真环境就绪的最长时间', def: 600 },
  'sandbox.backends.*.kind': { name: '平台类型', desc: 'ensp-pro / pnetlab / fake', def: 'fake' },
  'sandbox.backends.*.url': { name: '平台入口地址', desc: '仿真平台网页管理入口，用于在线探活', def: '' },
  'sandbox.backends.*.verify_tls': { name: '校验平台证书', desc: '自签名证书平台应保持关闭', def: false },
  'sandbox.backends.*.subnets': { name: '平台网段池', desc: '该平台实验室使用的子网池', def: [] },
  // ── Agent 承载（carrier） ──
  'carrier.task_timeout_s': { name: '单任务超时（秒）', desc: 'Agent 单个任务的最长执行时间', def: 1800 },
  'carrier.max_instances_per_host': { name: '每主机实例上限', desc: '未单独配置的主机使用的缺省上限', def: 10 },
  'carrier.hosts.*.docker': { name: 'Docker 端点', desc: 'local 为平台本机；远端填 SSH 连接串', def: 'local' },
  'carrier.hosts.*.subnets': { name: '主机网段池', desc: '分配给该主机上实验室网络的子网池', def: [] },
  'carrier.hosts.*.max_instances': { name: '该主机实例上限', desc: '留空使用全局缺省', def: null },
  'carrier.adapters.*.image_base': { name: '基础镜像', desc: '构建该类型 Agent 运行镜像的基础镜像', def: '' },
  // ── 插桩 / 轨迹 ──
  'instrumentor.policy.prefer': { name: '插桩方式优先级', desc: 'hooks → mcp_wrap → kb_proxy → source', def: [] },
  'instrumentor.policy.source_injection': { name: '允许源码插桩', desc: '其他方式不可用时的兜底手段', def: true },
  'instrumentor.policy.smoke_verify': { name: '插桩冒烟验证', desc: '插桩后先跑冒烟确认 Agent 可用', def: true },
  'tracing.collector.listen': { name: '采集监听地址', desc: '轨迹采集器绑定的网络地址', def: '0.0.0.0' },
  'tracing.collector.grpc': { name: 'OTLP gRPC 端口', desc: '轨迹上报 gRPC 端口', def: 4317 },
  'tracing.collector.http': { name: 'OTLP HTTP 端口', desc: '轨迹上报 HTTP 端口', def: 4318 },
  'tracing.redact.enabled': { name: '敏感信息脱敏', desc: '入库前按规则抹除密钥等敏感内容', def: true },
  'tracing.redact.rules_path': { name: '脱敏规则文件', desc: '脱敏规则的位置', def: 'redact-rules.yaml' },
  'tracing.archive.codec': { name: '归档压缩格式', desc: 'zstd / gzip / none', def: 'zstd' },
  'tracing.archive.hot_days': { name: '热数据保留天数', desc: '超期轨迹转入冷归档', def: 90 },
  'tracing.archive.keep_promoted_forever': { name: '永久保留晋级数据', desc: '已晋级轮次的轨迹不清理', def: true },
  // ── 裁判（judge） ──
  'judge.det_timeout_s': { name: '确定性评审超时（秒）', desc: '规则型评审单次执行的最长时间', def: 120 },
  'judge.llm_retry': { name: '模型评审重试次数', desc: '模型评审失败后的重试次数', def: 2 },
  'judge.pairwise_enabled': { name: '启用两两对比评审', desc: '同轮多方案时进行成对比较', def: true },
  'judge.llm.model': { name: '裁判模型', desc: '执行评审的模型名称，留空使用回放/规则兜底', def: '' },
  // ── 优化器 / 决策 ──
  'optimizer.critic_max_repair_rounds': { name: '评审修复轮数上限', desc: '自动评审发现问题后的最大修复轮数', def: 5 },
  'optimizer.sft.no_gain_rounds': { name: '无增益容忍轮数（SFT 准入）', desc: '连续无提升多少轮后允许整理训练数据', def: 5 },
  'optimizer.sft.min_samples': { name: 'SFT 最少样本数', desc: '整理训练数据所需的最低有效样本数', def: 50 },
  'decision.noise.k_repeats': { name: '噪声重复测量次数', desc: '标定基线噪声时的重复次数', def: 3 },
  'decision.noise.factor': { name: '噪声带倍数', desc: '95% 置信对应的 σ 倍数', def: 1.96 },
  'decision.alpha': { name: '显著性水平', desc: '统计检验的 α，越小越保守', def: 0.05 },
  'decision.no_gain_rounds_default': { name: '无增益容忍轮数', desc: '连续无提升多少轮后触发收敛判定', def: 5 },
  // ── 编排（orchestrator） ──
  'orchestrator.max_parallel_batches': { name: '最大并行批次数', desc: '同时运行的评测批次数上限', def: 2 },
  'orchestrator.checkpoint_interval_s': { name: '检查点间隔（秒）', desc: '断点续跑的存档频率', def: 5 },
  'orchestrator.health_monitor_interval_s': { name: '健康巡检间隔（秒）', desc: '巡检运行环境健康状态的周期', def: 30 },
  'orchestrator.budget.default_iteration.max_tokens': { name: '单会话 Token 预算', desc: '每个会话缺省 Token 上限', def: 50_000_000 },
  'orchestrator.budget.default_iteration.max_wall_hours': { name: '单会话时长预算（小时）', desc: '每个会话缺省墙上时长上限', def: 8 },
  'orchestrator.budget.default_iteration.max_cost_usd': { name: '单会话费用预算（美元）', desc: '每个会话缺省费用上限', def: 200 },
  'orchestrator.budget.global.max_tokens': { name: '全局 Token 预算上限', desc: '全平台累计 Token 消耗上限', def: 100_000_000 },
  'orchestrator.budget.global.max_wall_hours': { name: '全局时长预算（小时）', desc: '0 表示不限制', def: 0 },
  'orchestrator.budget.global.max_cost_usd': { name: '全局费用预算（美元）', desc: '全平台累计费用上限', def: 400 },
  // ── 模型服务（llm） ──
  'llm.providers.*.api_key_env': { name: 'API 密钥环境变量', desc: '从该环境变量读取模型服务密钥', def: '' },
  'llm.providers.*.base_url_env': { name: '服务地址环境变量', desc: '兼容服务（如 vLLM）的地址环境变量', def: null },
  'llm.defaults.*.model': { name: '缺省模型', desc: '该用途（裁判/归因/假设/评审）使用的模型', def: '' },
  'llm.defaults.*.temperature': { name: '采样温度', desc: '留空使用服务端缺省', def: null },
  'llm.cassette_mode': { name: '调用回放模式', desc: 'replay 回放录像 / record 录制 / off 真实调用', def: 'replay' },
  'llm.max_concurrent_calls': { name: '模型并发上限', desc: '同时进行的模型调用数上限', def: 4 },
  // ── 安全 / 调度 / 种子 ──
  'security.jwt_secret_env': { name: 'JWT 密钥环境变量', desc: '登录令牌签名密钥的读取位置', def: 'RSI_JWT_SECRET' },
  'security.master_key_env': { name: '主密钥环境变量', desc: '凭证加密主密钥的读取位置', def: 'RSI_MASTER_KEY' },
  'security.jwt_expire_hours': { name: '登录有效期（小时）', desc: '登录令牌的过期时间', def: 12 },
  'scheduler.kind': { name: '调度器类型', desc: 'asyncio 进程内调度 / rq 队列调度', def: 'asyncio' },
  'scheduler.redis_url': { name: 'Redis 地址', desc: '队列调度时使用的 Redis 连接串', def: null },
  'seed.master': { name: '全局随机种子', desc: '三段式种子推导的主种子', def: 20261102 },
  // ── 运行时键（default_stores 暴露，无 Settings 字段） ──
  'budget.llm.daily': { name: '每日模型调用上限', desc: '单日模型调用量预算（仅运行时生效，不落盘）', def: 100 },
  'platform.static_item': { name: '静态配置示例项', desc: '不可在线修改的静态项示例' },
};

// 分组 key → 中文名（ParamsTab 分组标题）
export const GROUP_LABELS: Record<string, string> = {
  server: '服务',
  database: '数据库',
  git: '代码库',
  storage: '存储',
  logging: '日志',
  sandbox: '网络仿真',
  carrier: 'Agent 承载',
  instrumentor: '插桩',
  tracing: '轨迹',
  judge: '裁判',
  optimizer: '优化器',
  decision: '决策',
  orchestrator: '编排',
  llm: '模型服务',
  security: '安全',
  scheduler: '调度',
  seed: '种子与平台',
  other: '其他',
};

// 精确匹配 → 段级通配匹配（carrier.hosts.*.docker）→ 回落原 path
export function settingLabel(path: string): SettingLabel {
  const exact = SETTINGS_LABELS[path];
  if (exact) return exact;
  const parts = path.split('.');
  const wildcard = Object.keys(SETTINGS_LABELS).find((key) => {
    const kp = key.split('.');
    return (
      kp.length === parts.length && kp.every((seg, i) => seg === '*' || seg === parts[i])
    );
  });
  return wildcard ? SETTINGS_LABELS[wildcard]! : { name: path };
}
