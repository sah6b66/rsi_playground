// 环境状态面板（用户第 5 轮反馈 1a）：状态灯 + 中文名 + 中文状态 + 延迟/失败原因。
// 后端 dependencies 接口不变；本面板隐藏已被上方资源指标卡覆盖的项
// （docker/docker:*/lab/instances/platform:*——其中 platform:* 的 0ms 延迟费解）。
import { Empty, List, Spin } from 'antd';
import { useDependencies, type Dependency } from '../api/hooks';
import { depDetailText, depLabel, depStatusText } from '../utils/labels';

const DOT: Record<string, string> = {
  ok: '#52c41a',
  down: '#ff4d4f',
};

const HIDDEN_DEP_EXACT = new Set(['docker', 'lab', 'instances']);
const HIDDEN_DEP_PREFIXES = ['docker:', 'platform:'];

export function isHiddenDep(name: string): boolean {
  return (
    HIDDEN_DEP_EXACT.has(name) ||
    HIDDEN_DEP_PREFIXES.some((p) => name.startsWith(p))
  );
}

function dotColor(dep: Dependency): string {
  if (dep.status === 'down' || dep.status === 'error') return DOT.down!;
  if (dep.status === 'ok' || dep.status === 'up') return DOT.ok!;
  return '#8c8c8c'; // 未配置/未知 → 灰
}

export function DependencyPanel() {
  const { data, isLoading } = useDependencies();
  if (isLoading) return <Spin />;
  const items = (data?.items ?? []).filter((d) => !isHiddenDep(d.name));
  if (items.length === 0) {
    return <Empty description="暂无依赖检查数据" />;
  }
  return (
    <List
      size="small"
      dataSource={items}
      renderItem={(dep) => {
        const ok = dep.status === 'ok' || dep.status === 'up';
        return (
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
            <span style={{ flex: 1 }}>
              {depLabel(dep.name)}
              <span
                style={{
                  marginLeft: 8,
                  fontSize: 12,
                  color: ok ? '#389e0d' : '#8c8c8c',
                }}
              >
                {depStatusText(dep.status)}
              </span>
            </span>
            <span style={{ color: '#8c8c8c', fontSize: 12 }}>
              {!ok && dep.detail
                ? depDetailText(dep.detail)
                : dep.latency_ms != null
                  ? `${dep.latency_ms}ms`
                  : '—'}
            </span>
          </List.Item>
        );
      }}
    />
  );
}
