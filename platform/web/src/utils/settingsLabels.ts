// 参数配置中文名映射（C2 修剪：仅覆盖后端 default_stores 暴露的用户级键）。
// 覆盖后端 default_stores（rsi/api/deps.py）暴露的全部键 +
// llm.providers.* 直配键（半脱敏编辑界面用，api_key/base_url）。
// def 为与代码缺省一致的静态快照（rsi/settings.py 各字段 default），
// 后端将来补充缺省值接口后可替换。
export type SettingScope = 'system' | 'agent' | 'session';

export interface SettingLabel {
  name: string;
  desc?: string;
  def?: string | number | boolean | null | unknown[];
  // hidden=true：不在参数页展示（内部键/演示占位键/内网不关注的费用预算）
  hidden?: boolean;
  // 适用层级：
  // system=平台级；agent=面向优化对象（网络Agent）的缺省；session=RSSI 会话级缺省
  scope?: SettingScope;
}

// 按前缀推导适用层级：优化相关（optimizer/归因/假设）面向优化对象
// → agent；裁判相关（judge）→ session；其余 → system。
export function settingScope(path: string): SettingScope {
  if (path.startsWith('optimizer.') || path.startsWith('attribution.') ||
      path.startsWith('hypothesis.')) {
    return 'agent';
  }
  if (path.startsWith('judge.') || path.startsWith('eval.')) return 'session';
  return 'system';
}

