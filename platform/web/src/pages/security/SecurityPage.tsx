// 安全边界页（U7a 菜单拆分）：只读展示，view:read。
import { Space, Typography } from 'antd';
import { SecurityTab } from '../settings/tabs/SecurityTab';

export function SecurityPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={4} style={{ margin: 0 }}>
        安全边界
      </Typography.Title>
      <SecurityTab />
    </Space>
  );
}
