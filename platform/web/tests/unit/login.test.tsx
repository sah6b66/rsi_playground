import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { LoginPage } from '../../src/pages/login/LoginPage';

test('登录成功存储 token 并跳转', async () => {
  localStorage.removeItem('rsi_token');
  render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
  await userEvent.type(screen.getByPlaceholderText('admin'), 'admin');
  await userEvent.type(screen.getByPlaceholderText('请输入密码'), 'rsi-admin');
  await userEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  await waitFor(() =>
    expect(localStorage.getItem('rsi_token')).toBe('test-token'),
  );
});

test('登录失败显示行内错误', async () => {
  localStorage.removeItem('rsi_token');
  render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
  await userEvent.type(screen.getByPlaceholderText('admin'), 'admin');
  await userEvent.type(screen.getByPlaceholderText('请输入密码'), 'wrong');
  await userEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  await waitFor(() =>
    expect(screen.getByText(/invalid credentials/)).toBeInTheDocument(),
  );
  expect(localStorage.getItem('rsi_token')).toBeNull();
});
