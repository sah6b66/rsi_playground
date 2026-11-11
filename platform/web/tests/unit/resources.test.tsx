// T6 资源四页（计划 Task 6 Step 3 四组断言）
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ImagesPage } from '../../src/pages/images/ImagesPage';
import { EnvironmentsPage } from '../../src/pages/environments/EnvironmentsPage';
import { SandboxesPage } from '../../src/pages/sandboxes/SandboxesPage';
import { EvalsetListPage } from '../../src/pages/evalsets/EvalsetListPage';
import { useUiStore } from '../../src/stores/ui';
import {
  resetEvalsetFixtures,
  resourceRequests,
} from '../msw/handlers';

function renderApp(route: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/images" element={<ImagesPage />} />
          <Route path="/environments" element={<EnvironmentsPage />} />
          <Route path="/sandboxes" element={<SandboxesPage />} />
          <Route path="/evalsets" element={<EvalsetListPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetEvalsetFixtures();
  Object.values(resourceRequests).forEach((arr) => {
    arr.length = 0;
  });
});

// ① 镜像页：按主机分组 + 路径导入 Modal → POST 202 → 刷新；被引用删除禁用
test('镜像页：主机分组 + 路径导入 + 被引用禁用', async () => {
  renderApp('/images');
  await waitFor(() =>
    expect(screen.getByText('ipops-agent:v1.2')).toBeInTheDocument(),
  );
  // 按主机分组（Collapse 头）
  expect(screen.getByText('main')).toBeInTheDocument();
  expect(screen.getByText('remote-1')).toBeInTheDocument();
  expect(screen.getByText('remote-2')).toBeInTheDocument(); // 离线主机提示
  // 被引用 SUT Tag
  expect(screen.getByText('sut-1')).toBeInTheDocument();
  // 被引用镜像的删除为禁用（not-allowed + tooltip 文案）
  const delBtn = screen.getByRole('button', { name: '删除 ipops-agent:v1.2' });
  expect(delBtn).toHaveStyle({ cursor: 'not-allowed' });
  // 未引用镜像可删除
  expect(
    screen.getByRole('button', { name: '删除 busybox:latest' }),
  ).not.toHaveStyle({ cursor: 'not-allowed' });
  // 路径导入 Modal
  await userEvent.click(screen.getByRole('button', { name: '路径导入' }));
  await userEvent.click(screen.getByRole('combobox', { name: /执行主机/ }));
  await userEvent.click(
    await screen.findByText('main', { selector: '.ant-select-item-option-content' }),
  );
  await userEvent.type(
    screen.getByRole('textbox', { name: /tar 路径/ }),
    '/home/images/ipops-agent.tar',
  );
  await userEvent.click(screen.getByRole('button', { name: /^导\s*入$/ }));
  await waitFor(() =>
    expect(resourceRequests.imageLoad.length).toBe(1),
  );
  expect(resourceRequests.imageLoad[0]).toEqual({
    host: 'main',
    tar_path: '/home/images/ipops-agent.tar',
  });
  // 202 成功后 Modal 关闭
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: /^导\s*入$/ })).not.toBeInTheDocument(),
  );
}, 60000);

// ② 环境页：local/remote 徽章 + 在线灯 + 编辑 → PUT → 重启生效 Alert
test('环境页：模式徽章 + 在线灯 + 编辑 PUT + 重启提示', async () => {
  renderApp('/environments');
  await waitFor(() => expect(screen.getByText('remote-1')).toBeInTheDocument());
  expect(screen.getByText('远端')).toBeInTheDocument();
  expect(screen.getByText('本地')).toBeInTheDocument();
  expect(screen.getAllByText('在线').length).toBeGreaterThanOrEqual(2);
  // 编辑 remote-1 → 保存 → PUT carrier/hosts → restart Alert
  const row = screen.getByText('remote-1').closest('tr')!;
  await userEvent.click(within(row).getByRole('button', { name: /编\s*辑/ }));
  const endpointInput = await screen.findByRole('textbox', { name: /访问端点/ });
  expect(endpointInput).toHaveValue('ssh://user@192.168.1.50');
  await userEvent.click(screen.getByRole('button', { name: /保\s*存/ }));
  await waitFor(() =>
    expect(resourceRequests.carrierHosts.length).toBe(1),
  );
  const body = resourceRequests.carrierHosts[0] as {
    items: { key: string; endpoint: string }[];
  };
  expect(body.items[0]).toMatchObject({
    key: 'remote-1',
    endpoint: 'ssh://user@192.168.1.50',
  });
  await waitFor(() =>
    expect(screen.getByTestId('restart-alert').textContent).toContain(
      '已写入配置，重启平台后生效',
    ),
  );
}, 60000);

