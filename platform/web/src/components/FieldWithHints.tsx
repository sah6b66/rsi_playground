// 表单四件套封装（DD-14 §6）：required *、placeholder=样例、extra=缺省值+范围、tooltip=说明+出处
import { Form, Tooltip } from 'antd';
import type { FormItemProps } from 'antd';
import { QuestionCircleOutlined } from '@ant-design/icons';
import type { ReactNode } from 'react';
import { cloneElement, isValidElement } from 'react';

export interface FieldWithHintsProps {
  label: string;
  required?: boolean;
  example?: string; // placeholder 样例
  extra?: string; // 缺省值 + 范围
  tooltip?: string; // 说明 + 出处（DD 条款）
  name?: FormItemProps['name'];
  rules?: FormItemProps['rules'];
  valuePropName?: FormItemProps['valuePropName'];
  children: ReactNode;
}

export function FieldWithHints({
  label,
  required,
  example,
  extra,
  tooltip,
  name,
  rules,
  valuePropName,
  children,
}: FieldWithHintsProps) {
  const labelNode = tooltip ? (
    <span>
      {label}&nbsp;
      <Tooltip title={tooltip}>
        <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
      </Tooltip>
    </span>
  ) : (
    label
  );
  const child =
    example && isValidElement(children)
      ? // 给唯一子元素注入 placeholder（若是 antd 输入组件）
        cloneElement(children, { placeholder: example })
      : children;
  return (
    <Form.Item
      label={labelNode}
      required={required}
      extra={extra}
      name={name}
      rules={required ? rules ?? [{ required: true, message: `请输入${label}` }] : rules}
      valuePropName={valuePropName}
    >
      {child}
    </Form.Item>
  );
}
