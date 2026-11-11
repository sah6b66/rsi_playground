// 评测集详情页（U7a）：页头概要 + 组网环境 / 设备清单（凭证剥离）/ 任务列表 三卡片。
import { Alert, Button, Card, Descriptions, Skeleton, Space, Table, Tag } from 'antd';
import { useNavigate, useParams } from 'react-router-dom';
import { useEvalset } from '../../api/hooks';
import { StatusBadge } from '../../components/StatusBadge';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

export function EvalsetDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const evalset = useEvalset(id);
  const user = useUiStore((s) => s.user);
  const canManage = can(user?.role, 'evalset:manage');

  if (evalset.isLoading) return <Skeleton active />;
  if (evalset.isError)
    return (
      <Alert
        type="error"
        showIcon
        message={`加载评测集失败：${evalset.error.message}`}
      />
    );
  const es = evalset.data!;
  const parsed = es.env_parsed ?? null;

  const exportZip = async () => {
    const { getToken } = await import('../../api/client');
    const resp = await fetch(`/api/v1/evalsets/${es.id}:export`, {
      headers: { Authorization: `Bearer ${getToken() ?? ''}` },
    });
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${es.name}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card>
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <Space wrap>
            <h3 style={{ margin: 0 }} data-testid="evalset-detail-name">
              {es.name}
            </h3>
            <Tag>{es.purpose}</Tag>
            {es.frozen ? (
              <Tag color="blue">已冻结</Tag>
            ) : (
              <Tag color="green">可编辑</Tag>
            )}
          </Space>
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="任务数">{es.tasks.length}</Descriptions.Item>
            <Descriptions.Item label="版本">v{es.version}</Descriptions.Item>
            <Descriptions.Item label="创建时间">
              {es.created_at}
            </Descriptions.Item>
          </Descriptions>
          <Space>
            <Button onClick={() => void exportZip()}>导出 zip</Button>
            {canManage && (
              <Button onClick={() => navigate('/evalsets')}>克隆（列表页）</Button>
            )}
            <Button onClick={() => navigate('/evalsets')}>返回列表</Button>
          </Space>
        </Space>
      </Card>

      {/* ① 组网环境 */}
      <Card title="组网环境" size="small">
        {parsed ? (
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="环境名称">
              {parsed.name || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="backend 类型">
              {parsed.backend || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="设备数">
              {parsed.devices.length}
            </Descriptions.Item>
            <Descriptions.Item label="网络环境准备API">
              {parsed.config_apis.length === 0
                ? '未定义'
                : parsed.config_apis.map((a, i) => (
                    <div key={i}>
                      {a.name || '未命名'}：{a.base_url || '未定义'}
                    </div>
                  ))}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Alert type="info" showIcon message="该评测集未携带 env.yaml" />
        )}
      </Card>

      {/* ② 设备清单（凭证字段永不展示） */}
      <Card title="设备清单" size="small">
        <Table
          rowKey={(_, i) => String(i)}
          size="small"
          pagination={false}
          dataSource={parsed?.devices ?? []}
          columns={[
            { title: '设备名称', dataIndex: 'name', render: (v) => v || '—' },
            { title: '类型', dataIndex: 'type', render: (v) => v || '—' },
            { title: '版本', dataIndex: 'version', render: (v) => v || '—' },
            { title: 'IP 地址', dataIndex: 'ip', render: (v) => v || '—' },
            {
              title: 'SSH 端口',
              dataIndex: 'port',
              render: (v) => (v == null ? '—' : v),
            },
          ]}
        />
      </Card>

      {/* ③ 任务列表 */}
      <Card title="任务列表" size="small">
        <Table
          rowKey={(_, i) => String(i)}
          size="small"
          pagination={false}
          dataSource={es.tasks.map((t, i) => ({
            idx: i + 1,
            id: (t as { id?: string }).id,
            title: (t as { prompt?: string }).prompt,
          }))}
          columns={[
            { title: '序号', dataIndex: 'idx', width: 70 },
            { title: '任务 ID', dataIndex: 'id', render: (v) => v || '—' },
            { title: '标题', dataIndex: 'title', render: (v) => v || '—' },
          ]}
        />
      </Card>

      {/* 冻结状态可视化（StatusBadge 复用可读文案） */}
      <Space>
        <span style={{ color: 'rgba(0,0,0,0.45)' }}>冻结状态：</span>
        <StatusBadge value={es.frozen ? 'frozen' : 'editable'} />
      </Space>
    </Space>
  );
}