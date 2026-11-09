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

// 菜单树（DD-14 §4 信息架构）：总览 / RSI 优化 / 资源管理 / 系统
export const MENU: MenuGroup[] = [
  {
    title: '总览',
    items: [{ key: '/', label: 'Dashboard', icon: 'dashboard', perm: 'view:read' }],
  },
  {
    title: 'RSI 优化',
    items: [{ key: '/sessions', label: '会话管理', icon: 'experiment', perm: 'view:read' }],
  },
  {
    title: '资源管理',
    items: [
      { key: '/evalsets', label: '评测集', icon: 'database', perm: 'view:read' },
      { key: '/suts', label: 'SUT 管理', icon: 'code', perm: 'view:read' },
      { key: '/images', label: 'Docker 镜像', icon: 'appstore', perm: 'view:read' },
      { key: '/sandboxes', label: '仿真沙箱', icon: 'cloud-server', perm: 'view:read' },
      { key: '/environments', label: '运行环境', icon: 'desktop', perm: 'view:read' },
    ],
  },
  {
    title: '系统',
    items: [{ key: '/settings', label: '参数配置', icon: 'setting', perm: 'view:read' }],
  },
];
