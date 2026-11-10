// 会话列表（DD-14 §5.3）：创建入口 + 全局待审批横幅 + 状态化操作 + 6 步迷你条。
import { Alert, Button, Space, Table, Tooltip } from 'antd';
import { Link, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import {
  useEvalsets,
  useSessionAction,
  useSessions,
  useSuts,
  type Iteration,
} from '../../api/hooks';
import { DangerConfirm } from '../../components/DangerConfirm';
import { modeLabel } from '../../utils/labels';
import { StatusBadge } from '../../components/StatusBadge';
import { useUiStore } from '../../stores/ui';
import { can } from '../../utils/permissions';

// 状态分组（值域对齐 IterationState + web 侧聚合态）
const RUNNING = new Set([
  'running', 'created', 'attributing', 'hypothesizing', 'reviewing',
  'applying', 'verifying', 'deciding',
]);
const AWAITING = new Set(['paused_awaiting', 'paused']);
const TERMINAL = new Set(['aborted', 'archived', 'finished', 'finalized', 'failed']);

// 6 步迷你条：归因→假设→门审→应用→验证→决策
const STEP_NAMES = ['归因', '假设', '门审', '应用', '验证', '决策'];
const STEP_OF: Record<string, number> = {
  attributing: 0, awaiting_direction: 0,
  awaiting_hypothesis: 1, hypothesizing: 1,
  awaiting_review: 2, reviewing: 2,
  applying: 3,
  awaiting_decision: 5, verifying: 4, deciding: 5,
};

function StepDots({ it }: { it: Iteration }) {
  // T4：current 由 state 映射步序（attributing→0…）；paused 等待态回退
  // current_step（awaiting_* 已在 STEP_OF 中对齐对应门步骤）。
  const idx =
    STEP_OF[it.state] ??
    (it.current_step ? STEP_OF[it.current_step] : undefined);
  return (
    <span data-testid="step-dots" style={{ whiteSpace: 'nowrap' }}>
      {STEP_NAMES.map((name, i) => (
        <Tooltip key={name} title={`${i + 1}. ${name}`}>
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: 4,
              marginInlineEnd: 3,
              background:
                idx === undefined
                  ? '#d9d9d9'
                  : i < idx
                    ? '#8c8c8c'
                    : i === idx
                      ? '#1677ff'
                      : '#d9d9d9',
            }}
          />
        </Tooltip>
      ))}
    </span>
  );
}

export function SessionListPage() {
  const navigate = useNavigate();
  const sessions = useSessions();
  const suts = useSuts();
  const evalsets = useEvalsets();
  const action = useSessionAction();
  const user = useUiStore((s) => s.user);
  const [abortTarget, setAbortTarget] = useState<Iteration | null>(null);

  const items = [...(sessions.data?.items ?? [])].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );
  const sutName = (id: string) =>
    suts.data?.items.find((s) => s.id === id)?.name ?? id;
  const evalName = (id?: string | null) =>
    id ? (evalsets.data?.items.find((e) => e.id === id)?.name ?? id) : '—';
  const awaiting = items.filter((it) => AWAITING.has(it.state));

  return (
    <div>
      {awaiting.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`有 ${awaiting.length} 个会话等待审批`}
          description={
            <Space wrap>
              {awaiting.map((it) => (
                <Link key={it.id} to={`/sessions/${it.id}`}>
                  {it.id}
                </Link>
              ))}
            </Space>
          }
        />
      )}

      <div style={{ marginBottom: 12 }}>
        <Button
          type="primary"
          disabled={!can(user?.role, 'session:create')}
          onClick={() => navigate('/sessions/new')}
        >
          创建会话
        </Button>
      </div>

      <Table<Iteration>
        size="small"
        rowKey="id"
        dataSource={items}
        loading={sessions.isLoading}
        rowClassName={(it) => (AWAITING.has(it.state) ? 'row-awaiting' : '')}
        onRow={(it) => ({
          onClick: () => navigate(`/sessions/${it.id}`),
          style: { cursor: 'pointer' },
        })}
        columns={[
          {
            title: '会话 ID',
            dataIndex: 'id',
            render: (id: string) => <Link to={`/sessions/${id}`}>{id}</Link>,
          },
          { title: '开始时间', dataIndex: 'created_at' },
          { title: '网络Agent', dataIndex: 'sut_id', render: (v: string) => sutName(v) },
          {
            title: '评测集',
            dataIndex: 'evalset_id',
            render: (v: string | null) => evalName(v),
          },
          { title: '运行模式',
          dataIndex: 'mode',
          render: (m: string) => <span>{modeLabel(m)}</span> },
          {
            title: '状态',
            dataIndex: 'state',
            render: (_: string, it: Iteration) => (
              <Space>
                <StatusBadge value={it.state} />
                <StepDots it={it} />
              </Space>
            ),
          },
          {
            title: '当前阶段',
            dataIndex: 'current_step',
            render: (v: string | null) => v ?? '—',
          },
          {
            title: 'Token 消耗',
            dataIndex: 'budget_used',
            render: (b: Record<string, unknown>) =>
              typeof b?.tokens === 'number'
                ? `${(b.tokens as number).toLocaleString()} Token`
                : '—',
          },
          {
            title: '操作',
            key: 'ops',
            render: (_: unknown, it: Iteration) => (
              <Space onClick={(e) => e.stopPropagation()}>
                {RUNNING.has(it.state) && (
                  <>
                    <Button
                      size="small"
                      onClick={() =>
                        action.mutate({ id: it.id, action: 'pause' })
                      }
                    >
                      暂停
                    </Button>
                    <Button
                      size="small"
                      danger
                      onClick={() => setAbortTarget(it)}
                    >
                      终止
                    </Button>
                  </>
                )}
                {AWAITING.has(it.state) && (
                  <>
                    <Button
                      size="small"
                      onClick={() =>
                        action.mutate({ id: it.id, action: 'resume' })
                      }
                    >
                      恢复
                    </Button>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() => navigate(`/sessions/${it.id}`)}
                    >
                      进详情审批
                    </Button>
                  </>
                )}
                {TERMINAL.has(it.state) && (
                  <Button
                    size="small"
                    disabled={it.state === 'archived'}
                    onClick={() =>
                      action.mutate({ id: it.id, action: 'archive' })
                    }
                  >
                    归档
                  </Button>
                )}
              </Space>
            ),
          },
        ]}
      />

      <DangerConfirm
        open={abortTarget !== null}
        title="终止会话"
        name={abortTarget?.id ?? ''}
        description="终止后不可恢复，请输入完整会话 ID 确认。"
        loading={action.isPending}
        onOk={() => {
          if (abortTarget) action.mutate({ id: abortTarget.id, action: 'abort' });
          setAbortTarget(null);
        }}
        onCancel={() => setAbortTarget(null)}
      />
    </div>
  );
}
