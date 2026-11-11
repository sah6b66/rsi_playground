// TraceTimeline（DD-14 §7）：虚拟滚动事件列表 + 行展开 JSON 树。
import { Tag, Tree, Typography } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import type { TraceEventDto } from '../api/hooks';
import { eventTypeLabel } from '../utils/labels';

export type { TraceEventDto };

const EVENT_COLOR: Record<string, string> = {
  tool_call: 'geekblue',
  tool_result: 'cyan',
  llm_request: 'purple',
  llm_response: 'magenta',
  run_started: 'green',
  run_finalized: 'green',
};

function relTs(events: TraceEventDto[], ev: TraceEventDto): string {
  const base = events[0] ? Date.parse(events[0].ts) : NaN;
  const t = Date.parse(ev.ts);
  if (Number.isNaN(base) || Number.isNaN(t)) return ev.ts;
  return `+${Math.max(0, t - base)}ms`;
}

function summarize(ev: TraceEventDto): string {
  const d = ev.data ?? {};
  if (ev.event_type === 'tool_call' && typeof d.cli === 'string') {
    return d.cli.slice(0, 60);
  }
  if (ev.event_type === 'llm_request') {
    const sys = d.system;
    const len =
      typeof sys === 'string'
        ? sys.length
        : Array.isArray(sys)
          ? sys.join('\n').length
          : JSON.stringify(sys ?? '').length;
    return `system ${len} 字符`;
  }
  const keys = Object.keys(d);
  return keys.length ? keys.join(', ') : '—';
}

function toJsonTree(value: unknown, key: string, depth: number): DataNode {
  if (value !== null && typeof value === 'object') {
    const children = Array.isArray(value)
      ? value.map((v, i) => toJsonTree(v, String(i), depth + 1))
      : Object.entries(value as Record<string, unknown>).map(([k, v]) =>
          toJsonTree(v, k, depth + 1),
        );
    return {
      title: `${key}: ${Array.isArray(value) ? `[${value.length}]` : '{…}'}`,
      key: `${key}#${depth}`,
      children,
    };
  }
  return { title: `${key}: ${JSON.stringify(value)}`, key: `${key}#${depth}` };
}

export function TraceTimeline({
  events,
  height = 600,
}: {
  events: TraceEventDto[];
  height?: number;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 40,
    overscan: 8,
  });

  const toggle = (seq: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

  if (!events.length)
    return (
      <Typography.Text type="secondary" data-testid="trace-empty">
        该 run 无轨迹事件
      </Typography.Text>
    );

  return (
    <div
      ref={parentRef}
      data-testid="trace-timeline"
      style={{ height, overflow: 'auto', border: '1px solid #f0f0f0', padding: 4 }}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
        {virtualizer.getVirtualItems().map((vi) => {
          const ev = events[vi.index];
          const open = expanded.has(ev.seq);
          return (
            <div
              key={ev.seq}
              data-testid={`trace-row-${ev.seq}`}
              ref={virtualizer.measureElement}
              data-index={vi.index}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${vi.start}px)`,
              }}
            >
              <div
                data-testid={`trace-toggle-${ev.seq}`}
                role="button"
                style={{ cursor: 'pointer', padding: '4px 0' }}
                onClick={() => toggle(ev.seq)}
              >
                <Tag color="blue" style={{ minWidth: 28, textAlign: 'center' }}>
                  {ev.seq}
                </Tag>
                <Tag color={EVENT_COLOR[ev.event_type] ?? 'default'}>
                  {eventTypeLabel(ev.event_type)}
                </Tag>
                <Typography.Text type="secondary" style={{ marginRight: 8 }}>
                  {relTs(events, ev)}
                </Typography.Text>
                <Typography.Text>{summarize(ev)}</Typography.Text>
              </div>
              {open && (
                <Tree
                  data-testid={`trace-json-${ev.seq}`}
                  defaultExpandAll={false}
                  defaultExpandedKeys={['data#0']}
                  treeData={[toJsonTree(ev.data ?? {}, 'data', 0)]}
                  style={{ marginBottom: 8 }}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
