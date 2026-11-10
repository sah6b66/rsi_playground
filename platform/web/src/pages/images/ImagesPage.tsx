// 镜像管理（DD-14 §5.7，T6）：按执行主机分组 + 路径导入 + 删除（被引用禁用）
import { Collapse, Form, Input, Modal, Select, Space, Spin, Table, Tag, Tooltip } from 'antd';
import type { CollapseProps } from 'antd';
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDeleteImage, useImages, useLoadImage } from '../../api/hooks';
import { DangerConfirm } from '../../components/DangerConfirm';
import { FieldWithHints } from '../../components/FieldWithHints';
import { QueryErrorResult } from '../../components/QueryErrorResult';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

export function ImagesPage() {
  const { data, isLoading, error } = useImages();
  const user = useUiStore((s) => s.user);
  const canManage = can(user?.role, 'image:manage');
  const loadMut = useLoadImage();
  const delMut = useDeleteImage();
  const navigate = useNavigate();
  const [importOpen, setImportOpen] = useState(false);
  const [form] = Form.useForm<{ host: string; tar_path: string }>();
  const [delTarget, setDelTarget] = useState<{ ref: string; host: string } | null>(
    null,
  );

  const hostKeys = useMemo(() => {
    const keys = new Set<string>();
    (data?.items ?? []).forEach((i) => keys.add(i.host));
    (data?.offline_hosts ?? []).forEach((h) => keys.add(h));
    return [...keys];
  }, [data]);

  const items: CollapseProps['items'] = hostKeys.map((host) => {
    const imgs = (data?.items ?? []).filter((i) => i.host === host);
    const offline = (data?.offline_hosts ?? []).includes(host);
    return {
      key: host,
      label: (
        <Space>
          <b>{host}</b>
          {offline ? <Tag color="red">离线</Tag> : <Tag color="green">在线</Tag>}
          <span>{imgs.length} 个镜像</span>
        </Space>
      ),
      children: (
        <Table
          rowKey="ref"
          size="small"
          dataSource={imgs}
          pagination={false}
          columns={[
            { title: '镜像', dataIndex: 'ref' },
            {
              title: '大小',
              dataIndex: 'size_mb',
              render: (v: number | null) => (v == null ? '—' : `${v} MB`),
            },
            { title: '创建时间', dataIndex: 'created_at' },
            {
              title: '被引用',
              dataIndex: 'used_by',
              render: (used: string[]) =>
                used.length ? (
                  <span>
                    {used.map((s) => (
                      <Tag key={s} color="blue">
                        {s}
                      </Tag>
                    ))}
                  </span>
                ) : (
                  <Tag>未引用</Tag>
                ),
            },
            {
              title: '操作',
              render: (_, rec: { ref: string; used_by: string[] }) => {
                const disabled = rec.used_by.length > 0 || !canManage;
                const btn = (
                  <a
                    role="button"
                    aria-label={`删除 ${rec.ref}`}
                    style={{
                      color: disabled ? 'rgba(0,0,0,0.25)' : undefined,
                      cursor: disabled ? 'not-allowed' : 'pointer',
                    }}
                    onClick={() => {
                      if (!disabled) setDelTarget({ ref: rec.ref, host });
                    }}
                  >
                    删除
                  </a>
                );
                return disabled ? (
                  <Tooltip title={`被 SUT 引用：${rec.used_by.join('、')}，不可删除`}>
                    {btn}
                  </Tooltip>
                ) : (
                  <Space>
                    {btn}
                    <a
                      role="button"
                      onClick={() =>
                        navigate(`/suts?image_ref=${encodeURIComponent(rec.ref)}`)
                      }
                    >
                      创建 SUT
                    </a>
                  </Space>
                );
              },
            },
          ]}
        />
      ),
    };
  });

  if (error) {
    return <QueryErrorResult error={error} needPerm="image:manage" />;
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        {canManage && (
          <a role="button" onClick={() => setImportOpen(true)}>
            路径导入
          </a>
        )}
        <a role="button" onClick={() => navigate('/suts')}>
          创建 SUT
        </a>
      </Space>
      {data && data.offline_hosts.length > 0 && (
        <p style={{ color: 'rgba(0,0,0,0.45)' }}>
          离线主机：{data.offline_hosts.join('、')}（Docker daemon 不可达）
        </p>
      )}
      {isLoading ? <Spin /> : <Collapse items={items} defaultActiveKey={hostKeys} />}

      <Modal
        open={importOpen}
        title="路径导入镜像"
        okText="导入"
        confirmLoading={loadMut.isPending}
        onCancel={() => setImportOpen(false)}
        onOk={async () => {
          const values = await form.validateFields();
          try {
            await loadMut.mutateAsync(values);
          } catch {
            return; // 错误已由 hook toast
          }
          form.resetFields();
          setImportOpen(false);
        }}
      >
        <Form form={form} layout="vertical">
          <FieldWithHints
            label="执行主机"
            required
            name="host"
            tooltip="镜像将导入到该主机的 Docker daemon"
          >
            <Select
              options={hostKeys.map((h) => ({ value: h, label: h }))}
              placeholder="main"
            />
          </FieldWithHints>
          <FieldWithHints
            label="tar 路径"
            required
            name="tar_path"
            example="/home/images/ipops-agent.tar"
            extra="平台运行主机上的镜像 tar 文件路径"
            tooltip="docker load 在平台侧读取 tar 并上传到目标 daemon（远端主机场景 tar 也需在平台主机上）"
          >
            <Input />
          </FieldWithHints>
        </Form>
      </Modal>

      <DangerConfirm
        open={!!delTarget}
        title="删除镜像"
        name={delTarget?.ref ?? ''}
        description={`将从主机 ${delTarget?.host} 删除该镜像`}
        loading={delMut.isPending}
        onCancel={() => setDelTarget(null)}
        onOk={() => {
          if (delTarget) delMut.mutate(delTarget);
          setDelTarget(null);
        }}
      />
    </div>
  );
}
