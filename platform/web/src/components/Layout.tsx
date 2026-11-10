// 全局布局（DD-14 §4）：侧边栏按角色过滤的分组菜单 + 顶栏用户/角色/退出 + <Outlet/>
import { Avatar, Button, Layout as AntLayout, Menu, Tag } from 'antd';
import {
  AppstoreOutlined,
  CloudServerOutlined,
  CodeOutlined,
  DashboardOutlined,
  DesktopOutlined,
  ExperimentOutlined,
  DatabaseOutlined,
  LogoutOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { MENU } from '../utils/permissions';
import { can, ROLE_LABEL } from '../utils/permissions';
import { useUiStore } from '../stores/ui';
import { clearToken } from '../api/client';
import { USER_KEY } from '../utils/constants';

const ICONS: Record<string, React.ReactNode> = {
  dashboard: <DashboardOutlined />,
  experiment: <ExperimentOutlined />,
  database: <DatabaseOutlined />,
  code: <CodeOutlined />,
  appstore: <AppstoreOutlined />,
  'cloud-server': <CloudServerOutlined />,
  desktop: <DesktopOutlined />,
  setting: <SettingOutlined />,
};

export function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, sidebarCollapsed, toggleSidebar } = useUiStore();

  const logout = () => {
    clearToken();
    localStorage.removeItem(USER_KEY);
    useUiStore.getState().setUser(null);
    navigate('/login');
  };

  const items = MENU.map((group, gi) => ({
    key: `g${gi}`,
    type: 'group' as const,
    label: group.title,
    children: group.items
      .filter((item) => can(user?.role, item.perm ?? 'view:read'))
      .map((item) => ({
        key: item.key,
        icon: item.icon ? ICONS[item.icon] : undefined,
        label: item.label,
      })),
  })).filter((g) => g.children.length > 0);

  const selected = '/' + location.pathname.split('/').filter(Boolean)[0];

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <AntLayout.Sider
        collapsible
        collapsed={sidebarCollapsed}
        onCollapse={toggleSidebar}
        width={208}
      >
        <div
          style={{
            color: '#fff',
            padding: '16px 0',
            textAlign: 'center',
            fontWeight: 600,
          }}
        >
          网络Agent RSI演练场
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          items={items}
          onClick={({ key }) => navigate(key)}
        />
      </AntLayout.Sider>
      <AntLayout>
        <AntLayout.Header
          style={{
            background: '#fff',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 12,
            paddingInline: 24,
          }}
        >
          {user && (
            <>
              <Avatar icon={<UserOutlined />} />
              <span>{user.username}</span>
              <Tag color="blue">{ROLE_LABEL[user.role]}</Tag>
            </>
          )}
          <Button icon={<LogoutOutlined />} onClick={logout}>
            退出
          </Button>
        </AntLayout.Header>
        <AntLayout.Content style={{ margin: 16 }}>
          <Outlet />
        </AntLayout.Content>
      </AntLayout>
    </AntLayout>
  );
}
