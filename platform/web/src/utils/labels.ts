// 用户可见文案的中文名映射与技术术语大小写规范（U1 中文 sweep）。
// 原则：内部实现词汇（英文键名/枚举）不直接暴露给使用者；
// 未覆盖的键保留原文回落。

// 技术术语大小写常量表（文案中出现的英文词统一写法）
const TECH_WORDS: Record<string, string> = {
  llm: 'LLM',
  git: 'Git',
  api: 'API',
  docker: 'Docker',
  ssh: 'SSH',
  cli: 'CLI',
  token: 'Token',
  tokens: 'Tokens',
  url: 'URL',
  id: 'ID',
  sut: 'Agent',
  suts: 'Agent',
};

// 将文案中的英文术语替换为规范大小写（按单词边界匹配）
export function techLabel(text: string): string {
  return text.replace(/\b[a-zA-Z]+\b/g, (w) => TECH_WORDS[w.toLowerCase()] ?? w);
}

// 外部依赖项中文名（DependencyPanel / Dashboard）
export const DEP_LABELS: Record<string, string> = {
  docker: 'Docker 引擎',
  lab: '实验环境设备',
  instances: 'Agent 实例',
  llm: 'LLM 服务',
  git: 'Git 代码库',
  db: '数据库',
  collector: '轨迹采集器',
  config_api: '外部配置 API',
};

export function depLabel(name: string): string {
  if (DEP_LABELS[name]) return DEP_LABELS[name]!;
  if (name.startsWith('docker:')) {
    return `Docker 引擎（${name.slice('docker:'.length)}）`;
  }
  if (name.startsWith('platform:')) {
    return `网络仿真平台（${name.slice('platform:'.length)}）`;
  }
  return name; // 未知名保留原文
}

// 外部依赖状态中文名
export function depStatusText(status: string | null | undefined): string {
  switch (status) {
    case 'ok':
    case 'up':
      return '在线';
    case 'down':
    case 'error':
      return '离线';
    case 'not_configured':
      return '未配置';
    default:
      return '未知';
  }
}

// 外部依赖失败/详情的中文描述（后端 checker 的 detail 原文映射）。
// full=true 表示整句替换（语义短语）；否则子串替换（保留路径/数量等上下文）。
const DEP_DETAIL_PHRASES: Array<{ re: RegExp; zh: string; full?: boolean }> = [
  { re: /^not_configured$/i, zh: '未配置', full: true },
  { re: /offline stub pool/i, zh: '离线桩（未配置执行主机）', full: true },
  { re: /offline mock handler/i, zh: '离线模拟（内置 Mock）', full: true },
  { re: /(\d+) active labs?/i, zh: '$1 个在用实验环境' },
  { re: /(\d+) running instances?/i, zh: '$1 个运行中 Agent 实例' },
  { re: /SELECT 1 ok/i, zh: '连接正常', full: true },
  { re: /checker error:\s*(\w+)/i, zh: '检查失败（$1）' },
  { re: /ConnectError|Connection refused|connection error/i, zh: '连接失败', full: true },
  { re: /timeout|ReadTimeout|TimeoutError|timed?\s*out/i, zh: '连接超时', full: true },
  { re: /UnsupportedProtocol/i, zh: '地址未配置', full: true },
  { re: /SSLError|certificate/i, zh: '证书错误', full: true },
  { re: /not writable/i, zh: '（不可写）' },
];

export function depDetailText(detail: string | null | undefined): string {
  if (!detail) return '';
  for (const { re, zh, full } of DEP_DETAIL_PHRASES) {
    if (re.test(detail)) return full ? zh : detail.replace(re, zh);
  }
  return detail; // 未覆盖的技术细节保留原文（灰字小号展示）
}

// 轨迹事件类型中文名（TraceTimeline Tag）
export const EVENT_TYPE_LABELS: Record<string, string> = {
  tool_call: '工具调用',
  tool_result: '工具返回',
  llm_request: '模型请求',
  llm_response: '模型响应',
  run_started: '运行开始',
  run_finalized: '运行结束',
  task_end: '任务结束',
};

export function eventTypeLabel(v: string): string {
  return EVENT_TYPE_LABELS[v] ?? v;
}

// 凭证类型中文名（CredsTab）
export const CRED_KIND_LABELS: Record<string, string> = {
  device_ssh: '设备 SSH',
  nms_api: '网管 API',
  llm_api: '模型 API',
  backend_ssh: '后端 SSH',
};

export function credKindLabel(v: string): string {
  return CRED_KIND_LABELS[v] ?? v;
}

// 审计操作中文名（AuditTab）：按 <对象>.<动作> 映射，未知保留原文
const AUDIT_ACTIONS: Record<string, string> = {
  'sut.create': '创建 Agent',
  'sut.update': '更新 Agent',
  'sut.delete': '删除 Agent',
  'sut.file_write': '写入 Agent 文件',
  'session.create': '创建会话',
  'session.control': '控制会话',
  'gate.decide': '门审批',
  'image.load': '导入镜像',
  'image.delete': '删除镜像',
  'user.create': '创建用户',
  'user.update': '更新用户',
  'auth.login': '登录',
};

export function auditActionLabel(v: string): string {
  return AUDIT_ACTIONS[v] ?? v;
}

// 会话运行模式中文名（SessionDetail Descriptions）
const MODE_LABELS: Record<string, string> = {
  local: '本地',
  remote: '远程',
};

export function modeLabel(v: string): string {
  return MODE_LABELS[v] ?? v;
}