export const SETTINGS_LABELS: Record<string, SettingLabel> = {
  // ── 服务 / 数据库 ──
  'server.host': { name: '服务监听地址', desc: '平台 API 绑定的网络地址', def: '0.0.0.0' },
  'server.port': { name: '服务监听端口', desc: '平台 API 的 HTTP 端口', def: 8000 },
  'database.url': { name: '数据库连接串', desc: 'P0 为 SQLite，P1 可切换 PostgreSQL', def: 'sqlite:///platform.db' },
  // ── 模型服务（LLM）──
  'llm.providers.*.api_key': { name: 'API 密钥', desc: '直接填入模型服务密钥（内网环境，明文保存于配置文件）', def: '' },
  'llm.providers.*.base_url': { name: '服务地址', desc: '留空使用官方地址；兼容网关填网关地址', def: '' },
  'llm.defaults.*.model': { name: '缺省模型', desc: '该用途（裁判/归因/假设/评审）使用的模型', def: '' },
  'llm.defaults.*.temperature': { name: '采样温度', desc: '留空使用服务端缺省', def: null },
  'llm.defaults.judge.model': { name: '裁判模型（LLM 缺省）', desc: 'llm.defaults 段的裁判模型；judge.llm.model 为其页面别名', def: '' },
  'llm.defaults.judge.temperature': { name: '裁判采样温度', desc: '0 表示尽量确定性输出', def: 0 },
  'llm.defaults.attribution.model': { name: '归因模型', desc: '执行结果归因分析的模型', def: '' },
  'llm.defaults.attribution.temperature': { name: '归因采样温度', desc: '留空使用服务端缺省', def: null },
  'llm.defaults.hypothesis.model': { name: '假设生成模型', desc: '生成优化假设的模型', def: '' },
  'llm.defaults.hypothesis.temperature': { name: '假设采样温度', desc: '留空使用服务端缺省', def: null },
  'llm.defaults.critic.model': { name: '自动评审模型', desc: '自动评审变更方案的模型', def: '' },
  'llm.defaults.critic.temperature': { name: '评审采样温度', desc: '留空使用服务端缺省', def: null },
  'llm.cassette_mode': {
    name: '调用回放模式（高级）',
    desc: 'LLM 调用录制/回放模式（用于测试可复现）：off=直连真实模型；replay=回放已录制的调用（默认，离线测试）；record=录制真实调用供后续回放',
    def: 'replay',
  },
  'llm.max_concurrent_calls': { name: '模型并发上限', desc: '同时进行的模型调用数上限', def: 4 },
  // ── 评估集与网络准备 API（ADR-27）──
  'evalset_api.url': { name: '评估集在线 API 地址', desc: '留空=不启用 API 模式', def: '' },
  'network_prep_api.url': { name: '网络准备 API 地址', desc: 'API 模式必填', def: '' },
  'network_prep_api.timeout_s': { name: '超时秒数', desc: '准备/恢复调用超时（秒）', def: 120 },
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
  'orchestrator.budget.iteration_default.max_tokens': { name: '单会话 Token 预算', desc: '每个 RSI 会话缺省 Token 上限', def: 50_000_000 },
  'orchestrator.budget.iteration_default.max_wall_hours': { name: '单会话时长预算（小时）', desc: '每个 RSI 会话缺省墙上时长上限', def: 8 },
  'orchestrator.budget.iteration_default.max_cost_usd': { name: '单会话费用预算（美元）', desc: '每个 RSI 会话缺省费用上限', def: 200, hidden: true },
  'orchestrator.budget.global_.max_tokens': { name: '全局 Token 预算上限', desc: '全平台累计 Token 消耗上限', def: 100_000_000 },
  'orchestrator.budget.global_.max_wall_hours': { name: '全局时长预算（小时）', desc: '0 表示不限制', def: 0 },
  'orchestrator.budget.global_.max_cost_usd': { name: '全局费用预算（美元）', desc: '全平台累计费用上限', def: 400, hidden: true },
  // ── Agent 承载（carrier）缺省 ──
  'carrier.task_timeout_s': { name: '单任务超时（秒）', desc: 'Agent 单个任务的最长执行时间', def: 1800 },
  'carrier.max_instances_per_host': { name: '每主机实例上限', desc: '未单独配置的主机使用的缺省上限', def: 10 },
  // ── 网络仿真（sandbox）缺省 ──
  'sandbox.build_retry': { name: '环境构建重试次数', desc: '构建实验环境失败后的重试次数', def: 2 },
  'sandbox.ready_timeout_s': { name: '环境就绪超时（秒）', desc: '等待仿真环境就绪的最长时间', def: 600 },
  // ── 轨迹（用户级子集） ──
  'tracing.redact.enabled': { name: '敏感信息脱敏', desc: '入库前按规则抹除密钥等敏感内容', def: true },
  'tracing.archive.hot_days': { name: '热数据保留天数', desc: '超期轨迹转入冷归档', def: 90 },
  // ── 日志 ──
  'logging.level': { name: '日志级别', desc: 'debug/info/warning/error 四级', def: 'info' },
  'logging.retention_days': { name: '日志保留天数', desc: '超期日志自动清理', def: 30 },
  'logging.error_split': { name: '错误日志单列', desc: '错误级别日志单独落盘便于排查', def: true },
  // ── 可复现（种子） ──
  'seed.master': { name: '随机种子', desc: '保证评测可复现：相同种子与相同输入得到相同的采样与裁决顺序', def: 20261102 },
  // ── 运行时键（default_stores 暴露，无 Settings 字段） ──
  'budget.llm.daily': { name: '每日模型调用上限', desc: '单日模型调用量预算（仅运行时生效，不落盘）', def: 100 },
  'platform.static_item': { name: '静态配置示例项', desc: '不可在线修改的静态项示例', hidden: true },
};

// 分组 key → 中文名（ParamsTab 分组标题）
export const GROUP_LABELS: Record<string, string> = {
  llm: '模型服务（LLM）',
  judge: '裁判',
  optimizer: '优化与决策',
  decision: '优化与决策',
  orchestrator: '编排',
  carrier: '载体缺省',
  sandbox: '网络仿真缺省',
  tracing: '轨迹',
  logging: '日志',
  database: '数据库',
  server: '服务',
  seed: '调度与随机种子',
  scheduler: '调度与随机种子',
  other: '其他',
};

// 精确匹配 → 段级通配匹配（llm.defaults.*.model）→ 回落原 path
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
