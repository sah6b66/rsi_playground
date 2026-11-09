// 状态色/文案单源（DD-14 §6）——运行=processing 蓝 / 待审批=warning 橙 /
// 成功=success 绿 / 失败=error 红 / env_failed=purple / 未知=default 灰
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

export const statusMeta = (value: string): StatusMeta =>
  STATUS_META[value] ?? { label: value, color: 'default' };

export const TOKEN_KEY = 'rsi_token';
export const USER_KEY = 'rsi_user'; // {username, role} JSON，随 token 一起清除
export const API_PREFIX = '/api/v1';
