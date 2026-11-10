// 状态色/文案单源（DD-14 §6）——运行=processing 蓝 / 待审批=warning 橙 /
// 成功=success 绿 / 失败=error 红 / env_failed=purple / 未知=default 灰
import { stepLabel } from './labels';

export interface StatusMeta {
  label: string;
  color: string;
}

export const STATUS_META: Record<string, StatusMeta> = {
  running: { label: '运行中', color: 'processing' },
  paused_awaiting: { label: '待审批', color: 'warning' },
  paused: { label: '已暂停', color: 'warning' },
  finalized: { label: '成功', color: 'success' },
  succeeded: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  aborted: { label: '已终止', color: 'default' },
  env_failed: { label: '环境失败', color: 'purple' },
  created: { label: '已创建', color: 'default' },
};

// 后端 IterationState 真实态（rsi/core/enums.py）→ 中文文案/色（T4）。
// StatusBadge 优先查此表；聚合值（running/paused_awaiting/finalized 等）
// 回落到 STATUS_META 保持兼容。
export const ITER_STATE_META: Record<string, StatusMeta> = {
  attributing: { label: '归因中', color: 'processing' },
  hypothesizing: { label: '假设生成中', color: 'processing' },
  reviewing: { label: '预审中', color: 'processing' },
  applying: { label: '应用变更中', color: 'processing' },
  verifying: { label: '验证中', color: 'processing' },
  deciding: { label: '决策中', color: 'processing' },
  paused: { label: '已暂停待审批', color: 'warning' },
  aborted: { label: '已终止', color: 'default' },
  finished: { label: '已完成', color: 'success' },
};

export const statusMeta = (value: string): StatusMeta =>
  ITER_STATE_META[value] ??
  STATUS_META[value] ?? {
    // 与 utils/labels 的阶段映射同源，保证未知/阶段值全站中文一致
    label: stepLabel(value) ?? value,
    color: 'default',
  };

export const TOKEN_KEY = 'rsi_token';
export const USER_KEY = 'rsi_user'; // {username, role} JSON，随 token 一起清除
export const API_PREFIX = '/api/v1';
