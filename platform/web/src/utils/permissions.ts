// 前端权限表 —— 与后端 platform/rsi/security/rbac.py 的 ROLE_PERMISSIONS 权威定义同构
// （菜单/路由守卫/按钮三处消费；后端始终为最终裁决，此处仅做 UI 显隐）。
// 注意：前端权限位（session:create 等）是后端权限位的 UI 投影，
// 语义对应关系见 DD-14 §4 权限矩阵。

export type Role = 'admin' | 'agent_engineer' | 'researcher' | 'approver' | 'service';

export const ROLES: Role[] = ['admin', 'agent_engineer', 'researcher', 'approver', 'service'];

export const ROLE_LABEL: Record<Role, string> = {
  admin: '管理员',
  agent_engineer: 'Agent 工程师',
  researcher: '研究员',
  approver: '审批人',
  service: '服务账号',
};

export const PERMS: Record<Role, string[]> = {
  admin: [
    'view:read', 'session:create', 'session:control', 'gate:decide', 'evalset:manage',
    'sut:manage', 'image:manage', 'sandbox:manage', 'env:manage', 'settings:write',
    'cred:manage', 'user:manage',
  ],
  agent_engineer: [
    'view:read', 'session:create', 'session:control', 'gate:decide',
    'evalset:manage', 'sut:manage',
  ],
  researcher: ['view:read'],
  approver: ['view:read', 'gate:decide'],
  service: ['view:read'],
};

export const can = (role: Role | undefined, perm: string): boolean =>
  !!role && PERMS[role].includes(perm);

export interface MenuItem {
  key: string;
  label: string;
  icon?: string;
  perm?: string;
}

export interface MenuGroup {
  title: string;
  items: MenuItem[];
}

// 菜单树（DD-14 §4 信息架构，U1 调整）：总览 / RSI 优化 / 资源管理 / 系统。
// U1：SUT 与 Docker 镜像合并为「网络Agent」（/suts 页内 Tab，镜像 Tab 需
// image:manage，无权显示 403）；强相关资源相邻（网络Agent→运行环境→网络仿真）。
export const MENU: MenuGroup[] = [
  {
    title: '总览',
    items: [{ key: '/', label: '运行总览', icon: 'dashboard', perm: 'view:read' }],
  },
  {
    title: 'RSI 优化',
    items: [{ key: '/sessions', label: '会话管理', icon: 'experiment', perm: 'view:read' }],
  },
  {
    title: '资源管理',
    items: [
      { key: '/suts', label: '网络Agent', icon: 'code', perm: 'view:read' },
      { key: '/environments', label: '运行环境', icon: 'desktop', perm: 'view:read' },
      // GET /sandbox/backends 仅需登录、GET /carrier/hosts 仅 view:read（低权限只读）
      // → 菜单留 view:read，写按钮在页内以对应 manage 权限门控。
      { key: '/sandboxes', label: '网络仿真', icon: 'cloud-server', perm: 'view:read' },
      { key: '/evalsets', label: '评测集', icon: 'database', perm: 'view:read' },
    ],
  },
  {
    title: '系统',
    // U7a：原 /settings 六 Tab 拆为五个直接子页（各自路由+权限）
    items: [
      { key: '/settings', label: '参数配置', icon: 'setting', perm: 'settings:write' },
      { key: '/logs', label: '日志与审计', icon: 'file-text', perm: 'view:read' },
      { key: '/credentials', label: '凭证管理', icon: 'key', perm: 'cred:manage' },
      { key: '/security', label: '安全边界', icon: 'safety', perm: 'view:read' },
      { key: '/users', label: '用户管理', icon: 'team', perm: 'user:manage' },
    ],
  },
];
