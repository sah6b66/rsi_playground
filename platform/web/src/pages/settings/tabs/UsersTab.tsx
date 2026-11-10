// 用户管理 Tab（admin，DD-14 §5.10⑥）：列表/创建（5 角色）/禁用 Switch。
// users.py 实际端点：GET/POST /users、PUT /users/{user_id}（role/disabled）；
// 无 reset-password 端点、无 created_at 字段——按后端实际对齐。
import { Alert, Button, Form, Input, Modal, Select, Space, Switch, Table, Tag } from 'antd';
import { useState } from 'react';
import { useCreateUser, useUpdateUser, useUsers } from '../../../api/hooks';
import { FieldWithHints } from '../../../components/FieldWithHints';
import { ROLES, ROLE_LABEL, type Role } from '../../../utils/permissions';

const ROLE_COLORS: Record<string, string> = {
  admin: 'red',
  agent_engineer: 'blue',
  researcher: 'green',
  approver: 'orange',
  service: 'purple',
};

interface UserForm {
  username: string;
  role: Role;
  password: string;
}

export function UsersTab() {
  const { data, isLoading } = useUsers();
  const createMut = useCreateUser();
  const updateMut = useUpdateUser();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<UserForm>();

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space>
        <Button type="primary" onClick={() => setOpen(true)}>
          创建用户
        </Button>
        {updateMut.isError && updateMut.error && (
          <Alert type="error" showIcon message={updateMut.error.message} closable />
        )}
      </Space>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={false}
        columns={[
          { title: '用户名', dataIndex: 'username' },
          {
            title: '角色',
            dataIndex: 'role',
            width: 140,
            render: (v: string) => (
              <Tag color={ROLE_COLORS[v] ?? 'default'}>
                {ROLE_LABEL[v as Role] ?? v}
              </Tag>
            ),
          },
          {
            title: '禁用',
            dataIndex: 'disabled',
            width: 100,
            render: (v: boolean, r) => (
              <Switch
                checked={v}
                checkedChildren="已禁用"
                unCheckedChildren="启用"
                loading={updateMut.isPending && updateMut.variables?.id === r.id}
                onChange={(disabled) => updateMut.mutate({ id: r.id, disabled })}
              />
            ),
          },
          { title: '用户 ID', dataIndex: 'id' },
        ]}
      />

      <Modal
        open={open}
        title="创建用户"
        okText="创建"
        cancelText="取消"
        confirmLoading={createMut.isPending}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ role: 'researcher' }}
          onFinish={(v) => {
            createMut.mutate(v, {
              onSuccess: () => {
                setOpen(false);
                form.resetFields();
              },
            });
          }}
        >
          <FieldWithHints
            label="用户名"
            required
            example="eng-1"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="角色"
            required
            name="role"
            tooltip="admin=管理员 / agent_engineer=Agent 工程师 / researcher=研究员 / approver=审批人 / service=服务账号"
          >
            <Select
              options={ROLES.map((r) => ({
                value: r,
                label: `${ROLE_LABEL[r]}（${r}）`,
              }))}
            />
          </FieldWithHints>
          <FieldWithHints
            label="密码"
            required
            name="password"
            extra="至少 8 位"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 8, message: '至少 8 位' },
            ]}
          >
            <Input.Password />
          </FieldWithHints>
        </Form>
      </Modal>
    </Space>
  );
}
