// 外部依赖面板（DD-14 §5.2）：状态灯 + 名称 + 延迟/失败原因。
import { Empty, List, Spin } from 'antd';
import { useDependencies, type Dependency } from '../api/hooks';
import { depLabel } from '../utils/labels';

const DOT: Record<string, string> = {
  ok: '#52c41a',
  down: '#ff4d4f',
};

function dotColor(dep: Dependency): string {
  return DOT[dep.status] ?? '#8c8c8c'; // 未配置/未知 → 灰
}

export function DependencyPanel() {
  const { data, isLoading } = useDependencies();
  if (isLoading) return <Spin />;
  const items = data?.items ?? [];
  if (items.length === 0) {
    return <Empty description="暂无依赖检查数据" />;
  }
  return (
    <List
      size="small"
      dataSource={items}
      renderItem={(dep) => (
        <List.Item>
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: dotColor(dep),
              marginRight: 8,
            }}
          />
          <span style={{ flex: 1 }}>{depLabel(dep.name)}</span>
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>
            {dep.status !== 'ok' && dep.detail
              ? dep.detail
              : dep.latency_ms != null
                ? `${dep.latency_ms}ms`
                : '—'}
          </span>
        </List.Item>
      )}
    />
  );
}