// ③ 沙箱页：卡片 + 探活 detail + 设备端点聚合（env.backend 前缀匹配平台键）
test('沙箱页：平台卡片 + 探活 detail + 设备端点聚合', async () => {
  renderApp('/sandboxes');
  await waitFor(() => expect(screen.getByText('ensp-pro-1')).toBeInTheDocument());
  expect(screen.getAllByText('eNSP Pro').length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText('离线开发用')).toBeInTheDocument(); // Fake 卡
  expect(screen.getByText('connect timeout')).toBeInTheDocument(); // pnetlab 探活 detail
  // 展开设备端点面板：env.backend=ensp-pro 匹配平台键 ensp-pro-1（前缀）
  await userEvent.click(screen.getByText('设备端点（关联评测集 1）'));
  await waitFor(() =>
    expect(screen.getAllByTestId('device-endpoint').length).toBe(1),
  );
  expect(screen.getByTestId('device-endpoint').textContent).toContain(
    '192.168.56.10:22',
  );
  expect(screen.getByText('去编辑凭证').closest('a')).toHaveAttribute(
    'href',
    '/evalsets',
  );
}, 60000);

// ③b 物理网络类型（用户第 5 轮反馈 3）：kind 四值 + physical 隐藏 URL + 卡片 Tag
test('沙箱页：kind 四值选项 + physical 隐藏 URL 输入 + 物理网络卡片 Tag', async () => {
  renderApp('/sandboxes');
  await waitFor(() => expect(screen.getByText('phys-1')).toBeInTheDocument());
  expect(screen.getAllByText('物理网络').length).toBeGreaterThanOrEqual(1); // phys-1 卡片类型 Tag（卡头+描述行）
  // 打开新增平台弹窗：类型选项四值（文本嵌在 Space 内——按 option-content DOM 断言）
  await userEvent.click(screen.getByRole('button', { name: '新增平台' }));
  const kindSelect = await screen.findByRole('combobox', { name: '类型' });
  await userEvent.click(kindSelect);
  const optionEls = await waitFor(() => {
    const els = document.querySelectorAll('.ant-select-item-option-content');
    expect(els.length).toBe(4);
    return els;
  }, { timeout: 10_000 });
  expect(Array.from(optionEls).map((e) => e.textContent)).toEqual([
    'eNSP Pro',
    'PNETLab',
    '物理网络',
    'Fake（离线开发）',
  ]);
  // 选「物理网络」→ URL 输入隐藏，出现设备 SSH 提示
  await userEvent.click(optionEls[2]!);
  await waitFor(() =>
    expect(
      screen.getByText('物理网络无需平台地址，以设备 SSH 可达判断在线'),
    ).toBeInTheDocument(),
  );
  expect(
    screen.queryByPlaceholderText('https://192.168.56.101:8443'),
  ).not.toBeInTheDocument();
}, 60000);

// ③c 平台级设备清单（U6/ADR-25 增补）：物理类型设备编辑器 + 提交 devices + 卡片"设备 N 台"
test('沙箱页：平台级设备清单编辑器 + 提交 devices + 卡片设备计数', async () => {
  renderApp('/sandboxes');
  // phys-1 卡片：设备 2 台 + 平台配置探测文案
  await waitFor(() =>
    expect(screen.getByTestId('device-count').textContent).toContain('设备 2 台'),
  );
  expect(screen.getByText('设备 SSH 可达（平台配置）')).toBeInTheDocument();
  // 新增平台 → 选物理网络 → 出现设备清单编辑器
  await userEvent.click(screen.getByRole('button', { name: '新增平台' }));
  const kindSelect = await screen.findByRole('combobox', { name: '类型' });
  await userEvent.click(kindSelect);
  const optionEls = await waitFor(() => {
    const els = document.querySelectorAll('.ant-select-item-option-content');
    expect(els.length).toBe(4);
    return els;
  }, { timeout: 10_000 });
  await userEvent.click(optionEls[2]!); // 物理网络
  await waitFor(() =>
    expect(screen.getByTestId('device-editor')).toBeInTheDocument(),
  );
  // 填平台标识 + 两个设备行 → 提交 body 含 devices
  await userEvent.type(screen.getByRole('textbox', { name: /平台标识/ }), 'phys-2');
  await userEvent.click(screen.getByRole('button', { name: /添加设备/ }));
  await userEvent.type(screen.getByPlaceholderText('R1'), 'R1');
  await userEvent.type(screen.getByPlaceholderText('192.168.56.10'), '192.168.56.11');
  await userEvent.click(screen.getByRole('button', { name: '保 存' }));
  await waitFor(() =>
    expect(resourceRequests.sandboxBackend.length).toBe(1),
  );
  const req = resourceRequests.sandboxBackend[0] as {
    key: string;
    body: Record<string, unknown>;
  };
  expect(req.key).toBe('phys-2');
  const body = req.body;
  expect(body.kind).toBe('physical');
  expect(body.devices).toEqual({ R1: { ip: '192.168.56.11', port: 22 } });
}, 60000);

