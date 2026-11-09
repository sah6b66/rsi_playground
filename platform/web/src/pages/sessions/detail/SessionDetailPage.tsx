// 会话详情（DD-14 §5.4，T4）：顶部概要 + PAUSED 审批横幅 + 五 Tab。
// 前两 Tab（进展/效果）本任务实现；后三个 T5 替换为内嵌占位。
import { Alert, Card, Descriptions, Skeleton, Tabs, Typography } from 'antd';
import { useParams } from 'react-router-dom';
import { useSession, useSuts, type Iteration } from '../../../api/hooks';
import { StatusBadge } from '../../../components/StatusBadge';
import { ProgressTab } from '../tabs/ProgressTab';
import { EffectTab } from '../tabs/EffectTab';

// awaiting_* → 门中文名（横幅/Steps 高亮共用）
export const AWAITING_LABEL: Record<string, string> = {
  awaiting_direction: '方向确认',
  awaiting_hypothesis: '假设提交',
  awaiting_review: '预审裁决',
  awaiting_decision: '决策执行',
};

function TabComingSoon({ title }: { title: string }) {
  return (
    <Card>
      <Typography.Text type="secondary">
        {title}将在后续版本提供（T5）
      </Typography.Text>
    </Card>
  );
}

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const session = useSession(id);
  const suts = useSuts();

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
          <Descriptions.Item label="SUT">{sutName}</Descriptions.Item>
          <Descriptions.Item label="评测集">
            {it.evalset_id ?? '—'}
          </Descriptions.Item>
          <Descriptions.Item label="模式">{it.mode}</Descriptions.Item>
          <Descriptions.Item label="状态">
            <StatusBadge value={it.state} />
          </Descriptions.Item>
          <Descriptions.Item label="预算消耗">
            {typeof it.budget_used?.tokens === 'number'
              ? `${it.budget_used.tokens.toLocaleString()} tokens`
              : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Tabs
        defaultActiveKey="progress"
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
            children: <TabComingSoon title="修改对比" />,
          },
          {
            key: 'tasks',
            label: '任务明细',
            children: <TabComingSoon title="任务明细" />,
          },
          {
            key: 'traces',
            label: '轨迹',
            children: <TabComingSoon title="轨迹" />,
          },
        ]}
      />
    </div>
  );
}
