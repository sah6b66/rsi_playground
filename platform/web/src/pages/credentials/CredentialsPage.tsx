// 凭证管理页（U7a 菜单拆分）：admin 专属（cred:manage）。
import { Space, Typography } from 'antd';
import { CredsTab } from '../settings/tabs/CredsTab';

export function CredentialsPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={4} style={{ margin: 0 }}>
        凭证管理
      </Typography.Title>
      <CredsTab />
    </Space>
  );
}
