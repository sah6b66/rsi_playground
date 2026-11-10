// 日志 Tab（DD-14 §5.10②）：GET /system/logs?level=（后端实际仅支持
// cursor/limit/level——run/experiment 检索待后端扩展）。
import { Select, Space, Table, Tag } from 'antd';
import { useState } from 'react';
import { useSystemLogs } from '../../../api/hooks';

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: 'default',
  INFO: 'blue',
  WARNING: 'orange',
  ERROR: 'red',
  CRITICAL: 'red',
};

export function LogsTab() {
  const [level, setLevel] = useState<string | undefined>();
  const { data, isLoading } = useSystemLogs({ level });

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <span>级别：</span>
        <Select
          allowClear
          placeholder="全部"
          style={{ width: 140 }}
          value={level}
          onChange={(v) => setLevel(v)}
          options={['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((l) => ({
            value: l,
            label: l,
          }))}
        />
      </Space>
      <Table
        rowKey={(r) => `${r.ts}-${r.message}`}
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={{ pageSize: 20 }}
        columns={[
          { title: '时间', dataIndex: 'ts', width: 220 },
          {
            title: '级别',
            dataIndex: 'level',
            width: 100,
            render: (v: string) => <Tag color={LEVEL_COLORS[v] ?? 'default'}>{v}</Tag>,
          },
          {
            title: '组件',
            dataIndex: 'component',
            width: 140,
            render: (v?: string) => v ?? '—',
          },
          { title: '消息', dataIndex: 'message' },
        ]}
      />
    </Space>
  );
}
