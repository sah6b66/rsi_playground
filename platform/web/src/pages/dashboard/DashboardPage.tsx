// Dashboard 运行总览（DD-14 §5.2）：指标卡行 + 依赖/系统面板 + 最近会话迷你表。
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

  const imageCount = suts.data?.items.length ?? 0;

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
        <Col xs={12} md={8} lg={4}>
          <MetricCard
            title="运行中"
            value={running}
            status="running"
            sub={`待审批 ${awaiting} · 已完成 ${done}`}
          />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard
            title="待审批"
            value={awaiting}
            status={awaiting > 0 ? 'paused_awaiting' : undefined}
          />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard title="已完成" value={done} status="finalized" />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard title="SUT 镜像" value={imageCount} />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard
            title="运行环境"
            value={`${hostOk}/${hostChecks.length}`}
            sub="执行主机"
          />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard
            title="仿真沙箱"
            value={`${backendOnline}/${backendItems.length}`}
            sub={
              backendItems.length > 0
                ? `共 ${backendItems.length}（${backendOnline} 在线）`
                : undefined
            }
          />
        </Col>
        <Col xs={12} md={8} lg={4}>
          <MetricCard title="SUT 实例运行数" value={runningInstances} />
        </Col>
      </Row>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title="外部依赖">
            <DependencyPanel />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="系统状态">
            服务状态：
            {health.data ? health.data.status : '…'}
            <div style={{ marginTop: 8 }}>
              预算消耗（tokens）
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
              { title: 'SUT', dataIndex: 'sut_id' },
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
