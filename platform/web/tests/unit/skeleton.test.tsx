import { render, screen } from '@testing-library/react';
import { StatusBadge } from '../../src/components/StatusBadge';
import { can } from '../../src/utils/permissions';

test('StatusBadge 渲染状态文案', () => {
  render(<StatusBadge value="running" />);
  expect(screen.getByText('运行中')).toBeInTheDocument();
});

test('未知状态回落原值', () => {
  render(<StatusBadge value="mystery" />);
  expect(screen.getByText('mystery')).toBeInTheDocument();
});

test('权限矩阵：agent_engineer 可建会话，researcher 不可', () => {
  expect(can('agent_engineer', 'session:create')).toBe(true);
  expect(can('researcher', 'session:create')).toBe(false);
});

test('admin 可管理用户', () => {
  expect(can('admin', 'user:manage')).toBe(true);
  expect(can('agent_engineer', 'user:manage')).toBe(false);
});
