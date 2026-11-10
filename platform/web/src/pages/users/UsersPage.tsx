// 用户管理页（U7a 菜单拆分）：admin 专属（user:manage）。
import { Space, Typography } from 'antd';
import { UsersTab } from '../settings/tabs/UsersTab';

export function UsersPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={4} style={{ margin: 0 }}>
        用户管理
      </Typography.Title>
      <UsersTab />
    </Space>
  );
}
