// 任务明细 Tab（DD-14 §5.4）：轮次×任务表（状态徽章/token/时延/root_cause），
// 行点击→轨迹 Tab 并选中该 run。
import { Skeleton, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useSessionTimeline } from '../../../api/hooks';
import { StatusBadge } from '../../../components/StatusBadge';
import type { SelectedTask } from '../detail/SessionDetailPage';

interface TaskRow {
  key: string;
  round: number;
  task_id: string | null;
  run_id: string;
  status: string | null;
  tokens: number | null;
  latency_ms: number | null;
  root_cause_match: boolean | null;
}

export function TasksTab({
  id,
  onSelect,
}: {
  id: string;
  onSelect: (t: SelectedTask) => void;
}) {
  const timeline = useSessionTimeline(id);
  if (timeline.isLoading) return <Skeleton active />;
  if (timeline.isError)
    return (
      <Typography.Text type="danger">
        加载任务明细失败：{timeline.error.message}
      </Typography.Text>
    );

  const rows: TaskRow[] = (timeline.data?.rounds ?? []).flatMap((r) =>
    (r.tasks ?? []).map((t) => ({
      key: `${r.round}-${t.run_id}`,
      round: r.round,
      task_id: t.task_id,
      run_id: t.run_id,
      status: t.status,
      tokens: t.tokens,
      latency_ms: t.latency_ms,
      root_cause_match: t.root_cause_match,
    })),
  );

  const columns: ColumnsType<TaskRow> = [
    { title: '轮次', dataIndex: 'round', key: 'round', render: (v) => `第 ${v} 轮` },
    { title: '任务 ID', dataIndex: 'task_id', key: 'task_id' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (v: string | null) => (v ? <StatusBadge value={v} /> : '—'),
    },
    {
      title: 'Token 数',
      dataIndex: 'tokens',
      key: 'tokens',
      render: (v: number | null) => (v != null ? v.toLocaleString() : '—'),
    },
    {
      title: '时延（ms）',
      dataIndex: 'latency_ms',
      key: 'latency_ms',
      render: (v: number | null) => (v != null ? v.toLocaleString() : '—'),
    },
    {
      title: '根因命中',
      dataIndex: 'root_cause_match',
      key: 'root_cause_match',
      render: (v: boolean | null) =>
        v == null ? '—' : v ? <Tag color="success">✓</Tag> : <Tag color="error">✗</Tag>,
    },
    {
      title: '运行 ID',
      dataIndex: 'run_id',
      key: 'run_id',
      render: (v: string) => <code>{v}</code>,
    },
  ];

  return (
    <Table
      data-testid="tasks-table"
      size="small"
      rowKey="key"
      pagination={false}
      dataSource={rows}
      columns={columns}
      onRow={(r) => ({
        onClick: () =>
          onSelect({ runId: r.run_id, round: r.round, taskId: r.task_id }),
        style: { cursor: 'pointer' },
      })}
    />
  );
}
