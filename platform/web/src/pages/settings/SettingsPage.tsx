// 参数配置页（U7a 菜单拆分）：原 SettingsPage 六 Tab 中的「参数」Tab 独立成页。
// 路由 /settings，权限 settings:write（App.tsx RequirePerm 守卫）。
import { Space, Typography } from 'antd';
import { ParamsTab } from './tabs/ParamsTab';

export function SettingsPage() {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={4} style={{ margin: 0 }}>
        参数配置
      </Typography.Title>
      <ParamsTab />
    </Space>
  );
}
