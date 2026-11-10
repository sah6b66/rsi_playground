// 凭证 Tab（admin，DD-14 §5.10④）：值永不回显——CredentialOut 无 secret/
// 指纹字段，列表展示密钥库引用 id；rotate 503（store 未配置）行内提示。
import { Alert, Button, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import { useState } from 'react';
import {
  CREDENTIAL_KINDS,
  useCreateCredential,
  useCredentials,
  useRotateCredential,
} from '../../../api/hooks';
import { FieldWithHints } from '../../../components/FieldWithHints';

interface CredForm {
  name: string;
  kind: (typeof CREDENTIAL_KINDS)[number];
  secret: string;
}

// 轮换 Modal：新值 + 输入凭证名确认（危险操作规范 DD-14 §6）
function RotateModal({
  target,
    onClose,
}: {
  target: { id: string; name: string } | null;
  onClose: () => void;
}) {
  const [form] = Form.useForm<{ secret: string; confirm: string }>();
  const mut = useRotateCredential();
  if (!target) return null;
  return (
    <>
      {mut.isError && mut.error && (
        <Alert type="error" showIcon message={mut.error.message} closable />
      )}
      <Modal
        open
        title={`轮换凭证 ${target.name}`}
        okText="确认轮换"
        okButtonProps={{ danger: true }}
        confirmLoading={mut.isPending}
        onCancel={() => {
          form.resetFields();
          onClose();
        }}
        destroyOnHidden
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(v) =>
            mut.mutate(
              { id: target.id, secret: v.secret },
              {
                onSuccess: () => {
                  form.resetFields();
                  onClose();
                },
              },
            )
          }
        >
          <FieldWithHints
            label="新值"
            required
            name="secret"
            extra="轮换后旧值立即失效，请确认已同步所有使用方"
          >
            <Input.Password />
          </FieldWithHints>
          <FieldWithHints
            label="确认"
            required
            name="confirm"
            extra={`请输入 ${target.name} 以确认`}
            rules={[
              { required: true, message: '请输入凭证名确认' },
              {
                validator: (_, v) =>
                  v === target.name
                    ? Promise.resolve()
                    : Promise.reject(new Error('输入与凭证名不一致')),
              },
            ]}
          >
            <Input placeholder={target.name} />
          </FieldWithHints>
        </Form>
      </Modal>
    </>
  );
}

export function CredsTab() {
  const { data, isLoading } = useCredentials();
  const createMut = useCreateCredential();
  const [createOpen, setCreateOpen] = useState(false);
  const [rotateTarget, setRotateTarget] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [form] = Form.useForm<CredForm>();

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Button type="primary" onClick={() => setCreateOpen(true)}>
          创建凭证
        </Button>
        <span style={{ color: 'rgba(0,0,0,0.45)' }}>
          凭证值加密存入密钥库，界面永不回显明文
        </span>
      </Space>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          {
            title: '类型',
            dataIndex: 'kind',
            width: 140,
            render: (v: string) => <Tag>{v}</Tag>,
          },
          {
            title: '密钥库引用',
            dataIndex: 'id',
            render: (v: string) => <code>{v}</code>,
          },
          { title: '创建时间', dataIndex: 'created_at', width: 220 },
          {
            title: '操作',
            width: 120,
            render: (_, r) => (
              <Button
                size="small"
                danger
                onClick={() => setRotateTarget({ id: r.id, name: r.name })}
              >
                轮换
              </Button>
            ),
          },
        ]}
      />

      <Modal
        open={createOpen}
        title="创建凭证"
        okText="创建"
        cancelText="取消"
        confirmLoading={createMut.isPending}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
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
            example="core-sw-ssh"
            name="name"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="类型"
            required
            name="kind"
            tooltip="device_ssh=设备 SSH / nms_api=网管 API / llm_api=模型 API / backend_ssh=后端 SSH"
          >
            <Select options={CREDENTIAL_KINDS.map((k) => ({ value: k, label: k }))} />
          </FieldWithHints>
          <FieldWithHints
            label="值"
            required
            name="secret"
            extra="提交后仅回显密钥库引用，明文不再可见"
          >
            <Input.Password />
          </FieldWithHints>
        </Form>
      </Modal>

      <RotateModal target={rotateTarget} onClose={() => setRotateTarget(null)} />
    </Space>
  );
}
