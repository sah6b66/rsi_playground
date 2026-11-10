import { Tag } from 'antd';
import { statusMeta } from '../utils/constants';

export function StatusBadge({ value }: { value: string }) {
  const meta = statusMeta(value);
  return <Tag color={meta.color}>{meta.label}</Tag>;
}
