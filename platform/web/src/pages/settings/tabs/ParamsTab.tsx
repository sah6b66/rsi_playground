// 参数 Tab（DD-14 §5.10①）：按 settings.yaml 章节分组；动态项（mutable=true）
// 行内编辑 → PUT /system/settings/{path} → "已热生效"；静态项锁标 +
// tooltip "修改 settings.yaml 后重启"。列四件套对齐（缺省/范围后端未
// 暴露时显示 —）。
import { Badge, Input, InputNumber, Select, Space, Spin, Table, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { LockOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import {
  useSystemSettings,
  useUpdateSetting,
  type SettingItem,
} from '../../../api/hooks';

// 分组映射：按 settings.yaml.example 章节前缀归组（前端常量；未匹配 → 其他）
const SETTINGS_GROUPS: [string, string[]][] = [
  ['server', ['server.']],
  ['database', ['database.', 'db.']],
  ['git', ['git.']],
  ['storage', ['storage.']],
  ['sandbox', ['sandbox.']],
  ['carrier', ['carrier.']],
  ['instrumentor', ['instrumentor.']],
  ['tracing', ['tracing.']],
  ['judge', ['judge.', 'eval.']],
  ['optimizer', ['optimizer.', 'hypothesis.', 'attribution.']],
  ['decision', ['decision.', 'promote.', 'critic.']],
  ['orchestrator', ['orchestrator.', 'iteration.', 'session.', 'budget.']],
  ['llm', ['llm.', 'model.']],
  ['security', ['security.', 'auth.', 'audit.']],
  ['scheduler', ['scheduler.', 'queue.']],
  ['seed', ['seed.', 'platform.']],
];

function groupOf(path: string): string {
  for (const [name, prefixes] of SETTINGS_GROUPS) {
    if (prefixes.some((p) => path.startsWith(p))) return name;
  }
  return 'other';
}

const typeOf = (v: unknown): string =>
  v === null
    ? 'null'
    : Array.isArray(v)
      ? 'array'
      : typeof v === 'number'
        ? 'number'
        : typeof v === 'boolean'
          ? 'boolean'
          : typeof v === 'object'
            ? 'object'
            : 'string';

// 按类型渲染可编辑控件（number/boolean/string；object 仅 JSON 文本）
function ValueEditor({
  item,
  onSave,
  saving,
}: {
  item: SettingItem;
  onSave: (value: unknown) => void;
  saving: boolean;
}) {
  const t = typeOf(item.value);
  const [text, setText] = useState(
    t === 'object' || t === 'array' ? JSON.stringify(item.value, null, 2) : '',
  );
  if (t === 'number') {
    return (
      <InputNumber
        data-testid={`edit-${item.path}`}
        size="small"
        defaultValue={item.value as number}
        disabled={saving}
        onBlur={(e) => {
          const raw = e.target.value;
          if (raw.trim() === '') return;
          const n = Number(raw);
          onSave(Number.isNaN(n) ? raw : n);
        }}
      />
    );
  }
  if (t === 'boolean') {
    return (
      <Select
        size="small"
        style={{ width: 90 }}
        defaultValue={item.value ? 'true' : 'false'}
        disabled={saving}
        onChange={(v) => onSave(v === 'true')}
        options={[
          { value: 'true', label: 'true' },
          { value: 'false', label: 'false' },
        ]}
      />
    );
  }
  if (t === 'object' || t === 'array') {
    return (
      <Input.TextArea
        rows={2}
        value={text}
        disabled={saving}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          try {
            onSave(JSON.parse(text));
          } catch {
            /* 非法 JSON：失焦不保存 */
          }
        }}
      />
    );
  }
  return (
    <Input
      data-testid={`edit-${item.path}`}
      size="small"
      defaultValue={String(item.value)}
      disabled={saving}
      onBlur={(e) => onSave(e.target.value)}
    />
  );
}

export function ParamsTab() {
  const { data, isLoading } = useSystemSettings();
  const mut = useUpdateSetting();

  const groups = useMemo(() => {
    const byGroup = new Map<string, SettingItem[]>();
    (data?.items ?? []).forEach((it) => {
      const g = groupOf(it.path);
      byGroup.set(g, [...(byGroup.get(g) ?? []), it]);
    });
    return [...byGroup.entries()].map(([group, items]) => ({
      key: group,
      group,
      items,
    }));
  }, [data]);

  const columns: ColumnsType<SettingItem> = [
    { title: '键', dataIndex: 'path' },
    {
      title: '类型',
      width: 80,
      render: (_, r) => typeOf(r.value),
    },
    {
      title: '当前值',
      dataIndex: 'value',
      render: (_, r) =>
        r.mutable ? (
          <ValueEditor
            key={String(r.value)}
            item={r}
            saving={mut.isPending}
            onSave={(value) =>
              mut.mutate({ path: r.path, value })
            }
          />
        ) : (
          <span data-testid={`static-${r.path}`}>
            {typeof r.value === 'object' ? JSON.stringify(r.value) : String(r.value)}
          </span>
        ),
    },
    {
      title: '缺省值',
      width: 100,
      render: () => '—', // 后端 SettingItem 未暴露缺省值/范围（U9 以后端补充为准）
    },
    {
      title: '范围/可选值',
      width: 120,
      render: () => '—',
    },
    {
      title: '生效方式',
      width: 150,
      render: (_, r) =>
        r.mutable ? (
          <Tooltip title="修改后立即生效，无需重启">
            <Badge status="processing" text={<Space size={4}><ThunderboltOutlined />动态</Space>} />
          </Tooltip>
        ) : (
          <Tooltip title="修改 settings.yaml 后重启">
            <Badge status="default" text={<Space size={4}><LockOutlined />静态</Space>} />
          </Tooltip>
        ),
    },
  ];

  if (isLoading) return <Spin />;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {groups.map((g) => (
        <Table
          key={g.key}
          title={() => <b>{g.group}</b>}
          rowKey="path"
          size="small"
          dataSource={g.items}
          columns={columns}
          pagination={false}
        />
      ))}
    </Space>
  );
}
