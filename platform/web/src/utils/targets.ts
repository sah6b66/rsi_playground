// 优化目标白名单 / 冻结域共享常量（SecurityTab 与 Agent 详情「优化范围」
// 共用；清单来自代码级常量 rsi/optimizer/targets.py，后端未提供 registry
// 端点时的前端兜底）。
export interface TargetItem {
  key: string;
  path: string;
  desc: string;
}

// 可优化目标白名单（8 项）
export const TARGET_PATHS: TargetItem[] = [
  { key: 'system_prompt', path: 'suts/{id}/system-prompt(.md)?', desc: 'Agent 系统提示词' },
  { key: 'skills', path: 'suts/{id}/skills/.*', desc: 'Agent 技能包' },
  { key: 'scripts', path: 'suts/{id}/scripts/.*', desc: 'Agent 辅助脚本' },
  { key: 'mcp_config', path: 'suts/{id}/mcp\\.json', desc: 'MCP 服务器配置' },
  { key: 'mcp_src', path: 'suts/{id}/mcp-src/.*', desc: 'MCP 服务器源码' },
  { key: 'memory', path: 'suts/{id}/memory/.*', desc: 'Agent 记忆文件' },
  { key: 'knowledge', path: 'suts/{id}/knowledge/.*', desc: '知识库文件' },
  { key: 'model_config', path: 'suts/{id}/model\\.json', desc: '模型调用配置' },
];

// 冻结域（3 项）：评审/评测基础设施任何 changeset 不可触碰
export interface FrozenItem {
  path: string;
  desc: string;
}

export const FROZEN_PATHS: FrozenItem[] = [
  { path: 'judge/.*', desc: '评审器定义（裁判中立性保障）' },
  { path: 'prompts/.*', desc: '评审提示词（防提示注入自改）' },
  { path: 'evalsets/.*', desc: '评测集（跨轮可比性锚点）' },
];

// ── U7b per-agent 优化范围 ────────────────────────────────────────────────
// 域根（相对 Agent 配置树根；目录域带 /，文件域为精确文件名）
export const DOMAIN_ROOTS: Record<string, string> = {
  system_prompt: 'system-prompt.md',
  skills: 'skills/',
  scripts: 'scripts/',
  mcp_config: 'mcp.json',
  mcp_src: 'mcp-src/',
  memory: 'memory/',
  knowledge: 'knowledge/',
  model_config: 'model.json',
};

// 选择状态：undefined=未勾选；[]=整个域可优化；非空=域内细化路径模式
export type ScopeSelection = Record<string, string[] | undefined>;

// 细化模式 → 提交项：目录域自动补域根前缀与 / 后缀（bgp-diag/ → skills/bgp-diag/）
export function scopeFromSelection(sel: ScopeSelection): string[] {
  const items: string[] = [];
  for (const t of TARGET_PATHS) {
    const v = sel[t.key];
    if (v === undefined) continue;
    const root = DOMAIN_ROOTS[t.key] ?? '';
    if (!v.length) {
      items.push(root);
    } else {
      for (let p of v.map((s) => s.trim()).filter(Boolean)) {
        if (root.endsWith('/') && !p.endsWith('/')) p = `${p}/`;
        items.push(root.endsWith('/') ? `${root}${p}` : p);
      }
    }
  }
  return items;
}

// 提交项 → 选择状态（编辑回填）：按域根前缀归位，域根本身=整域勾选
export function selectionFromScope(
  scope: string[] | null | undefined,
): ScopeSelection {
  const sel: ScopeSelection = {};
  for (const item of scope ?? []) {
    for (const t of TARGET_PATHS) {
      const root = DOMAIN_ROOTS[t.key] ?? '';
      if (item === root) {
        sel[t.key] = [];
        break;
      }
      if (root.endsWith('/') && item.startsWith(root)) {
        const rest = item.slice(root.length);
        sel[t.key] = [...(sel[t.key] ?? []), rest];
        break;
      }
    }
  }
  return sel;
}
