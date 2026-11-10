// 参数 Tab（U2 重构）：中文名+描述为主列，内部 path 收进 tooltip 供排障；
// 分组标题中文；"可热更/需重启" Tag 替代原 mutable 文案。
import { Input, InputNumber, Select, Space, Spin, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import {
  useSystemSettings,
  useUpdateSetting,
  type SettingItem,
} from '../../../api/hooks';
import {
  GROUP_LABELS,
  settingLabel,
  settingScope,
  type SettingScope,
} from '../../../utils/settingsLabels';

// 分组映射：按配置章节前缀归组（前端常量；未匹配 → 其他）
const SETTINGS_GROUPS: [string, string[]][] = [
  ['server', ['server.']],
  ['database', ['database.', 'db.']],
  ['git', ['git.']],
  ['storage', ['storage.']],
  ['logging', ['logging.']],
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
  ['scheduler', ['scheduler.', 'queue.', 'seed.', 'platform.']],
];

function groupOf(path: string): string {
  for (const [name, prefixes] of SETTINGS_GROUPS) {
    if (prefixes.some((p) => path.startsWith(p))) return name;
  }
  return 'other';
}

// 值类型中文（用户第 4 轮反馈第 3 条）；未覆盖类型回落原文
const TYPE_LABELS: Record<string, string> = {
  number: '数值',
  string: '字符串',
  boolean: '布尔',
  list: '列表',
  array: '列表',
  dict: '字典',
  object: '字典',
  null: '空值',
};

// 适用层级 → Tag 配置（第 4 条 b）；agent/session 行描述加缺省值后缀
const SCOPE_TAGS: Record<SettingScope, { label: string; color?: string; suffix: string }> = {
  system: { label: '系统级', color: 'blue', suffix: '' },
  agent: { label: '网络Agent 缺省', color: 'cyan', suffix: '（作为未单独配置时的缺省值）' },
  session: { label: 'RSI 会话缺省', color: 'purple', suffix: '（作为未单独配置时的缺省值）' },
};

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
    {
      title: '配置项',
      render: (_, r) => {
        const label = settingLabel(r.path);
        return (
          <Tooltip title={`内部标识：${r.path}`} placement="right">
            <span data-testid={`label-${r.path}`}>
              <b>{label.name}</b>
              {label.desc && (
                <span
                  style={{
                    display: 'block',
                    fontSize: 12,
                    color: 'rgba(0,0,0,0.45)',
                  }}
                >
                  {label.desc}
                  {SCOPE_TAGS[settingScope(r.path)].suffix}
                </span>
              )}
            </span>
          </Tooltip>
        );
      },
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
      title: '值类型',
      width: 80,
      render: (_, r) => TYPE_LABELS[typeOf(r.value)] ?? typeOf(r.value),
    },
    {
      title: '适用层级',
      width: 140,
      render: (_, r) => {
        const meta = SCOPE_TAGS[settingScope(r.path)];
        return (
          <Tooltip title={meta.suffix || '平台全局配置，对所有会话与 Agent 生效'}>
            <Tag color={meta.color} data-testid={`scope-${r.path}`}>
              {meta.label}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      title: '缺省值',
      width: 120,
      render: (_, r) => {
        const def = settingLabel(r.path).def;
        if (def === null) return '—（不限）';
        return def === undefined || (Array.isArray(def) && def.length === 0)
          ? '—'
          : String(def);
      },
    },
    {
      title: '在线修改',
      width: 110,
      render: (_, r) =>
        r.mutable ? (
          <Tooltip title="修改后立即生效，无需重启">
            <Tag color="green" data-testid={`mut-${r.path}`}>可热更</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="属于静态配置，修改配置文件并重启平台后生效">
            <Tag data-testid={`mut-${r.path}`}>需重启</Tag>
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
          title={() => <b>{GROUP_LABELS[g.group] ?? g.group}</b>}
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
