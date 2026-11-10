// 登录页（DD-14 §5.1）：POST /auth/login → {token, expires_at, user_id, role}
import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch, setToken } from '../../api/client';
import { USER_KEY } from '../../utils/constants';
import type { Role } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

interface LoginResponse {
  token: string;
  expires_at: number;
  user_id: string;
  role: Role;
}

export function LoginPage() {
  const navigate = useNavigate();
  const setUser = useUiStore((s) => s.setUser);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const onFinish = async (values: { username: string; password: string }) => {
    setError(null);
    setLoading(true);
    try {
      const resp = await apiFetch<LoginResponse>(
        'POST',
        '/auth/login',
        values,
      );
      setToken(resp.token);
      localStorage.setItem(
        USER_KEY,
        JSON.stringify({ username: values.username, role: resp.role }),
      );
      setUser({ username: values.username, role: resp.role });
      navigate('/');
    } catch (e) {
      setError(e instanceof Error ? e.message : '登录失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <Card style={{ width: 380 }}>
        <Typography.Title level={3} style={{ textAlign: 'center' }}>
          网络Agent RSI演练场
        </Typography.Title>
        {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />}
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="admin" autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password placeholder="请输入密码" autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            登录
          </Button>
        </Form>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          初始账号由部署方配置，见 INSTALL.md
        </Typography.Text>
      </Card>
    </div>
  );
}
