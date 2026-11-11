// 审计 Tab（admin，DD-14 §5.10③）：GET /audit-logs?action=&actor=；
// detail JSON 可展开。
import { Input, Space, Table, Tag } from 'antd';
import { useState } from 'react';
import { useAuditLogs } from '../../../api/hooks';
import { auditActionLabel } from '../../../utils/labels';

function DetailJson({ detail }: { detail: Record<string, unknown> }) {
  return (
    <pre style={{ margin: 0, fontSize: 12 }}>
      {JSON.stringify(detail, null, 2)}
    </pre>
  );
}

export function AuditTab() {
  const [action, setAction] = useState('');
  const [actor, setActor] = useState('');
  const { data, isLoading } = useAuditLogs({
    action: action || undefined,
    actor: actor || undefined,
  });

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Input.Search
          placeholder="操作包含（如 sut.create）"
          style={{ width: 240 }}
          allowClear
          onSearch={setAction}
          data-testid="audit-action-filter"
        />
        <Input.Search
          placeholder="操作人等于（如 admin）"
          style={{ width: 200 }}
          allowClear
          onSearch={setActor}
        />
      </Space>
      <Table
        rowKey={(r) => `${r.ts}-${r.actor}-${r.action}-${r.object_id}`}
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={{ pageSize: 20 }}
        expandable={{
          expandedRowRender: (r) => <DetailJson detail={r.detail} />,
          rowExpandable: (r) => Object.keys(r.detail ?? {}).length > 0,
        }}
        columns={[
          { title: '时间', dataIndex: 'ts', width: 220 },
          { title: '操作人', dataIndex: 'actor', width: 120 },
          {
            title: '操作',
            dataIndex: 'action',
            width: 180,
            render: (v: string) => (
            <Tag color="blue">{auditActionLabel(v)}</Tag>
          ),
          },
          { title: '对象类型', dataIndex: 'object_type', width: 120 },
          { title: '对象 ID', dataIndex: 'object_id', width: 180 },
        ]}
      />
    </Space>
  );
}
