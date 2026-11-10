// EE-007 golden path（T8）：登录 → Dashboard → 会话列表（待审批）→ 创建向导
// 三步 → PAUSED 会话详情（门②假设提交）→ 效果/修改对比/任务明细 Tab → 退出。
// 后端为 start_server.py 离线装配（预置 SUT/评测集/PAUSED 会话）。
import { expect, test } from '@playwright/test';

const ADMIN = 'admin';
const PASSWORD = 'Playwright123!';

test('golden path: login → dashboard → create → paused session gates → tabs → logout', async ({
  page,
}) => {
  // ── 登录 ─────────────────────────────────────────────────────────────
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('用户名').fill(ADMIN);
  await page.getByLabel('密码').fill(PASSWORD);
  await page.getByRole('button', { name: /登\s*录/ }).click();

  // ── Dashboard：指标卡 + 依赖面板 ────────────────────────────────────
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText('运行中', { exact: true })).toBeVisible();
  await expect(page.getByText('待审批', { exact: true })).toBeVisible();
  await expect(page.getByText('外部依赖')).toBeVisible();
  await expect(page.getByText('系统状态')).toBeVisible();

  // ── 会话列表：预置 PAUSED 会话 + 待审批横幅 ─────────────────────────
  await page.getByRole('menuitem', { name: '会话管理' }).click();
  await expect(page).toHaveURL(/\/sessions$/);
  await expect(page.getByText('等待审批', { exact: false })).toBeVisible();
  const banner = page.locator('.ant-alert-warning', {
    hasText: '会话等待审批',
  });
  await expect(banner).toBeVisible();

  // ── 创建会话向导三步（用第二个 SUT——预置会话占用第一个会被 409）───
  await page.getByRole('button', { name: '创建会话' }).click();
  await expect(page).toHaveURL(/\/sessions\/new$/);

  // Step 1：SUT / 运行环境 / 评测集
  // antd Select 下拉为 body 级 portal：可见项是 .ant-select-item-option
  // （title=选项名，[role=option] 是 0x0 隐藏的无障碍副本，点不到）。
  const openOption = (title: string) =>
    page
      .locator(
        `.ant-select-dropdown:visible .ant-select-item-option[title="${title}"]`,
      )
      .last();
  await page.getByRole('combobox', { name: 'SUT' }).click();
  await openOption('e2e-agent-2').click();
  await page.getByRole('combobox', { name: '运行环境' }).click();
  await openOption('main').click();
  await page.getByRole('combobox', { name: '评测集' }).click();
  await openOption('e2e-evalset').click();
  await page.getByRole('button', { name: '下一步' }).click();

  // Step 2：目标与策略（缺省 profile 已含 1 条目标 + 公式预览）
  await expect(page.getByText('公式预览：', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '下一步' }).click();

  // Step 3：摘要 → 提交（201）→ 跳回列表
  await expect(page.getByText('e2e-agent-2')).toBeVisible();
  await page.getByRole('button', { name: '提交创建' }).click();
  await expect(page).toHaveURL(/\/sessions$/);
  await expect(page.getByText('e2e-agent-2').first()).toBeVisible();

  // ── 进入 PAUSED 会话详情（横幅链接直达）──────────────────────────────
  await banner.locator('a').first().click();
  await expect(page).toHaveURL(/\/sessions\/iter-/);

  // 进展 Tab：Steps + 等待审批横幅
  await expect(
    page.getByTestId('awaiting-alert'),
  ).toContainText('等待您审批：假设提交');
  await expect(page.getByTestId('steps-panel')).toBeVisible();
  await expect(page.getByTestId('steps-panel').getByText('假设')).toBeVisible();

  // 门②：输入 hypothesis 提交 → 成功 message
  const gate2 = page.getByTestId('gate-hypothesis');
  await expect(gate2.getByText('待审批')).toBeVisible();
  await gate2.locator('textarea').fill('E2E: 在 BGP 排查 skill 中增加先校验邻居状态的步骤');
  await gate2.getByRole('button', { name: '提交假设' }).click();
  await expect(page.getByText('已提交假设：', { exact: false })).toBeVisible();

  // ── 效果 Tab：ECharts canvas ────────────────────────────────────────
  await page.getByRole('tab', { name: '效果' }).click();
  await expect(page.locator('canvas').first()).toBeVisible();

  // ── 修改对比 Tab：预置轮次有版本快照 + files_changed ─────────────────
  await page.getByRole('tab', { name: '修改对比' }).click();
  await expect(page.getByText('skill/bgp.md').first()).toBeVisible();

  // ── 任务明细 Tab：预置 run 行存在 ───────────────────────────────────
  await page.getByRole('tab', { name: '任务明细' }).click();
  await expect(page.getByText('run-e2e-1').first()).toBeVisible();

  // ── 退出登录 ─────────────────────────────────────────────────────────
  await page.getByRole('button', { name: /退\s*出/ }).click();
  await expect(page).toHaveURL(/\/login$/);
});
