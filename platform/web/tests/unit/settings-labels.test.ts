/// <reference types="vite/client" />
// C2：settingsLabels 覆盖率（对照后端 default_stores 用户级键快照）+
// 用户可见文案不得含内部文档编号/内部路径（纯数据检查）+
// 关键页面源码非注释区无文档编号残留（?raw 扫描，排除 // 注释行）。
import { describe, expect, test } from 'vitest';
import {
  SETTINGS_LABELS,
  GROUP_LABELS,
  settingLabel,
  settingScope,
} from '../../src/utils/settingsLabels';
import { LEVEL_LABELS } from '../../src/pages/settings/tabs/LogsTab';

// 后端 rsi/api/deps.py default_stores["settings"] 的键清单快照
// （后端增删键时此测试失败，提醒同步前端中文名）。
// C1 后 45 项：42 个 Settings 用户级键 + judge.llm.model 别名 +
// budget.llm.daily / platform.static_item 运行时键。
const BACKEND_SETTING_KEYS = [
  'budget.llm.daily',
  'carrier.max_instances_per_host',
  'carrier.task_timeout_s',
  'database.url',
  'decision.alpha',
  'decision.no_gain_rounds_default',
  'decision.noise.factor',
  'decision.noise.k_repeats',
  'judge.det_timeout_s',
  'judge.llm.model',
  'judge.llm_retry',
  'judge.pairwise_enabled',
  'llm.cassette_mode',
  'llm.defaults.attribution.model',
  'llm.defaults.attribution.temperature',
  'llm.defaults.critic.model',
  'llm.defaults.critic.temperature',
  'llm.defaults.hypothesis.model',
  'llm.defaults.hypothesis.temperature',
  'llm.defaults.judge.model',
  'llm.defaults.judge.temperature',
  'llm.max_concurrent_calls',
  'logging.error_split',
  'logging.level',
  'logging.retention_days',
  'optimizer.critic_max_repair_rounds',
  'optimizer.sft.min_samples',
  'optimizer.sft.no_gain_rounds',
  'orchestrator.budget.global_.max_cost_usd',
  'orchestrator.budget.global_.max_tokens',
  'orchestrator.budget.global_.max_wall_hours',
  'orchestrator.budget.iteration_default.max_cost_usd',
  'orchestrator.budget.iteration_default.max_tokens',
  'orchestrator.budget.iteration_default.max_wall_hours',
  'orchestrator.checkpoint_interval_s',
  'orchestrator.health_monitor_interval_s',
  'orchestrator.max_parallel_batches',
  'platform.static_item',
  'sandbox.build_retry',
  'sandbox.ready_timeout_s',
  'seed.master',
  'server.host',
  'server.port',
  'tracing.archive.hot_days',
  'tracing.redact.enabled',
];

test('SETTINGS_LABELS 覆盖后端 default_stores 全部键（45 项快照）', () => {
  expect(BACKEND_SETTING_KEYS.length).toBe(45);
  BACKEND_SETTING_KEYS.forEach((k) => {
    const label = SETTINGS_LABELS[k];
    expect(label, `缺少键 ${k} 的中文名`).toBeDefined();
    expect(label!.name).not.toBe(k); // 必须是中文名而非回落
  });
});

test('被移除键的 label 已删除（storage/git/security/instrumentor/scheduler 等）', () => {
  [
    'storage.traces_dir',
    'logging.dir',
    'git.repo.path',
    'security.jwt_secret_env',
    'instrumentor.policy.prefer',
    'scheduler.kind',
    'carrier.hosts.*.docker',
    'sandbox.backends.*.kind',
    'carrier.adapters.*.image_base',
    'llm.providers.*.api_key_env',
    'llm.providers.*.base_url_env',
    'orchestrator.budget.default_iteration.max_tokens',
    'orchestrator.budget.global.max_tokens',
  ].forEach((k) => expect(SETTINGS_LABELS[k], `应删除键 ${k}`).toBeUndefined());
});

