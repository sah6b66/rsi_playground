// SUT 列表页（DD-14 §5.6/§5.8，T7）：列表 + 创建 Modal（G5 镜像接入）+
// 验证镜像（GET /suts/{id}/verify-image）+ 删除确认；?image_ref= 预填。
import { Button, Form, Input, Modal, Radio, Space, Table, Tag } from 'antd';
import type { TableProps } from 'antd';
import type { Sut } from '../../api/hooks';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  useCreateSut,
  useDeleteSut,
  useSuts,
  useVerifyImage,
  type ImageCheckResult,
} from '../../api/hooks';
import { DangerConfirm } from '../../components/DangerConfirm';
import { FieldWithHints } from '../../components/FieldWithHints';

interface SutForm {
  name: string;
  image_ref: string;
  carrier: string;
  description?: string;
}

export function SutListPage() {
  const { data, isLoading } = useSuts();
  const createMut = useCreateSut();
  const delMut = useDeleteSut();
  const verifyMut = useVerifyImage();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(false);
  const [delTarget, setDelTarget] = useState<{ id: string; name: string } | null>(null);
  const [verifyResult, setVerifyResult] = useState<ImageCheckResult | null>(null);
  // 验证状态仅存前端会话内存（SutOut 无 verify 字段）——刷新后回"未验证"
  const [verified, setVerified] = useState<Record<string, boolean>>({});
  const [form] = Form.useForm<SutForm>();

  const presetImageRef = params.get('image_ref') ?? '';

  useEffect(() => {
    if (presetImageRef) setCreateOpen(true);
  }, [presetImageRef]);

  const badge = (id: string) => {
    const v = verified[id];
    if (v === undefined) return <Tag>未验证</Tag>;
    return v ? (
      <Tag color="green">镜像已验证</Tag>
    ) : (
      <Tag color="red">验证未通过</Tag>
    );
  };

  const columns: TableProps<Sut>['columns'] = [
      { title: 'ID', dataIndex: 'id', width: 140 },
      {
        title: '名称',
        dataIndex: 'name',
        render: (v: string, r) => (
          <a
            onClick={() => navigate(`/suts/${r.id}`)}
            data-testid={`sut-link-${r.id}`}
          >
            {v}
          </a>
        ),
      },
      {
        title: '镜像',
        render: (_, r) => (
          <Space>
            <code>{r.image_ref}</code>
            {badge(r.id)}
          </Space>
        ),
      },
      { title: '载体', dataIndex: 'carrier', width: 110 },
      {
        title: '操作',
        width: 220,
        render: (_, r) => (
          <Space>
            <Button
              size="small"
              loading={verifyMut.isPending && verifyMut.variables === r.id}
              onClick={() =>
                verifyMut.mutate(r.id, {
                  onSuccess: (res) => {
                    setVerified((m) => ({ ...m, [r.id]: !!res.ok }));
                    setVerifyResult(res);
                  },
                })
              }
            >
              验证镜像
            </Button>
            <Button size="small" danger onClick={() => setDelTarget({ id: r.id, name: r.name })}>
              删除
            </Button>
          </Space>
        ),
      },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button
        type="primary"
        onClick={() => {
          form.setFieldsValue(
            presetImageRef ? { carrier: 'opencode', image_ref: presetImageRef } : { carrier: 'opencode' },
          );
          setCreateOpen(true);
        }}
      >
        创建 SUT
      </Button>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        open={createOpen}
        title="创建 SUT"
        okText="创建"
        cancelText="取消"
        onOk={() => form.submit()}
        confirmLoading={createMut.isPending}
        onCancel={() => {
          setCreateOpen(false);
          if (presetImageRef) navigate('/suts', { replace: true });
        }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ carrier: 'opencode' }}
          onFinish={(v) => {
            createMut.mutate(v, {
              onSuccess: () => {
                setCreateOpen(false);
                form.resetFields();
              },
            });
          }}
        >
          <FieldWithHints
            label="名称"
            required
            example="ipops-agent"
            name="name"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="镜像引用"
            required
            example="ipops-agent:v1.2"
            extra="需先 docker load 到执行主机"
            tooltip="G5 镜像接入：创建后可在列表行执行“验证镜像”（docker inspect + run --version）"
            name="image_ref"
            rules={[{ required: true, message: '请输入镜像引用' }]}
          >
            <Input placeholder="ipops-agent:v1.2" />
          </FieldWithHints>
          <FieldWithHints label="载体" required name="carrier">
            <Radio.Group>
              <Radio value="opencode">OpenCode</Radio>
            </Radio.Group>
          </FieldWithHints>
          <FieldWithHints label="描述" name="description">
            <Input.TextArea rows={2} />
          </FieldWithHints>
        </Form>
      </Modal>

      <DangerConfirm
        open={!!delTarget}
        title="删除 SUT"
        name={delTarget?.name ?? ''}
        description="删除后其 staging 文件与版本引用将不可恢复。"
        loading={delMut.isPending}
        onOk={() => {
          if (delTarget) delMut.mutate(delTarget.id);
          setDelTarget(null);
        }}
        onCancel={() => setDelTarget(null)}
      />

      <Modal
        open={!!verifyResult}
        title={`镜像验证：${verifyResult?.image_ref ?? ''}`}
        footer={null}
        onCancel={() => setVerifyResult(null)}
        destroyOnHidden
        data-testid="verify-result-modal"
      >
        {verifyResult && (
          <div>
            <p>
              整体结果：{' '}
              {verifyResult.ok === null ? (
                <Tag>未知（执行主机离线）</Tag>
              ) : verifyResult.ok ? (
                <Tag color="green">通过</Tag>
              ) : (
                <Tag color="red">未通过</Tag>
              )}
            </p>
            <ul data-testid="verify-checks">
              {verifyResult.checks.map((c, i) => (
                <li key={i}>
                  {verifyResult.ok === null ? '—' : verifyResult.ok ? '✓' : '✗'} {c}
                </li>
              ))}
            </ul>
            {verifyResult.detail && (
              <p style={{ color: 'rgba(0,0,0,0.45)' }}>{verifyResult.detail}</p>
            )}
          </div>
        )}
      </Modal>
    </Space>
  );
}
