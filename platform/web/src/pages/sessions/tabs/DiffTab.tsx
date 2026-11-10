// 修改对比 Tab（DD-14 §5.4）：轮次选择器 → /versions/{a}/diff/{b} +
// 逐文件 Monaco DiffViewer（内容经 /suts/{id}/files?version= 取前后版本）
// + 该轮修改清单表（path/target/git ref）。
import { Alert, List, Select, Skeleton, Table, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import {
  useSessionTimeline,
  useSutFilePair,
  useVersionDiff,
} from '../../../api/hooks';
import { DiffViewer } from '../../../components/DiffViewer';

const OP_LABEL: Record<string, { label: string; color: string }> = {
  modified: { label: '修改', color: 'blue' },
  added: { label: '新增', color: 'green' },
  deleted: { label: '删除', color: 'red' },
  renamed: { label: '重命名', color: 'orange' },
};

export function DiffTab({ id, sutId }: { id: string; sutId: string }) {
  const timeline = useSessionTimeline(id);
  const selectable = useMemo(
    () =>
      (timeline.data?.rounds ?? []).filter(
        (r) => r.config_version_before != null,
      ),
    [timeline.data],
  );

  const [roundNo, setRoundNo] = useState<number | null>(null);
  const [path, setPath] = useState<string | null>(null);

  useEffect(() => {
    if (roundNo == null && selectable.length) setRoundNo(selectable[0].round);
  }, [selectable, roundNo]);

  const round = selectable.find((r) => r.round === roundNo) ?? null;
  const fromV = round?.config_version_before ?? null;
  const toV = round?.config_version_after ?? null;
  const diff = useVersionDiff(fromV, toV);

  // diff 项（含 op/patch）优先；无 diff 数据时回落 timeline.files_changed
  const files = useMemo(() => {
    if (diff.data?.length) return diff.data.map((d) => d.path);
    return (round?.files_changed ?? [])
      .map((f) => f.path)
      .filter((p): p is string => !!p);
  }, [diff.data, round]);

  useEffect(() => {
    setPath(files.length ? files[0] : null);
  }, [files]);

  const pair = useSutFilePair(sutId, fromV, toV, path);

  if (timeline.isLoading) return <Skeleton active />;
  if (timeline.isError)
    return (
      <Alert
        type="error"
        showIcon
        message={`加载轮次数据失败：${timeline.error.message}`}
      />
    );
  if (!selectable.length)
    return (
      <Typography.Text type="secondary">
        尚无带版本快照的轮次可对比
      </Typography.Text>
    );

  const targetOf = (p: string) =>
    round?.files_changed.find((f) => f.path === p)?.target ?? null;

  return (
    <div>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8 }}>
        <Typography.Text>轮次：</Typography.Text>
        <Select
          data-testid="diff-round-select"
          style={{ width: 260 }}
          value={roundNo ?? undefined}
          onChange={(v) => setRoundNo(v)}
          options={selectable.map((r) => ({
            value: r.round,
            label: `第 ${r.round} 轮（v${r.config_version_before} → v${r.config_version_after}）`,
          }))}
        />
      </div>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ width: 280, flexShrink: 0 }}>
          <Typography.Text strong>变更文件</Typography.Text>
          <div data-testid="diff-file-list">
            <List
              size="small"
              dataSource={files}
              renderItem={(p) => (
                <List.Item
                  style={{
                    cursor: 'pointer',
                    background: p === path ? '#e6f4ff' : undefined,
                    padding: '4px 8px',
                  }}
                  onClick={() => setPath(p)}
                >
                  <span style={{ fontSize: 13 }}>
                    {p}
                    {targetOf(p) && <Tag style={{ marginLeft: 8 }}>{targetOf(p)}</Tag>}
                  </span>
                </List.Item>
              )}
            />
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Typography.Text strong>{path ?? '—'}</Typography.Text>
          <div style={{ marginTop: 8 }}>
            {path ? (
              pair.oldText != null || pair.newText != null ? (
                <DiffViewer
                  oldText={pair.oldText ?? ''}
                  newText={pair.newText ?? ''}
                />
              ) : (
                <Skeleton active />
              )
            ) : null}
          </div>
        </div>
      </div>
      <div style={{ marginTop: 16 }}>
        <Typography.Text strong>修改清单</Typography.Text>
        <Table
          data-testid="diff-manifest-table"
          size="small"
          rowKey="path"
          pagination={false}
          dataSource={files.map((p) => ({
            path: p,
            target: targetOf(p),
            op: diff.data?.find((d) => d.path === p)?.op,
          }))}
          columns={[
            { title: '文件路径', dataIndex: 'path', key: 'path' },
            {
              title: '优化项',
              dataIndex: 'target',
              key: 'target',
              render: (t: string | null) => (t ? <Tag>{t}</Tag> : '—'),
            },
            {
              title: '操作',
              dataIndex: 'op',
              key: 'op',
              render: (op: string | undefined) =>
                op ? (
                  <Tag color={OP_LABEL[op]?.color ?? 'default'}>
                    {OP_LABEL[op]?.label ?? op}
                  </Tag>
                ) : (
                  '—'
                ),
            },
            {
              title: 'Git 版本',
              key: 'ref',
              render: () => (toV != null ? <code>v{toV}</code> : '—'),
            },
          ]}
        />
      </div>
    </div>
  );
}
