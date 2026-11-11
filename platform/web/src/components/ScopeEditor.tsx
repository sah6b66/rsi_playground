// U7b 优化范围编辑器（创建 Modal 与详情「优化范围」Tab 共用）：
// 8 个目标域 Checkbox + 勾选域下可细化路径模式（tags 输入，留空=整个域可优化）。
import { Checkbox, Select, Space, Typography } from 'antd';
import { TARGET_PATHS } from '../utils/targets';
import type { ScopeSelection } from '../utils/targets';

export function ScopeEditor({
  value,
  onChange,
}: {
  value: ScopeSelection;
  onChange: (v: ScopeSelection) => void;
}) {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        勾选允许 RSI 自动优化的目标域；可在域内进一步细化路径模式（如{' '}
        <code>bgp-diag/</code>），留空=整个域可优化。
      </Typography.Text>
      {TARGET_PATHS.map((t) => {
        const checked = value[t.key] !== undefined;
        return (
          <div key={t.key} data-testid={`scope-domain-${t.key}`}>
            <Checkbox
              checked={checked}
              onChange={(e) =>
                onChange({ ...value, [t.key]: e.target.checked ? [] : undefined })
              }
            >
              {t.desc}
            </Checkbox>
            {checked && (
              <Select
                mode="tags"
                allowClear
                style={{ width: '100%', marginTop: 4 }}
                placeholder="bgp-diag/（留空=整个域可优化）"
                value={value[t.key]}
                onChange={(v: string[]) => onChange({ ...value, [t.key]: v })}
                data-testid={`scope-patterns-${t.key}`}
              />
            )}
          </div>
        );
      })}
    </Space>
  );
}
