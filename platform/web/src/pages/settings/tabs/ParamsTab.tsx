// 参数 Tab（C2）：全部用户级键可在线编辑并落盘；
// 动态键即时生效，静态键保存后标「待重启」Badge；
// 页顶汇总待重启变更数 + 平台重启按钮；LLM 提供商半脱敏直配区。
import {
  Alert,
  Badge,
  Button,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import {
  useLlmConfig,
  useRestartPlatform,
  useSystemSettings,
  useUpdateLlmProvider,
  useUpdateSetting,
  type LlmProviderDto,
  type SettingItem,
} from '../../../api/hooks';
import { DangerConfirm } from '../../../components/DangerConfirm';
import { useUiStore } from '../../../stores/ui';
import {
  GROUP_LABELS,
  settingLabel,
  settingScope,
  type SettingScope,
} from '../../../utils/settingsLabels';

// 热更键（后端 _DYNAMIC_PATHS 同构快照）：修改即时生效、无需重启
const HOT_KEYS = new Set([
  'carrier.task_timeout_s',
  'carrier.max_instances_per_host',
  'logging.level',
  'llm.defaults.judge.model',
  // 运行时键不落盘，天然即时生效
  'budget.llm.daily',
]);

// 分组映射（C2：按用户使用频率排序）
const SETTINGS_GROUPS: [string, string[]][] = [
  ['llm', ['llm.']],
  ['judge', ['judge.']],
  ['optimizer', ['optimizer.']],
  ['decision', ['decision.']],
  ['orchestrator', ['orchestrator.']],
  ['carrier', ['carrier.']],
  ['sandbox', ['sandbox.']],
  ['tracing', ['tracing.']],
  ['logging', ['logging.']],
  ['database', ['database.']],
  ['server', ['server.']],
  ['seed', ['seed.']],
];

function groupOf(path: string): string {
  for (const [name, prefixes] of SETTINGS_GROUPS) {
    if (prefixes.some((p) => path.startsWith(p))) return name;
  }
  return 'other';
}

// 值类型中文；未覆盖类型回落原文
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

// 适用层级 → Tag 配置；agent/session 行描述加缺省值后缀
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

// LLM 提供商编辑行：api_key 半脱敏显示，编辑时清空输入新值；base_url 可改
function LlmProviderRow({
  provider,
  onSave,
  saving,
}: {
  provider: LlmProviderDto;
  onSave: (v: { api_key?: string; base_url?: string }) => void;
  saving: boolean;
}) {
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  return (
    <Space wrap data-testid={`llm-provider-${provider.name}`}>
      <Tag color="geekblue">{provider.name}</Tag>
      <Tooltip title="留空表示不修改；当前密钥仅显示尾 3 位">
        <Input.Password
          data-testid={`llm-key-${provider.name}`}
          style={{ width: 220 }}
          size="small"
          placeholder={provider.api_key_masked || '未配置密钥'}
          value={apiKey}
          disabled={saving}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </Tooltip>
      <Input
        data-testid={`llm-url-${provider.name}`}
        style={{ width: 300 }}
        size="small"
        placeholder="服务地址（留空用官方地址）"
        value={baseUrl}
        disabled={saving}
        onChange={(e) => setBaseUrl(e.target.value)}
      />
      <Button
        size="small"
        type="primary"
        disabled={saving}
        onClick={() =>
          onSave({
            api_key: apiKey !== '' ? apiKey : undefined,
            base_url: baseUrl,
          })
        }
      >
        保存
      </Button>
    </Space>
  );
}

export function ParamsTab() {
  const { data, isLoading } = useSystemSettings();
  const mut = useUpdateSetting();
  const llm = useLlmConfig();
  const llmMut = useUpdateLlmProvider();
  const restart = useRestartPlatform();
  const role = useUiStore((s) => s.user?.role);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const pendingCount = useMemo(
    () => (data?.items ?? []).filter((it) => it.pending_restart).length,
    [data],
  );

  const groups = useMemo(() => {
    const byGroup = new Map<string, SettingItem[]>();
    (data?.items ?? []).forEach((it) => {
      const g = groupOf(it.path);
      byGroup.set(g, [...(byGroup.get(g) ?? []), it]);
    });
    // 按 SETTINGS_GROUPS 声明顺序输出（使用频率排序）；未匹配键归入「其他」垫底
    const ordered = SETTINGS_GROUPS.filter(([g]) => byGroup.has(g)).map(
      ([group]) => ({ key: group, group, items: byGroup.get(group)! }),
    );
    if (byGroup.has('other')) {
      ordered.push({ key: 'other', group: 'other', items: byGroup.get('other')! });
    }
    return ordered;
  }, [data]);

  const doRestart = () => {
    setConfirmOpen(false);
    restart.mutate(undefined, {
      onSuccess: () => {
        void message.success('平台正在重启，页面将在几秒后自动刷新');
        setTimeout(() => window.location.reload(), 5000);
      },
      onError: (err) => void message.error(err.message),
    });
  };

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
            onSave={(value) => mut.mutate({ path: r.path, value })}
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
      title: '生效方式',
      width: 150,
      render: (_, r) =>
        !r.mutable ? (
          <Tooltip title="属于静态配置，不允许在线修改">
            <Tag data-testid={`mut-${r.path}`}>只读</Tag>
          </Tooltip>
        ) : HOT_KEYS.has(r.path) ? (
          <Tooltip title="修改后立即生效，无需重启">
            <Tag color="green" data-testid={`mut-${r.path}`}>可热更</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="已保存到配置文件，重启平台后生效">
            <span data-testid={`mut-${r.path}`}>
              {r.pending_restart ? (
                <Badge status="warning" text={<span style={{ color: '#d48806' }}>待重启</span>} />
              ) : (
                <Tag>需重启生效</Tag>
              )}
            </span>
          </Tooltip>
        ),
    },
  ];

  if (isLoading) return <Spin />;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {pendingCount > 0 && (
        <Alert
          type="warning"
          showIcon
          data-testid="pending-restart-alert"
          message={`待重启变更 ${pendingCount} 项：已保存到配置文件，重启平台后生效`}
          action={
            role === 'admin' && (
              <Button danger size="small" onClick={() => setConfirmOpen(true)}>
                重启平台
              </Button>
            )
          }
        />
      )}
      <DangerConfirm
        open={confirmOpen}
        title="重启平台"
        name="重启平台"
        description="重启将中断所有运行中的评测批次；已保存的配置变更将在重启后生效。"
        loading={restart.isPending}
        onOk={doRestart}
        onCancel={() => setConfirmOpen(false)}
      />
      <Table
        title={() => <b>{GROUP_LABELS['llm']}</b>}
        rowKey="name"
        size="small"
        loading={llm.isLoading}
        dataSource={llm.data?.providers ?? []}
        pagination={false}
        columns={[
          {
            title: '模型服务提供商',
            render: (_, r: LlmProviderDto) => (
              <span data-testid={`llm-label-${r.name}`}>
                <b>模型服务</b>
                <span style={{ display: 'block', fontSize: 12, color: 'rgba(0,0,0,0.45)' }}>
                  直连密钥与地址（半脱敏显示；留空密钥表示不修改）
                </span>
              </span>
            ),
          },
          {
            title: 'API 密钥 / 服务地址',
            render: (_, r: LlmProviderDto) => (
              <LlmProviderRow
                key={`${r.name}-${r.api_key_masked}-${r.base_url}`}
                provider={r}
                saving={llmMut.isPending}
                onSave={(v) => llmMut.mutate({ name: r.name, ...v })}
              />
            ),
          },
        ]}
      />
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
