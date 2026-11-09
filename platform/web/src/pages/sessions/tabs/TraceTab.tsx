// 轨迹 Tab（DD-14 §5.4）：单 run TraceTimeline + 跨轮对比双栏
// （选另一轮同 task_id 的 run → 并排两个 TraceTimeline）。
import { Col, Row, Select, Skeleton, Typography } from 'antd';
import { useMemo, useState } from 'react';
import { useRunTrace, useSessionTimeline } from '../../../api/hooks';
import { TraceTimeline } from '../../../components/TraceTimeline';
import type { SelectedTask } from '../detail/SessionDetailPage';

function shortRun(runId: string): string {
  return runId.length > 8 ? runId.slice(0, 8) : runId;
}

function TraceColumn({
  title,
  runId,
}: {
  title: string;
  runId: string;
}) {
  const trace = useRunTrace(runId);
  return (
    <div data-testid={`trace-panel-${runId}`}>
      <Typography.Text strong>
        {title} · <code>{shortRun(runId)}</code>
      </Typography.Text>
      {trace.isLoading ? (
        <Skeleton active />
      ) : trace.isError ? (
        <Typography.Text type="danger">
          加载轨迹失败：{trace.error.message}
        </Typography.Text>
      ) : (
        <TraceTimeline events={trace.data?.items ?? []} height={480} />
      )}
    </div>
  );
}

export function TraceTab({
  id,
  selected,
}: {
  id: string;
  selected: SelectedTask | null;
}) {
  const timeline = useSessionTimeline(id);
  const [compareRunId, setCompareRunId] = useState<string | null>(null);

  // 同 task_id 其他轮的可对比 run（排除当前 run）
  const candidates = useMemo(() => {
    if (!selected?.taskId) return [];
    const out: { runId: string; round: number }[] = [];
    for (const r of timeline.data?.rounds ?? []) {
      for (const t of r.tasks ?? []) {
        if (t.task_id === selected.taskId && t.run_id !== selected.runId) {
          out.push({ runId: t.run_id, round: r.round });
        }
      }
    }
    return out;
  }, [timeline.data, selected]);

  if (!selected)
    return (
      <Typography.Text type="secondary" data-testid="trace-none-selected">
        在「任务明细」中点击一行任务查看其轨迹
      </Typography.Text>
    );

  const compare = candidates.find((c) => c.runId === compareRunId) ?? null;

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <Typography.Text>与其他轮对比：</Typography.Text>
        <Select
          data-testid="trace-compare-select"
          allowClear
          placeholder="选择同任务其他轮的 run"
          style={{ width: 300 }}
          value={compareRunId ?? undefined}
          onChange={(v) => setCompareRunId(v ?? null)}
          options={candidates.map((c) => ({
            value: c.runId,
            label: `第 ${c.round} 轮 · ${shortRun(c.runId)}`,
          }))}
        />
      </div>
      {compare ? (
        <Row gutter={12}>
          <Col span={12}>
            <TraceColumn
              title={`第 ${selected.round} 轮`}
              runId={selected.runId}
            />
          </Col>
          <Col span={12}>
            <TraceColumn title={`第 ${compare.round} 轮`} runId={compare.runId} />
          </Col>
        </Row>
      ) : (
        <TraceColumn title={`第 ${selected.round} 轮`} runId={selected.runId} />
      )}
    </div>
  );
}
