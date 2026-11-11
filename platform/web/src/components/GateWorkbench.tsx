// 四门工作台（DD-14 §5.2 / iterations.py 四门端点）。
// 仅当前门可操作；已过的门标"已过"，未到的门 disabled 标"未到"。
import { useState } from 'react';
import { Button, Card, Col, Input, Radio, Row, Select, Tag } from 'antd';
import { useGateAction, useSessionTimeline, type Iteration } from '../api/hooks';

const GATES = [
  {
    key: 'confirm-direction' as const,
    step: 'awaiting_direction',
    title: '门① 方向确认',
  },
  { key: 'hypothesis' as const, step: 'awaiting_hypothesis', title: '门② 假设提交' },
  { key: 'review' as const, step: 'awaiting_review', title: '门③ 预审裁决' },
  { key: 'decision' as const, step: 'awaiting_decision', title: '门④ 决策执行' },
];

export function GateWorkbench({ session }: { session: Iteration }) {
  const timeline = useSessionTimeline(session.id);
  const gate1 = useGateAction(session.id, 'confirm-direction');
  const gate2 = useGateAction(session.id, 'hypothesis');
  const gate3 = useGateAction(session.id, 'review');
  const gate4 = useGateAction(session.id, 'decision');
  const [direction, setDirection] = useState<string[]>([]);
  const [hypothesis, setHypothesis] = useState('');
  const [review, setReview] = useState<'pass' | 'reject'>('pass');
  const [reviewReasons, setReviewReasons] = useState('');
  const [decision, setDecision] = useState('promote');
  const [decisionReasons, setDecisionReasons] = useState('');

  const paused = session.state === 'paused';
  const currentIdx = paused
    ? GATES.findIndex((g) => g.step === session.current_step)
    : -1;

  // 方向候选：timeline files_changed 的 target（fixture clusters 近似）
  const clusterOptions = [
    ...new Set(
      (timeline.data?.rounds ?? []).flatMap((r) =>
        r.files_changed
          .map((f) => f.target)
          .filter((t): t is string => !!t),
      ),
    ),
  ].map((t) => ({ label: t, value: t }));

  return (
    <Row gutter={[12, 12]} data-testid="gate-row">
      {GATES.map((g, i) => {
        const isCurrent = i === currentIdx;
        const passed = session.state !== 'paused' || i < currentIdx;
        return (
          <Col span={6} key={g.key} style={{ display: 'flex' }}>
          <Card
            size="small"
            title={g.title}
            style={{ flex: 1, opacity: isCurrent ? 1 : 0.75 }}
            extra={
              isCurrent ? (
                <Tag color="warning">待审批</Tag>
              ) : passed ? (
                <Tag color="success">已过</Tag>
              ) : (
                <Tag>未到</Tag>
              )
            }
            data-testid={`gate-${g.key}`}
          >
            {g.key === 'confirm-direction' && (
              <>
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder={
                    clusterOptions.length
                      ? '选择归因方向（可多选）'
                      : '暂无可选方向（无归因数据）'
                  }
                  options={clusterOptions}
                  value={direction}
                  onChange={setDirection}
                  disabled={!isCurrent}
                />
                <Button
                  block
                  style={{ marginTop: 8 }}
                  disabled={!isCurrent || direction.length === 0}
                  onClick={() =>
                    gate1.mutate({ selected: direction })
                  }
                >
                  提交方向
                </Button>
              </>
            )}
            {g.key === 'hypothesis' && (
              <>
                <Input.TextArea
                  rows={4}
                  placeholder="例：为 BGP 排查 skill 增加先查邻居状态的步骤"
                  value={hypothesis}
                  onChange={(e) => setHypothesis(e.target.value)}
                  disabled={!isCurrent}
                />
                <Button
                  block
                  type="primary"
                  style={{ marginTop: 8 }}
                  disabled={!isCurrent || !hypothesis.trim()}
                  loading={gate2.isPending}
                  onClick={() => gate2.mutate({ hypothesis })}
                >
                  提交假设
                </Button>
              </>
            )}
            {g.key === 'review' && (
              <>
                <Radio.Group
                  value={review}
                  onChange={(e) => setReview(e.target.value)}
                  disabled={!isCurrent}
                >
                  <Radio value="pass">pass 通过</Radio>
                  <Radio value="reject">reject 驳回</Radio>
                </Radio.Group>
                <Input.TextArea
                  rows={3}
                  style={{ marginTop: 8 }}
                  placeholder="裁决理由（可选）"
                  value={reviewReasons}
                  onChange={(e) => setReviewReasons(e.target.value)}
                  disabled={!isCurrent}
                />
                <Button
                  block
                  style={{ marginTop: 8 }}
                  disabled={!isCurrent}
                  loading={gate3.isPending}
                  onClick={() =>
                    gate3.mutate({ approved: review === 'pass', reasons: reviewReasons })
                  }
                >
                  提交裁决
                </Button>
              </>
            )}
            {g.key === 'decision' && (
              <>
                <Radio.Group
                  value={decision}
                  onChange={(e) => setDecision(e.target.value)}
                  disabled={!isCurrent}
                >
                  <Radio value="promote">promote 晋级</Radio>
                  <Radio value="revert">revert 回滚</Radio>
                  <Radio value="advise_hold">advise_hold 观察</Radio>
                </Radio.Group>
                <Input.TextArea
                  rows={3}
                  style={{ marginTop: 8 }}
                  placeholder="决策理由（可选）"
                  value={decisionReasons}
                  onChange={(e) => setDecisionReasons(e.target.value)}
                  disabled={!isCurrent}
                />
                <Button
                  block
                  style={{ marginTop: 8 }}
                  disabled={!isCurrent}
                  loading={gate4.isPending}
                  onClick={() =>
                    gate4.mutate({ decision, reasons: decisionReasons })
                  }
                >
                  提交决策
                </Button>
              </>
            )}
          </Card>
          </Col>
        );
      })}
    </Row>
  );
}
