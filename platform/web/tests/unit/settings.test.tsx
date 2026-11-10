// T7 参数六 Tab（计划 Task 7 Step 1①③：ParamsTab 动态编辑/静态锁、
// UsersTab 创建+禁用、SecurityTab 只读渲染）
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { SettingsPage } from '../../src/pages/settings/SettingsPage';
import { useUiStore } from '../../src/stores/ui';
import { resetT7Settings, resetT7Users, t7Requests } from '../msw/handlers';

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetT7Settings();
  resetT7Users();
  Object.values(t7Requests).forEach((arr) => {
    arr.length = 0;
  });
});

// ① ParamsTab：动态项可编辑保存（PUT body 值）→ 刷新后显示新值；
// 静态项锁标"静态"、无编辑控件
test('ParamsTab：动态编辑保存热生效 + 静态项锁', async () => {
  renderSettings();
  // U2：配置项以中文名渲染，内部 path 不作为单元格文本（在 tooltip 中）
  await screen.findByText('裁判模型');
  expect(screen.getByText('每日模型调用上限')).toBeInTheDocument();
  expect(screen.getByText('环境构建重试次数')).toBeInTheDocument();
  expect(screen.queryByText('budget.llm.daily')).not.toBeInTheDocument();
  // 缺省值列来自静态快照
  expect(screen.getByText('100')).toBeInTheDocument(); // budget.llm.daily 缺省
  // 可热更 / 需重启 Tag
  expect(screen.getByText('需重启')).toBeInTheDocument();
  expect(screen.getAllByText('可热更').length).toBeGreaterThanOrEqual(2);
  // 编辑动态项 budget.llm.daily（数值输入框）：清空 → 输入 200 → 失焦保存
  const num = screen.getByTestId('edit-budget.llm.daily');
  await userEvent.clear(num);
  await userEvent.type(num, '200');
  await userEvent.tab();
  await waitFor(() => expect(t7Requests.settingPut.length).toBe(1));
  const put = t7Requests.settingPut[0] as {
    path: string;
    body: { value: unknown };
  };
  expect(put.path).toBe('budget.llm.daily');
  expect(put.body.value).toBe(200);
  // invalidate 后编辑器重挂载显示新值 200（key=value 强制刷新）
  await waitFor(() => expect(num).toHaveValue('200'));
  // 数字型动态项共 5 个（4 个 config 重试/超时 + budget.llm.daily），静态项无输入框
  expect(screen.getAllByRole('spinbutton').length).toBe(5);
}, 20_000);

// ② UsersTab：创建 Modal（用户名/角色 5 选/密码 ≥8 位）→ POST body；
// 禁用 Switch → PUT {disabled:true}
test('UsersTab：创建用户 + 禁用 Switch', async () => {
  renderSettings();
  // 切到用户管理 Tab
  await userEvent.click(screen.getByRole('tab', { name: '用户管理' }));
  await screen.findByText('u-admin');
  expect(screen.getByText('u-eng-1')).toBeInTheDocument();
  // 创建 Modal
  await userEvent.click(screen.getByRole('button', { name: /创建用户/ }));
  await userEvent.type(
    screen.getByRole('textbox', { name: /用户名/ }),
    'eng-2',
  );
  await userEvent.click(screen.getByRole('combobox', { name: /角色/ }));
  // 角色下拉 5 选
  const roleOptions = await screen.findAllByText(
    /（admin|agent_engineer|researcher|approver|service）/,
    { selector: '.ant-select-item-option-content' },
  );
  expect(roleOptions.length).toBe(5);
  await userEvent.click(
    screen.getAllByText('审批人（approver）', {
      selector: '.ant-select-item-option-content',
    })[0]!,
  );
  await userEvent.type(screen.getByLabelText(/密码/), 'pass-12345');
  await userEvent.click(screen.getByRole('button', { name: /^创\s*建$/ }));
  await waitFor(() => expect(t7Requests.userCreate.length).toBe(1));
  expect(t7Requests.userCreate[0]).toEqual({
    username: 'eng-2',
    password: 'pass-12345',
    role: 'approver',
  });
  // 创建成功入表
  await screen.findByText('eng-2');
  // 禁用 Switch → PUT /users/u-eng-1 {disabled:true}
  const eng1Row = screen.getByText('u-eng-1').closest('tr') as HTMLElement;
  await userEvent.click(within(eng1Row).getByRole('switch'));
  await waitFor(() => expect(t7Requests.userUpdate.length).toBe(1));
  expect(t7Requests.userUpdate[0]).toEqual({
    id: 'u-eng-1',
    body: { disabled: true },
  });
}, 60_000);

// ③ SecurityTab 只读：白名单 8 项路径与冻结域 3 项渲染
test('SecurityTab：白名单 8 项 + 冻结域 3 项只读渲染', async () => {
  renderSettings();
  await userEvent.click(screen.getByRole('tab', { name: '安全边界' }));
  // 白名单 8 项（代码级常量兜底）
  await waitFor(() =>
    expect(screen.getByText('suts/{id}/skills/.*')).toBeInTheDocument(),
  );
  expect(screen.getByText('suts/{id}/mcp\\.json')).toBeInTheDocument();
  expect(screen.getByText('suts/{id}/model\\.json')).toBeInTheDocument();
  // 冻结域 3 项
  expect(screen.getByText('judge/.*')).toBeInTheDocument();
  expect(screen.getByText('prompts/.*')).toBeInTheDocument();
  expect(screen.getByText('evalsets/.*')).toBeInTheDocument();
  // 三层保障说明
  expect(screen.getByText(/三层保障/)).toBeInTheDocument();
  // 只读：激活面板内无任何按钮型写操作
  expect(within(screen.getByRole('tabpanel')).queryAllByRole('button')).toEqual([]);
});

// ④ UsersTab 重置密码：操作列按钮 → Modal（Input.Password ≥8 位）→ POST body
test('UsersTab：重置密码 Modal + POST body', async () => {
  renderSettings();
  await userEvent.click(screen.getByRole('tab', { name: '用户管理' }));
  await screen.findByText('u-admin');
  // 每行操作列都有"重置密码"按钮
  const eng1Row = screen.getByText('u-eng-1').closest('tr') as HTMLElement;
  await userEvent.click(within(eng1Row).getByRole('button', { name: '重置密码' }));
  // Modal：新密码 Input.Password（extra 至少 8 位）
  expect(screen.getByText(/至少 8 位/)).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(/新密码/), 'new-pass-9');
  await userEvent.click(screen.getByRole('button', { name: /^重\s*置$/ }));
  await waitFor(() => expect(t7Requests.userResetPassword.length).toBe(1));
  expect(t7Requests.userResetPassword[0]).toEqual({
    id: 'u-eng-1',
    password: 'new-pass-9',
  });
}, 60_000);
