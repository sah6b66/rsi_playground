/// <reference types="vite/client" />
// U2：settingsLabels 覆盖率（对照后端 default_stores 键快照）+
// 用户可见文案不得含内部文档编号/内部路径（纯数据检查）+
// 关键页面源码非注释区无文档编号残留（?raw 扫描，排除 // 注释行）。
import { describe, expect, test } from 'vitest';
import {
  SETTINGS_LABELS,
  GROUP_LABELS,
  settingLabel,
} from '../../src/utils/settingsLabels';
import { LEVEL_LABELS } from '../../src/pages/settings/tabs/LogsTab';

// 后端 rsi/api/deps.py default_stores["settings"] 的键清单快照
// （后端增删键时此测试失败，提醒同步前端中文名）。
const BACKEND_SETTING_KEYS = [
  'sandbox.build_retry',
  'sandbox.ready_timeout_s',
  'judge.det_timeout_s',
  'judge.llm_retry',
  'logging.level',
  'judge.llm.model',
  'budget.llm.daily',
  'platform.static_item',
];

test('SETTINGS_LABELS 覆盖后端 default_stores 全部键', () => {
  BACKEND_SETTING_KEYS.forEach((k) => {
    const label = SETTINGS_LABELS[k];
    expect(label, `缺少键 ${k} 的中文名`).toBeDefined();
    expect(label!.name).not.toBe(k); // 必须是中文名而非回落
  });
});

test('settingLabel：精确 → 通配 → 原文回落', () => {
  expect(settingLabel('judge.llm.model').name).toBe('裁判模型');
  expect(settingLabel('carrier.hosts.main.docker').name).toBe('Docker 端点');
  expect(settingLabel('sandbox.backends.x.verify_tls').name).toBe('校验平台证书');
  expect(settingLabel('totally.unknown.key').name).toBe('totally.unknown.key');
});

test('分组中文映射覆盖 ParamsTab 全部分组', () => {
  [
    'server', 'database', 'git', 'storage', 'logging', 'sandbox', 'carrier',
    'instrumentor', 'tracing', 'judge', 'optimizer', 'decision',
    'orchestrator', 'llm', 'security', 'scheduler', 'seed', 'other',
  ].forEach((g) => expect(GROUP_LABELS[g], `分组 ${g} 缺中文名`).toBeDefined());
});

// 用户第 5 条：文案不得出现内部实现信息（文档编号/内部路径）
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
