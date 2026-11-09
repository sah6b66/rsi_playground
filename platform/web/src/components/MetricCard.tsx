// 指标卡片（DD-14 §5.2）：紧凑 Card + 可选状态色点。
import { Card } from 'antd';
import type { ReactNode } from 'react';
import { statusMeta } from '../utils/constants';

export interface MetricCardProps {
  title: string;
  value: ReactNode;
  sub?: string;
  /** 状态键（STATUS_META），用于色点 */
  status?: string;
}

const DOT_COLORS: Record<string, string> = {
  processing: '#1677ff',
  warning: '#fa8c16',
  success: '#52c41a',
  error: '#ff4d4f',
  purple: '#722ed1',
  default: '#8c8c8c',
};

export function MetricCard({ title, value, sub, status }: MetricCardProps) {
  const dot = status ? DOT_COLORS[statusMeta(status).color] : undefined;
  return (
    <Card size="small" data-testid="metric-card">
      <div style={{ color: '#8c8c8c', fontSize: 12 }}>
        {dot && (
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: dot,
              marginRight: 6,
            }}
          />
        )}
        {title}
      </div>
      <div style={{ fontSize: 26, fontWeight: 600, lineHeight: '34px' }}>
        {value}
      </div>
      {sub && <div style={{ color: '#8c8c8c', fontSize: 12 }}>{sub}</div>}
    </Card>
  );
}
