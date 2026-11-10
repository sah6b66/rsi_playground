// 参数配置六 Tab（DD-14 §5.10，T7）：参数/日志/审计/凭证/安全边界/用户。
// 外层 RequirePerm(settings:write)（menu 已控）；admin 专属三个 Tab 级再守卫。
import { Tabs } from 'antd';
import type { TabsProps } from 'antd';
import { useUiStore } from '../../stores/ui';
import { can } from '../../utils/permissions';
import { ParamsTab } from './tabs/ParamsTab';
import { LogsTab } from './tabs/LogsTab';
import { AuditTab } from './tabs/AuditTab';
import { CredsTab } from './tabs/CredsTab';
import { SecurityTab } from './tabs/SecurityTab';
import { UsersTab } from './tabs/UsersTab';

export function SettingsPage() {
  const role = useUiStore((s) => s.user)?.role;
  const isAdmin = can(role, 'user:manage');

  const items: TabsProps['items'] = [
    { key: 'params', label: '参数', children: <ParamsTab /> },
    { key: 'logs', label: '日志', children: <LogsTab /> },
    // 审计/凭证/用户为 admin 专属（后端 user:manage/credential:manage 裁决）
    ...(isAdmin
      ? [
          { key: 'audit', label: '审计', children: <AuditTab /> },
          { key: 'creds', label: '凭证', children: <CredsTab /> },
        ]
      : []),
    { key: 'security', label: '安全边界', children: <SecurityTab /> },
    ...(isAdmin ? [{ key: 'users', label: '用户管理', children: <UsersTab /> }] : []),
  ];

  return <Tabs defaultActiveKey="params" items={items} destroyOnHidden />;
}
