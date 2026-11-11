// SUT 运行环境管理（DD-14 §5.9，T6）：主机列表 + 新增/编辑（写 settings.yaml，重启生效）
import { Alert, Badge, Form, Input, InputNumber, Modal, Select, Space, Table, Tag } from 'antd';
import { useState } from 'react';
import {
  useEnvironments,
  useSaveHosts,
  type EnvironmentHost,
} from '../../api/hooks';
import { FieldWithHints } from '../../components/FieldWithHints';
import { QueryErrorResult } from '../../components/QueryErrorResult';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

export function EnvironmentsPage() {
  const { data, isLoading, error } = useEnvironments();
  const user = useUiStore((s) => s.user);
  const canManage = can(user?.role, 'env:manage');
  const saveMut = useSaveHosts();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [form] = Form.useForm<{
    key: string;
    endpoint: string;
    max_instances: number;
    subnets: string[];
  }>();

  const openEdit = (item?: EnvironmentHost) => {
    form.setFieldsValue({
      key: item?.key ?? '',
      endpoint: item?.mode === 'remote' ? item.endpoint : '',
      max_instances: item?.max_instances ?? 2,
      subnets: item?.subnets ?? [],
    });
    setOpen(true);
  };

  if (error) {
    return <QueryErrorResult error={error} needPerm="view:read" />;
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        {canManage && (
          <a role="button" onClick={() => openEdit()}>
            新增主机
          </a>
        )}
      </Space>
      {saved && (
        <Alert
          data-testid="restart-alert"
          type="info"
          showIcon
          message="已写入配置，重启平台后生效"
          description="主机清单属于静态配置，保存后需重启平台才会生效；运行中的主机池不会自动重建"
          closable
          style={{ marginBottom: 16 }}
        />
      )}
      <Table
        rowKey="key"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={false}
        columns={[
          { title: '主机', dataIndex: 'key' },
          {
            title: '模式',
            dataIndex: 'mode',
            render: (m: string) =>
              m === 'remote' ? (
                <Tag color="geekblue">远端</Tag>
              ) : (
                <Tag color="green">本地</Tag>
              ),
          },
          { title: '访问端点', dataIndex: 'endpoint' },
          {
            title: '状态',
            dataIndex: 'online',
            render: (online: boolean) => (
              <Badge
                status={online ? 'success' : 'error'}
                text={online ? '在线' : '离线'}
              />
            ),
          },
          {
            title: '实例',
            render: (_, rec) => `${rec.active_instances} / ${rec.max_instances}`,
          },
          {
            title: '网段池',
            dataIndex: 'subnets',
            render: (subs: string[]) =>
              subs.length ? (
                <span>
                  {subs.map((s) => (
                    <Tag key={s}>{s}</Tag>
                  ))}
                </span>
              ) : (
                '—'
              ),
          },
          {
            title: '操作',
            render: (_, rec) =>
              canManage ? (
                <a role="button" onClick={() => openEdit(rec)}>
                  编辑
                </a>
              ) : null,
          },
        ]}
      />

      <Modal
        open={open}
        title="新增 / 编辑执行主机"
        okText="保存"
        confirmLoading={saveMut.isPending}
        onCancel={() => setOpen(false)}
        onOk={async () => {
          const v = await form.validateFields();
          try {
            await saveMut.mutateAsync([
              {
                key: v.key,
                endpoint: v.endpoint || 'local',
                max_instances: v.max_instances,
                subnets: v.subnets ?? [],
              },
            ]);
          } catch {
            return;
          }
          setOpen(false);
          setSaved(true);
        }}
      >
        <Form form={form} layout="vertical">
          <FieldWithHints
            label="主机标识"
            required
            name="key"
            example="agent-host-1"
            extra="仅小写字母、数字与连字符；创建后不可修改"
            tooltip="用于在平台内唯一标识这台执行主机的名字，会话创建与镜像导入时按它选择主机"
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="访问端点"
            name="endpoint"
            example="ssh://user@192.168.1.50"
            extra="本地 Docker 留空即可（缺省 local）；远端主机需可免密 SSH 访问其 Docker 服务"
            tooltip="本地执行主机使用平台自带 Docker，留空即可；远端主机填写 SSH 连接串（端口缺省 22）"
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="实例上限"
            name="max_instances"
            extra="缺省 2，范围 1–64"
            tooltip="该主机可同时承载的 Agent 容器数上限"
          >
            <InputNumber min={1} max={64} style={{ width: '100%' }} />
          </FieldWithHints>
          <FieldWithHints
            label="网段池"
            name="subnets"
            example="10.101.0.0/16"
            extra="回车添加多个"
            tooltip="分配给该主机上实验室网络的子网池"
          >
            <Select mode="tags" open={false} suffixIcon={null} />
          </FieldWithHints>
        </Form>
      </Modal>
    </div>
  );
}
