// 进展 Tab（DD-14 §5.4）：6 步大图 + 轮次时间线 + 四门工作台。
import { PauseOutlined } from '@ant-design/icons';
import { Card, Steps, Timeline as AntdTimeline, Typography } from 'antd';
import type { Iteration } from '../../../api/hooks';
import { useSessionTimeline } from '../../../api/hooks';
import { GateWorkbench } from '../../../components/GateWorkbench';
import { statusMeta } from '../../../utils/constants';

// 步序（DD 六阶段）；awaiting_* 对齐其所属门阶段
export const STEP_NAMES = ['归因', '假设', '门审', '应用', '验证', '决策'];
export const STEP_OF: Record<string, number> = {
  attributing: 0, awaiting_direction: 0,
  awaiting_hypothesis: 1, hypothesizing: 1,
  awaiting_review: 2, reviewing: 2,
  applying: 3,
  verifying: 4,
  awaiting_decision: 5, deciding: 5,
};

export function currentStepIndex(it: Iteration): number {
  return STEP_OF[it.state] ?? (it.current_step ? STEP_OF[it.current_step] : 0);
}

// 轮状态 → Timeline dot 色（finalized=绿 / running=蓝 / failed=红 / 其他灰）
const ROUND_COLOR: Record<string, string> = {
  finalized: 'green',
  running: 'blue',
  failed: 'red',
  aborted: 'gray',
  created: 'gray',
};

export function ProgressTab({ session }: { session: Iteration }) {
  const timeline = useSessionTimeline(session.id);
  const current = currentStepIndex(session);
  const paused = session.state === 'paused';

  return (
    <div>
      <Card title="迭代进展" style={{ marginBottom: 12 }} data-testid="steps-panel">
        <Steps
          size="small"
          current={current}
          items={STEP_NAMES.map((name, i) => ({
            title: name,
            // PAUSED：当前门用 pause 图标高亮（等待人工审批）
            icon:
              paused && i === current ? <PauseOutlined /> : undefined,
            description:
              paused && i === current ? '等待审批' : undefined,
          }))}
        />
      </Card>

      <Card title="轮次记录" style={{ marginBottom: 12 }}>
        {(timeline.data?.rounds.length ?? 0) === 0 ? (
          <Typography.Text type="secondary">暂无轮次数据</Typography.Text>
        ) : (
          <AntdTimeline
            items={(timeline.data?.rounds ?? []).map((r) => ({
              color: ROUND_COLOR[r.status ?? ''] ?? 'gray',
              children: (
                <span data-testid={`round-${r.round}`}>
                  第 {r.round} 轮 · {statusMeta(r.status ?? '').label} ·{' '}
                  {r.files_changed.length} 处文件修改
                </span>
              ),
            }))}
          />
        )}
      </Card>

      <GateWorkbench session={session} />
    </div>
  );
}
