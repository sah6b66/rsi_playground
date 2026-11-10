// SUT 详情页（DD-14 §5.6，T7）：Descriptions 头 + lineage 版本链（列表呈现，
// DAG 可视化留后续）+ 配置文件（FileEditor）+ 版本（选两版本 DiffViewer）。
import { Card, Descriptions, Empty, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useSut, useSutLineage, useVersionDiff } from '../../api/hooks';
import { DiffViewer } from '../../components/DiffViewer';
import { FileEditorTab } from './FileEditorTab';

function LineagePanel({ sutId }: { sutId: string }) {
  const { data, isLoading } = useSutLineage(sutId);
  const [fromV, setFromV] = useState<string | null>(null);
  const [toV, setToV] = useState<string | null>(null);
  const diff = useVersionDiff(fromV, toV);

  if (isLoading) return <Spin />;
  const nodes = data?.nodes ?? [];
  if (!nodes.length) return <Empty description="尚无版本（保存时开启 commit 产生新版本）" />;

  const options = nodes.map((n) => ({
    value: n.version_id,
    label: `${n.label}（${n.kind}）`,
  }));

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {/* 版本链（简化 DAG：按 parent 关系列表呈现；图形化 DAG 留后续迭代） */}
      <Table
        rowKey="version_id"
        size="small"
        dataSource={nodes}
        pagination={false}
        columns={[
          { title: '版本', dataIndex: 'version_id' },
          { title: '标签', dataIndex: 'label' },
          {
            title: '类型',
            dataIndex: 'kind',
            width: 110,
            render: (v: string) => <Tag>{v}</Tag>,
          },
          {
            title: '父版本',
            dataIndex: 'parent_ids',
            render: (v: string[]) => (v.length ? v.join(', ') : '—'),
          },
        ]}
      />
      <Card size="small" title="版本对比（DAG 图形化视图规划中）">
        <Space wrap>
          <span>基准：</span>
          <Select
            style={{ width: 200 }}
            placeholder="选择版本"
            value={fromV}
            onChange={setFromV}
            options={options}
            data-testid="diff-from"
          />
          <span>对比：</span>
          <Select
            style={{ width: 200 }}
            placeholder="选择版本"
            value={toV}
            onChange={setToV}
            options={options}
            data-testid="diff-to"
          />
        </Space>
        {fromV && toV && (
          <div style={{ marginTop: 12 }}>
            {diff.isLoading ? (
              <Spin />
            ) : diff.data?.length ? (
              diff.data.map((d) => (
                <div key={d.path} style={{ marginBottom: 16 }}>
                  <p style={{ margin: '4px 0' }}>
                    <code>{d.path}</code> <Tag>{d.op}</Tag>
                  </p>
                  <pre
                    style={{
                      background: '#fafafa',
                      padding: 8,
                      fontSize: 12,
                      maxHeight: 200,
                      overflow: 'auto',
                    }}
                  >
                    {d.patch}
                  </pre>
                </div>
              ))
            ) : (
              <Empty description="两版本无差异" />
            )}
          </div>
        )}
      </Card>
    </Space>
  );
}

export function SutDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: sut, isLoading } = useSut(id);

  if (isLoading) return <Spin />;
  if (!sut) return <Empty description="SUT 不存在" />;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="ID">{sut.id}</Descriptions.Item>
          <Descriptions.Item label="名称">{sut.name}</Descriptions.Item>
          <Descriptions.Item label="载体">{sut.carrier}</Descriptions.Item>
          <Descriptions.Item label="镜像">
            <code>{sut.image_ref}</code>
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">{sut.created_at}</Descriptions.Item>
          <Descriptions.Item label="描述">{sut.description || '—'}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Tabs
        defaultActiveKey="files"
        destroyOnHidden
        items={[
          { key: 'files', label: '配置文件', children: <FileEditorTab sutId={sut.id} /> },
          { key: 'versions', label: '版本', children: <LineagePanel sutId={sut.id} /> },
        ]}
      />
    </Space>
  );
}
