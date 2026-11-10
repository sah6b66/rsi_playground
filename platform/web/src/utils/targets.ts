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
