// Dashboard 运行总览（DD-14 §5.2，UX 二轮+第 5 轮）：
// 指标卡两行各 3 张（会话类/资源类分行，span=8 等宽）；资源卡统一 X/Y 风格
// （网络Agent=运行实例/注册总数、运行环境=/carrier/hosts 在线数、
// 网络仿真平台=available 数）；系统状态与环境状态面板各自独占一行
// （全宽）；Token 消耗并入系统状态面板。
import { Card, Col, Empty, Progress, Row, Table } from 'antd';
import { Link } from 'react-router-dom';
import {
  useBackends,
  useEnvironments,
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
  const environments = useEnvironments();
  const health = useSystemHealth();

  const items = sessions.data?.items ?? [];
  const running = items.filter((it) => ACTIVE_STATES.has(it.state)).length;
  const awaiting = items.filter((it) => AWAITING_STATES.has(it.state)).length;
  const done = items.filter((it) => DONE_STATES.has(it.state)).length;

  const agentCount = suts.data?.items.length ?? 0;

  // 运行环境（X/Y）：GET /carrier/hosts，在线=online true 计数
  const hostItems = environments.data?.items ?? [];
  const hostOnline = hostItems.filter((h) => h.online).length;

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
        <Col span={8}>
          <MetricCard title="运行中会话" value={running} status="running" />
        </Col>
        <Col span={8}>
          <MetricCard
            title="待审批会话"
            value={awaiting}
            status={awaiting > 0 ? 'paused_awaiting' : undefined}
          />
        </Col>
        <Col span={8}>
          <MetricCard title="已完成会话" value={done} status="finalized" />
        </Col>
        <Col span={8}>
          <MetricCard
            title="网络Agent"
            value={`${runningInstances}/${agentCount}`}
            sub="运行实例 / 注册总数"
          />
        </Col>
        <Col span={8}>
          <MetricCard
            title="运行环境"
            value={`${hostOnline}/${hostItems.length}`}
            sub="在线 / 总数"
          />
        </Col>
        <Col span={8}>
          <MetricCard
            title="网络仿真平台"
            value={`${backendOnline}/${backendItems.length}`}
            sub="在线 / 总数"
          />
        </Col>
      </Row>

      <Card size="small" title="系统状态" style={{ marginTop: 12 }}>
        服务状态：
        {health.data
          ? health.data.status === 'ok'
            ? '正常'
            : health.data.status
          : '…'}
        <div style={{ marginTop: 8 }}>
          Token 消耗
          <Progress
            percent={budgetPct}
            status={budgetPct >= 100 ? 'exception' : 'normal'}
          />
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>
            {tokensUsed.toLocaleString()} / {BUDGET_LIMIT.toLocaleString()}（缺省上限）
          </span>
        </div>
      </Card>

      <Card size="small" title="环境状态" style={{ marginTop: 12 }}>
        <DependencyPanel />
      </Card>

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
