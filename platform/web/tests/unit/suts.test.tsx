// T7 SUT 管理（计划 Task 7 Step 1④）：列表创建 Modal 提交 body /
// 验证镜像结果清单 / FileEditor 409 行内错误 / 详情版本链对比
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { SutListPage } from '../../src/pages/suts/SutListPage';
import { SutDetailPage } from '../../src/pages/suts/SutDetailPage';
import { useUiStore } from '../../src/stores/ui';
import { resetT7Settings, t7Requests, t7SutId } from '../msw/handlers';

function renderApp(route: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/suts" element={<SutListPage />} />
          <Route path="/suts/:id" element={<SutDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetT7Settings();
  Object.values(t7Requests).forEach((arr) => {
    arr.length = 0;
  });
});

// ① 列表创建 Modal：image_ref 四件套提示 + 提交 body（carrier 默认 opencode）
test('SUT 列表：创建 Modal 提交 body', async () => {
  renderApp('/suts');
  await screen.findByText('ipops-agent-1'); // dashboardHandlers 列表 fixture
  await userEvent.click(screen.getByRole('button', { name: /创建 SUT/ }));
  // 镜像引用四件套：placeholder 样例 + docker load 提示
  const imageInput = screen.getByRole('textbox', { name: /镜像引用/ });
  expect(imageInput).toHaveAttribute('placeholder', 'ipops-agent:v1.2');
  expect(screen.getByText('需先 docker load 到执行主机')).toBeInTheDocument();
  await userEvent.type(screen.getByRole('textbox', { name: /名称/ }), 'ipops-agent-x');
  await userEvent.type(imageInput, 'ipops-agent:v1.3');
  await userEvent.click(screen.getByRole('button', { name: /^创\s*建$/ }));
  await waitFor(() => expect(t7Requests.sutCreate.length).toBe(1));
  expect(t7Requests.sutCreate[0]).toEqual({
    name: 'ipops-agent-x',
    image_ref: 'ipops-agent:v1.3',
    carrier: 'opencode',
  });
});

// ② 验证镜像：GET verify-image → 结果清单 Modal（整体未通过 + 逐项清单）
test('SUT 列表：验证镜像结果清单', async () => {
  renderApp('/suts');
  const row = (await screen.findByText('ipops-agent-1')).closest('tr') as HTMLElement;
  await userEvent.click(within(row).getByRole('button', { name: '验证镜像' }));
  // 结果 Modal：整体未通过 + 逐项清单 + 失败原因
  await screen.findByText('未通过');
  expect(screen.getByText(/image exists on host main/)).toBeInTheDocument();
  expect(screen.getByText(/container runs --version/)).toBeInTheDocument();
  expect(screen.getByText('docker: image not found')).toBeInTheDocument();
  // 行徽章更新为"验证未通过"
  await waitFor(() =>
    expect(screen.getByText('验证未通过')).toBeInTheDocument(),
  );
}, 20_000);

// ③ FileEditor：文件树 + Monaco 保存 commit=false → PUT staging；
// 冻结路径 commit=true → 409 行内错误
test('FileEditor：staging 保存 + 冻结路径 409 行内错误', async () => {
  renderApp(`/suts/${t7SutId}`);
  await screen.findByText('配置文件');
  await screen.findByText('当前文件：');
  // 文件树两个文件（目录聚合）
  expect(screen.getByText('skills')).toBeInTheDocument();
  expect(screen.getByText('bgp.md')).toBeInTheDocument();
  // 默认选中第一个（skills/bgp.md）→ 保存（commit 默认 false）→ PUT staging
  await userEvent.click(screen.getByTestId('save-file'));
  await waitFor(() => expect(t7Requests.filePut.length).toBe(1));
  expect(t7Requests.filePut[0]).toEqual({
    path: 'skills/bgp.md',
    body: { content: '# BGP triage\ncheck peers\n', commit: false, message: '' },
  });
  // 切到冻结域文件 judge/rubric.md → 开 commit + message → 保存 → 409 行内错误
  await userEvent.click(screen.getByText('rubric.md'));
  await userEvent.click(screen.getByTestId('commit-switch'));
  await userEvent.type(screen.getByTestId('commit-message'), 'update rubric');
  await userEvent.click(screen.getByTestId('save-file'));
  const err = await screen.findByTestId('file-409-error');
  expect(err).toHaveTextContent(
    "path 'judge/rubric.md' outside the editable target domain",
  );
  expect(err).toHaveTextContent('未落盘');
  await waitFor(() => expect(t7Requests.filePut.length).toBe(2));
  expect(t7Requests.filePut[1]).toEqual({
    path: 'judge/rubric.md',
    body: {
      content: '# judge rubric (frozen fixture)\n',
      commit: true,
      message: 'update rubric',
    },
  });
});

// ④ 详情页版本 Tab：lineage 版本链 + 选两版本 diff
test('SUT 详情：版本链 + 双版本对比', async () => {
  renderApp(`/suts/${t7SutId}`);
  await screen.findByText('ipops-agent'); // Descriptions 头
  expect(screen.getByText('opencode')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('tab', { name: '版本' }));
  // 版本链表（version_id 与 label 列各含 v1/v2）
  await screen.findAllByText('v1');
  expect(screen.getAllByText('v2').length).toBeGreaterThanOrEqual(2);
  // 选两版本 → diff 渲染（task5 versions handler fixture）
  const [fromSel, toSel] = screen.getAllByRole('combobox');
  await userEvent.click(fromSel);
  await userEvent.click(
    await screen.findByText('v1（snapshot）', {
      selector: '.ant-select-item-option-content',
    }),
  );
  await userEvent.click(toSel);
  // 前一个下拉可能未收起：两个下拉选项同时在 DOM，取最后出现的
  const v2opts = await screen.findAllByText('v2（commit）', {
    selector: '.ant-select-item-option-content',
  });
  await userEvent.click(v2opts[v2opts.length - 1]!);
  await screen.findByText('skill/bgp.md');
}, 20_000);
