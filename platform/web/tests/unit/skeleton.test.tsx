import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { StatusBadge } from '../../src/components/StatusBadge';
import { Layout } from '../../src/components/Layout';
import { can, MENU } from '../../src/utils/permissions';
import { depLabel, eventTypeLabel } from '../../src/utils/labels';
import { useUiStore } from '../../src/stores/ui';

test('StatusBadge 渲染状态文案', () => {
  render(<StatusBadge value="running" />);
  expect(screen.getByText('运行中')).toBeInTheDocument();
});

test('Layout 顶栏显示品牌名「网络Agent RSI演练场」', () => {
  useUiStore.getState().setUser({ username: 'admin', role: 'admin' });
  render(
    <MemoryRouter>
      <Layout />
    </MemoryRouter>,
  );
  expect(screen.getByText('网络Agent RSI演练场')).toBeInTheDocument();
});

test('依赖中文名映射：DEP_LABELS 生效（lab→实验环境设备）', () => {
  expect(depLabel('lab')).toBe('实验环境设备');
  expect(depLabel('llm')).toBe('LLM 服务');
  expect(depLabel('docker')).toBe('Docker 引擎');
  expect(depLabel('docker:host-b')).toBe('Docker 引擎（host-b）');
  expect(depLabel('platform:ensp-pro-1')).toBe('网络仿真平台（ensp-pro-1）');
  expect(depLabel('mystery-dep')).toBe('mystery-dep'); // 未知保留原文
});

test('轨迹事件类型中文名映射 + 回落原值', () => {
  expect(eventTypeLabel('tool_call')).toBe('工具调用');
  expect(eventTypeLabel('llm_request')).toBe('模型请求');
  expect(eventTypeLabel('llm_response')).toBe('模型响应');
  expect(eventTypeLabel('task_end')).toBe('任务结束');
  expect(eventTypeLabel('unknown_evt')).toBe('unknown_evt');
});

test('菜单 IA（U1）：无 /images 项，/suts 标签为「网络Agent」，运行环境紧随其后', () => {
  const flat = MENU.flatMap((g) => g.items);
  expect(flat.map((i) => i.key)).not.toContain('/images');
  const sutsItem = flat.find((i) => i.key === '/suts')!;
  expect(sutsItem.label).toBe('网络Agent');
  const labels = flat.map((i) => i.label);
  expect(labels.indexOf('运行环境')).toBe(labels.indexOf('网络Agent') + 1);
  expect(labels).toContain('网络仿真');
  expect(labels).toContain('运行总览');
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
