// Dashboard 运行总览（DD-14 §5.2，U1 布局规范化）：
// 指标卡统一栅格（span=6 一行 4 张）+ 依赖/系统面板并列同高 + 最近会话独占一行。
import { Card, Col, Empty, Progress, Row, Table } from 'antd';
import { Link } from 'react-router-dom';
import {
  useBackends,
  useDependencies,
  useInstances,
  useSessions,
  useSuts,
  useSystemHealth,
  type Iteration,
} from '../../api/hooks';
import { DependencyPanel } from '../../components/DependencyPanel';
import { MetricCard } from '../../components/MetricCard';
import { StatusBadge } from '../../components/StatusBadge';

// 会话状态分组（前端聚合；state 值对齐 rsi/core/enums.IterationState）
const ACTIVE_STATES = new Set([
  'running',
  'attributing',
  'hypothesizing',
  'reviewing',
  'applying',
  'verifying',
  'deciding',
]);
const AWAITING_STATES = new Set(['paused_awaiting', 'paused']);
const DONE_STATES = new Set(['finalized', 'finished']);

export function DashboardPage() {
  const sessions = useSessions();
  const suts = useSuts();
  const backends = useBackends();
  const instances = useInstances();
  const deps = useDependencies();
  const health = useSystemHealth();

  const items = sessions.data?.items ?? [];
  const running = items.filter((it) => ACTIVE_STATES.has(it.state)).length;
  const awaiting = items.filter((it) => AWAITING_STATES.has(it.state)).length;
  const done = items.filter((it) => DONE_STATES.has(it.state)).length;

  const agentCount = suts.data?.items.length ?? 0;

  // 运行环境：T6 前无 /carrier/hosts —— 用 dependencies 中
  // platform:*（执行主机探活项）计数替代。
  const depItems = deps.data?.items ?? [];
  const hostChecks = depItems.filter((d) => d.name.startsWith('platform:'));
  const hostOk = hostChecks.filter((d) => d.status === 'ok').length;

  const backendItems = backends.data?.items ?? [];
  const backendOnline = backendItems.filter((b) => b.available).length;

  const runningInstances =
    instances.data?.items.filter((i) => i.status === 'running').length ?? 0;

  // 全局预算：sessions budget_used 聚合（上限未配置 → 显示"未配置"）
  const tokensUsed = items.reduce(
    (sum, it) =>
      sum + (typeof (it.budget_used as { tokens?: number })?.tokens === 'number'
        ? (it.budget_used as { tokens: number }).tokens
        : 0),
    0,
  );
  const BUDGET_LIMIT = 1_000_000; // 缺省上限；真实上限随 profile 下发（T4）
  const budgetPct = Math.min(100, Math.round((tokensUsed / BUDGET_LIMIT) * 100));

  const recent: Iteration[] = [...items]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 5);

  return (
    <div>
      <Row gutter={[12, 12]}>
        <Col span={6}>
          <MetricCard title="运行中会话" value={running} status="running" />
        </Col>
        <Col span={6}>
          <MetricCard
            title="待审批会话"
            value={awaiting}
            status={awaiting > 0 ? 'paused_awaiting' : undefined}
          />
        </Col>
        <Col span={6}>
          <MetricCard title="已完成会话" value={done} status="finalized" />
        </Col>
        <Col span={6}>
          <MetricCard title="网络Agent 数" value={agentCount} sub="已登记 Agent" />
        </Col>
        <Col span={6}>
          <MetricCard
            title="运行环境（在线）"
            value={`${hostOk}/${hostChecks.length}`}
            sub="执行主机在线 / 总数"
          />
        </Col>
        <Col span={6}>
          <MetricCard
            title="网络仿真平台（在线）"
            value={`${backendOnline}/${backendItems.length}`}
            sub={`共 ${backendItems.length} 个平台`}
          />
        </Col>
        <Col span={6}>
          <MetricCard
            title="Agent 实例（运行中）"
            value={runningInstances}
            sub="运行中的 Agent 实例数"
          />
        </Col>
        <Col span={6}>
          <MetricCard
            title="Token 预算消耗"
            value={tokensUsed}
            sub={`占缺省上限 ${budgetPct}%`}
          />
        </Col>
      </Row>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col span={12}>
          <Card size="small" title="外部依赖状态" style={{ height: '100%' }}>
            <DependencyPanel />
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small" title="系统状态" style={{ height: '100%' }}>
            服务状态：
            {health.data ? health.data.status : '…'}
            <div style={{ marginTop: 8 }}>
              预算消耗（Token）
              <Progress
                percent={budgetPct}
                status={budgetPct >= 100 ? 'exception' : 'normal'}
              />
              <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                {tokensUsed.toLocaleString()} / {BUDGET_LIMIT.toLocaleString()}（缺省上限）
              </span>
            </div>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="最近会话" style={{ marginTop: 12 }}>
        {recent.length === 0 ? (
          <Empty description="尚无会话">
            <Link to="/sessions">去创建</Link>
          </Empty>
        ) : (
          <Table<Iteration>
            size="small"
            rowKey="id"
            dataSource={recent}
            pagination={false}
            rowClassName={(it) =>
              AWAITING_STATES.has(it.state) ? 'row-awaiting' : ''
            }
            columns={[
              {
                title: '会话',
                dataIndex: 'id',
                render: (id: string) => <Link to={`/sessions/${id}`}>{id}</Link>,
              },
              {
                title: '网络Agent',
                dataIndex: 'sut_id',
              },
              {
                title: '状态',
                dataIndex: 'state',
                render: (s: string) => <StatusBadge value={s} />,
              },
              { title: '轮次', dataIndex: 'round' },
              { title: '创建时间', dataIndex: 'created_at' },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
