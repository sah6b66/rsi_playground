// 日志与审计页（U7a 菜单拆分）：Tab1 系统日志（排障）+ Tab2 操作审计（追责）。
import { Alert, Space, Tabs, Typography } from 'antd';
import { LogsTab } from '../settings/tabs/LogsTab';
import { AuditTab } from '../settings/tabs/AuditTab';

export function LogsPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={4} style={{ margin: 0 }}>
        日志与审计
      </Typography.Title>
      <Alert
        type="info"
        showIcon
        message="系统日志用于排障；操作审计记录全部写操作用于追责，两者相互独立。"
      />
      <Tabs
        defaultActiveKey="logs"
        destroyOnHidden
        items={[
          { key: 'logs', label: '系统日志', children: <LogsTab /> },
          { key: 'audit', label: '操作审计', children: <AuditTab /> },
        ]}
      />
    </Space>
  );
}