// ④ 克隆向导：设备 ip/port 动态行（源 ssh_access 预填）→ 提交 201 → 列表出现新名
test('评测集克隆：设备端点预填 + 覆盖提交 + 重名 409', async () => {
  renderApp('/evalsets');
  await waitFor(() =>
    expect(screen.getByText('netops-bgp-triage')).toBeInTheDocument(),
  );
  const modalOk = () =>
    screen
      .getAllByRole('button', { name: /^克\s*隆$/ })
      .find((b) => b.tagName === 'BUTTON')!;
  const row = screen.getByText('netops-bgp-triage').closest('tr')!;
  // 源集设备数列（ssh_access 计数）
  expect(within(row).getByText('1')).toBeInTheDocument();
  await userEvent.click(within(row).getByRole('button', { name: /克\s*隆/ }));
  // 设备端点动态行：R1 预填 192.168.56.10 / 22
  const ipInput = await screen.findByDisplayValue('192.168.56.10');
  expect(ipInput).toBeInTheDocument();
  // 覆盖 ip/port
  await userEvent.clear(ipInput);
  await userEvent.type(ipInput, '10.201.0.10');
  const nameInput = screen.getByRole('textbox', { name: /新名称/ });
  await userEvent.clear(nameInput);
  await userEvent.type(nameInput, 'netops-bgp-site2');
  await userEvent.click(modalOk());
  await waitFor(() =>
    expect(resourceRequests.clone.length).toBe(1),
  );
  expect(resourceRequests.clone[0]).toMatchObject({
    new_name: 'netops-bgp-site2',
    overrides: {
      device_endpoints: { R1: { ip: '10.201.0.10', port: 22 } },
    },
  });
  // 201 → 列表刷新出现新集
  await waitFor(() =>
    expect(screen.getByText('netops-bgp-site2')).toBeInTheDocument(),
  );

  // 重名 409 → Modal 内错误提示
  const row2 = screen.getByText('netops-bgp-triage').closest('tr')!;
  await userEvent.click(within(row2).getByRole('button', { name: /克\s*隆/ }));
  const nameInput2 = await screen.findByRole('textbox', { name: /新名称/ });
  await userEvent.clear(nameInput2);
  await userEvent.type(nameInput2, 'netops-smoke');
  await userEvent.click(modalOk());
  await waitFor(() =>
    expect(screen.getAllByText(/already exists/).length).toBeGreaterThanOrEqual(1),
  );
}, 60000);

// ⑤ M1 权限投影：菜单权限位 + 无权列表 403 + 写按钮门控
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { MENU, can } from '../../src/utils/permissions';

test('菜单权限投影（U1 合并后）：菜单无 /images 项，镜像入口收敛到 /suts 页内 Tab', () => {
  const visible = MENU.flatMap((g) => g.items)
    .filter((i) => can('researcher', i.perm ?? 'view:read'))
    .map((i) => i.key);
  expect(visible).not.toContain('/images'); // 菜单不再有独立镜像项（任何角色）
  expect(visible).toContain('/sandboxes'); // GET 仅需登录 → view:read
  expect(visible).toContain('/environments'); // GET 仅 view:read
  const adminVisible = MENU.flatMap((g) => g.items)
    .filter((i) => can('admin', i.perm ?? 'view:read'))
    .map((i) => i.key);
  expect(adminVisible).not.toContain('/images');
});

test('镜像页 403：无权列表渲染 403 Result 而非空白', async () => {
  server.use(
    http.get('/api/v1/docker/images', () =>
      HttpResponse.json({ detail: 'forbidden' }, { status: 403 }),
    ),
  );
  renderApp('/images');
  expect(await screen.findByText('403 无权访问')).toBeInTheDocument();
  expect(screen.getByText(/image:manage/)).toBeInTheDocument();
});

test('镜像页写按钮门控：researcher 无路径导入且删除禁用', async () => {
  useUiStore.getState().setUser({ username: 'researcher', role: 'researcher' });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/images']}>
        <Routes>
          <Route path="/images" element={<ImagesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(screen.getByText('ipops-agent:v1.2')).toBeInTheDocument(),
  );
  expect(screen.queryByRole('button', { name: '路径导入' })).not.toBeInTheDocument();
  // 删除按钮禁用（无 image:manage）
  expect(
    screen.getByRole('button', { name: '删除 busybox:latest' }),
  ).toHaveStyle({ cursor: 'not-allowed' });
});
