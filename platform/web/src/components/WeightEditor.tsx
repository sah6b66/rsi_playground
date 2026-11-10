// 权重编辑器（DD-14 §5.5 并入，T3）：指标/方向/权重滑块 + 实时公式预览 + 总和校验。
import { Alert, Button, Select, Slider, Switch } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';

export interface Goal {
  metric: string;
  direction: 'max' | 'min';
  weight: number;
}

// 与后端 MetricDef（rsi/optimizer/metrics）对齐的 8 项指标常量
export const METRIC_DEFS: { value: string; label: string }[] = [
  { value: 'accuracy', label: '准确率' },
  { value: 'token_cost', label: 'Token 消耗' },
  { value: 'latency', label: '时延' },
  { value: 'final_state_match', label: '终态匹配' },
  { value: 'safety_pass', label: '安全红线通过' },
  { value: 'root_cause_match', label: '根因命中' },
  { value: 'checklist_pass', label: '检查单命中' },
  { value: 'efficiency', label: '效率' },
];

const metricLabel = (v: string) =>
  METRIC_DEFS.find((m) => m.value === v)?.label ?? v;

// 公式预览：S = 0.70×(+Δaccuracy/σ) + 0.30×(−Δtoken_cost/σ)
export function scoreFormula(goals: Goal[]): string {
  if (goals.length === 0) return 'S = （未配置指标）';
  const terms = goals
    .map(
      (g) =>
        `${g.weight.toFixed(2)}×(${g.direction === 'max' ? '+' : '−'}Δ${g.metric}/σ)`,
    )
    .join(' + ');
  return `S = ${terms}`;
}

export function WeightSumHint({ goals }: { goals: Goal[] }) {
  const sum = goals.reduce((s, g) => s + g.weight, 0);
  if (goals.length === 0 || Math.abs(sum - 1) < 1e-9) return null;
  return (
    <Alert
      data-testid="weight-sum-warning"
      type="warning"
      showIcon
      message={`权重和为 ${sum.toFixed(2)}，建议为 1.00（不阻止提交）`}
    />
  );
}

interface Props {
  goals: Goal[];
  onChange: (goals: Goal[]) => void;
}

export function WeightEditor({ goals, onChange }: Props) {
  const update = (i: number, patch: Partial<Goal>) =>
    onChange(goals.map((g, idx) => (idx === i ? { ...g, ...patch } : g)));

  return (
    <div data-testid="weight-editor">
      {goals.map((goal, i) => (
        <div
          key={i}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 8,
          }}
        >
          <Select
            style={{ width: 160 }}
            value={goal.metric}
            options={METRIC_DEFS}
            onChange={(metric) => update(i, { metric })}
            aria-label={`指标-${i}`}
          />
          <span title="方向：开=最大化(max)，关=最小化(min)">
            {goal.direction === 'max' ? '↑max' : '↓min'}
            <Switch
              style={{ marginInline: 4 }}
              size="small"
              checked={goal.direction === 'max'}
              checkedChildren="↑"
              unCheckedChildren="↓"
              onChange={(max) =>
                update(i, { direction: max ? 'max' : 'min' })
              }
              aria-label={`方向-${i}`}
            />
          </span>
          <Slider
            style={{ flex: 1, minWidth: 120, marginInline: 8 }}
            min={0}
            max={1}
            step={0.05}
            value={goal.weight}
            onChange={(weight) => update(i, { weight })}
            aria-label={`权重-${i}`}
          />
          <span style={{ width: 44, textAlign: 'right' }}>
            {goal.weight.toFixed(2)}
          </span>
          <Button
            type="text"
            danger
            size="small"
            icon={<DeleteOutlined />}
            onClick={() => onChange(goals.filter((_, idx) => idx !== i))}
            aria-label={`删除指标-${i}`}
          />
        </div>
      ))}
      <Button
        size="small"
        icon={<PlusOutlined />}
        onClick={() =>
          onChange([
            ...goals,
            { metric: 'token_cost', direction: 'min', weight: 0.3 },
          ])
        }
      >
        添加指标
      </Button>
      <div
        data-testid="formula-preview"
        style={{
          marginTop: 8,
          padding: '4px 8px',
          background: '#fafafa',
          fontFamily: 'monospace',
        }}
      >
        公式预览：{scoreFormula(goals)}（σ 为基线噪声标准差，归一化后加权求和）
        <span style={{ marginLeft: 8, color: '#8c8c8c' }}>
          指标：{goals.map((g) => `${metricLabel(g.metric)}`).join('、') || '—'}
        </span>
      </div>
      <div style={{ marginTop: 8 }}>
        <WeightSumHint goals={goals} />
      </div>
    </div>
  );
}
