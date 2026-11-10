// 会话详情（DD-14 §5.4，T4+T5）：顶部概要 + PAUSED 审批横幅 + 五 Tab
// （进展/效果 T4；修改对比/任务明细/轨迹 T5）。
import { Alert, Card, Descriptions, Skeleton, Tabs } from 'antd';
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useSession, useSuts, type Iteration } from '../../../api/hooks';
import { StatusBadge } from '../../../components/StatusBadge';
import { modeLabel } from '../../../utils/labels';
import { ProgressTab } from '../tabs/ProgressTab';
import { EffectTab } from '../tabs/EffectTab';
import { DiffTab } from '../tabs/DiffTab';
import { TasksTab } from '../tabs/TasksTab';
import { TraceTab } from '../tabs/TraceTab';

// awaiting_* → 门中文名（横幅/Steps 高亮共用）
export const AWAITING_LABEL: Record<string, string> = {
  awaiting_direction: '方向确认',
  awaiting_hypothesis: '假设提交',
  awaiting_review: '预审裁决',
  awaiting_decision: '决策执行',
};

// 任务明细行点击提升到页级：选中 run + 切到轨迹 Tab（T5）
export interface SelectedTask {
  runId: string;
  round: number;
  taskId: string | null;
}

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const session = useSession(id);
  const suts = useSuts();
  const [activeKey, setActiveKey] = useState('progress');
  const [selectedTask, setSelectedTask] = useState<SelectedTask | null>(null);

  if (session.isLoading) return <Skeleton active />;
  if (session.isError)
    return (
      <Alert type="error" showIcon message={`加载会话失败：${session.error.message}`} />
    );
  const it = session.data as Iteration;
  const sutName =
    suts.data?.items.find((s) => s.id === it.sut_id)?.name ?? it.sut_id;
  const awaiting = AWAITING_LABEL[it.current_step ?? ''];

  return (
    <div>
      {it.state === 'paused' && awaiting && (
        <Alert
          type="warning"
          showIcon
          data-testid="awaiting-alert"
          style={{ marginBottom: 12 }}
          message={`等待您审批：${awaiting}`}
        />
      )}
      <Card style={{ marginBottom: 12 }}>
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="会话 ID">{it.id}</Descriptions.Item>
          <Descriptions.Item label="网络Agent">{sutName}</Descriptions.Item>
          <Descriptions.Item label="评测集">
            {it.evalset_id ?? '—'}
          </Descriptions.Item>
          <Descriptions.Item label="运行模式">{modeLabel(it.mode)}</Descriptions.Item>
          <Descriptions.Item label="状态">
            <StatusBadge value={it.state} />
          </Descriptions.Item>
          <Descriptions.Item label="Token 消耗">
            {typeof it.budget_used?.tokens === 'number'
              ? `${it.budget_used.tokens.toLocaleString()} Token`
              : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="模型调用次数">
            {typeof (it.budget_used as { llm_calls?: unknown } | undefined)?.llm_calls ===
            'number'
              ? (it.budget_used as { llm_calls: number }).llm_calls.toLocaleString()
              : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Tabs
        activeKey={activeKey}
        onChange={setActiveKey}
        items={[
          {
            key: 'progress',
            label: '进展',
            children: <ProgressTab session={it} />,
          },
          { key: 'effect', label: '效果', children: <EffectTab id={it.id} /> },
          {
            key: 'diff',
            label: '修改对比',
            children: <DiffTab id={it.id} sutId={it.sut_id} />,
          },
          {
            key: 'tasks',
            label: '任务明细',
            children: (
              <TasksTab
                id={it.id}
                onSelect={(t) => {
                  setSelectedTask(t);
                  setActiveKey('traces');
                }}
              />
            ),
          },
          {
            key: 'traces',
            label: '轨迹',
            children: <TraceTab id={it.id} selected={selectedTask} />,
          },
        ]}
      />
    </div>
  );
}
