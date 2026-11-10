// 危险操作确认（DD-14 §6）：输入对象名等于 name 才允许 onOk
import { Input, Modal } from 'antd';
import { useState } from 'react';

interface Props {
  open: boolean;
  title: string;
  name: string; // 需要输入确认的对象名（SUT/评测集/镜像/会话）
  description?: string;
  onOk: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export function DangerConfirm({
  open,
  title,
  name,
  description,
  onOk,
  onCancel,
  loading,
}: Props) {
  const [value, setValue] = useState('');
  return (
    <Modal
      open={open}
      title={title}
      okText="确认执行"
      okButtonProps={{ danger: true, disabled: value !== name }}
      onOk={() => {
        onOk();
        setValue('');
      }}
      onCancel={() => {
        setValue('');
        onCancel();
      }}
      confirmLoading={loading}
      destroyOnClose
    >
      {description && <p>{description}</p>}
      <p>
        请输入 <b>{name}</b> 以确认：
      </p>
      <Input
        data-testid="danger-confirm-input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={name}
      />
    </Modal>
  );
}