test('settingLabel：精确 → 通配 → 原文回落', () => {
  expect(settingLabel('judge.llm.model').name).toBe('裁判模型');
  expect(settingLabel('llm.defaults.reviewer.model').name).toBe('缺省模型');
  expect(settingLabel('totally.unknown.key').name).toBe('totally.unknown.key');
});

test('分组中文映射覆盖 ParamsTab 全部分组', () => {
  [
    'llm', 'judge', 'optimizer', 'decision', 'orchestrator', 'carrier',
    'sandbox', 'tracing', 'logging', 'database', 'server', 'seed', 'other',
  ].forEach((g) => expect(GROUP_LABELS[g], `分组 ${g} 缺中文名`).toBeDefined());
  // 被移除分组不再需要中文名
  ['git', 'storage', 'instrumentor', 'security'].forEach((g) =>
    expect(GROUP_LABELS[g], `分组 ${g} 应已移除`).toBeUndefined(),
  );
});

// 用户可见文案不得出现内部实现信息（文档编号/内部路径）
const DOC_REF = /DD-\d|ADR-\d|FR\d|TU-\d|IN-\d|§|NFR|rsi\//;
const labelTexts: string[] = [];
for (const [path, l] of Object.entries(SETTINGS_LABELS)) {
  labelTexts.push(l.name, l.desc ?? '');
  expect(path, '键本身为内部标识，允许').toBeTruthy();
}
Object.values(GROUP_LABELS).forEach((v) => labelTexts.push(v));
Object.values(LEVEL_LABELS).forEach((v) => labelTexts.push(v));

test('labels 全部文案不含内部文档编号/内部路径', () => {
  labelTexts.forEach((t) => expect(t).not.toMatch(DOC_REF));
});

// 关键表单页面源码扫描：剔除 // 注释行后不得残留文档编号
// （tooltip/extra/placeholder 属字符串字面量，与注释同行的场景可忽略）
const pageSources = import.meta.glob('../../src/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

test('全部页面源码非注释区无 DD-/ADR-/§ 残留', () => {
  const offenders: string[] = [];
  for (const [file, src] of Object.entries(pageSources)) {
    const code = src
      .split('\n')
      .filter((line) => !line.trim().startsWith('//'))
      .join('\n');
    if (DOC_REF.test(code)) offenders.push(file);
  }
  expect(offenders, `以下文件字符串中残留文档编号：${offenders.join(', ')}`).toEqual([]);
});

// ── C2：settings.yaml.example 用户级键覆盖（抽样） ──
test('SETTINGS_LABELS 覆盖 settings.yaml.example 关键键（抽样）', () => {
  [
    'llm.providers.*.api_key',
    'llm.providers.*.base_url',
    'llm.defaults.*.model',
    'llm.defaults.*.temperature',
    'llm.cassette_mode',
    'llm.max_concurrent_calls',
    'database.url',
    'orchestrator.budget.iteration_default.max_tokens',
    'orchestrator.budget.global_.max_tokens',
    'seed.master',
  ].forEach((k) =>
    expect(SETTINGS_LABELS[k], `缺少键 ${k}`).toBeDefined(),
  );
});

test('seed 键名与组名符合第 4 轮反馈', () => {
  expect(SETTINGS_LABELS['seed.master']!.name).toBe('随机种子');
  expect(SETTINGS_LABELS['seed.master']!.desc).toContain('可复现');
  expect(GROUP_LABELS.seed).toBe('调度与随机种子');
});

test('settingScope：judge=session / optimizer=agent / 其余 system', () => {
  expect(settingScope('judge.llm.model')).toBe('session');
  expect(settingScope('judge.llm_retry')).toBe('session');
  expect(settingScope('optimizer.sft.min_samples')).toBe('agent');
  expect(settingScope('server.port')).toBe('system');
  expect(settingScope('llm.max_concurrent_calls')).toBe('system');
});
