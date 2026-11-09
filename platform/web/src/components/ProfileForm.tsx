// 目标与策略表单（DD-14 §5.3 创建向导 Step2）：WeightEditor + 守卫 + 预算 +
// mode（P2 未开放）+ 评测集三绑定。
import { Checkbox, Form, InputNumber, Radio, Select } from 'antd';
import { useEvalsets } from '../api/hooks';
import { WeightEditor, type Goal } from './WeightEditor';

export interface ProfileDraft {
  mode: string;
  goals: Goal[];
  guards: {
    missed_check_worsen: boolean;
    safety_any_worsen: boolean;
    high_risk_actions: boolean;
  };
  budget_tokens: number | null;
  evalsets: { iterate: string | null; smoke: string | null; holdout: string | null };
}

export const defaultProfile = (): ProfileDraft => ({
  mode: 'p1',
  goals: [{ metric: 'accuracy', direction: 'max', weight: 0.7 }],
  guards: { missed_check_worsen: true, safety_any_worsen: true, high_risk_actions: true },
  budget_tokens: 1_000_000,
  evalsets: { iterate: null, smoke: null, holdout: null },
});

const GUARDS: { key: keyof ProfileDraft['guards']; label: string }[] = [
  { key: 'missed_check_worsen', label: 'missed_check 恶化即否决' },
  { key: 'safety_any_worsen', label: 'safety 任意恶化即否决' },
  { key: 'high_risk_actions', label: '高危动作守卫' },
];

interface Props {
  value: ProfileDraft;
  onChange: (draft: ProfileDraft) => void;
}

export function ProfileForm({ value, onChange }: Props) {
  const evalsets = useEvalsets();
  const options = (evalsets.data?.items ?? []).map((e) => ({
    value: e.id,
    label: e.name,
  }));

  return (
    <div data-testid="profile-form">
      <Form layout="vertical" component="div">
        <Form.Item
          label="优化目标（权重编辑器）"
          required
          tooltip="S = Σ wᵢ·(±Δmᵢ/σᵢ)；σ 取基线噪声标准差（DD-14 §5.5）"
        >
          <WeightEditor
            goals={value.goals}
            onChange={(goals) => onChange({ ...value, goals })}
          />
        </Form.Item>

        <Form.Item label="守卫（任一触发即否决该轮）">
          {GUARDS.map((g) => (
            <Checkbox
              key={g.key}
              style={{ marginRight: 16 }}
              checked={value.guards[g.key]}
              onChange={(e) =>
                onChange({
                  ...value,
                  guards: { ...value.guards, [g.key]: e.target.checked },
                })
              }
            >
              {g.label}
            </Checkbox>
          ))}
        </Form.Item>

        <Form.Item
          label="预算上限（tokens）"
          extra="缺省 1,000,000 token，范围 10,000–100,000,000"
        >
          <InputNumber
            min={10_000}
            max={100_000_000}
            step={10_000}
            value={value.budget_tokens}
            onChange={(v) => onChange({ ...value, budget_tokens: v })}
            style={{ width: 200 }}
          />
        </Form.Item>

        <Form.Item label="迭代模式" tooltip="P0=人工门全开；P1=半自动（四门）；P2 未开放">
          <Radio.Group
            value={value.mode}
            onChange={(e) => onChange({ ...value, mode: e.target.value })}
          >
            <Radio value="p0">P0 人工</Radio>
            <Radio value="p1">P1 半自动</Radio>
            <Radio value="p2" disabled>
              P2 全自动（未开放）
            </Radio>
          </Radio.Group>
        </Form.Item>

        <Form.Item label="评测集绑定" required tooltip="iterate 迭代集必选；smoke/holdout 可选">
          <Select
            placeholder="迭代集 iterate *"
            style={{ width: 240, marginRight: 8 }}
            value={value.evalsets.iterate ?? undefined}
            options={options}
            onChange={(v) =>
              onChange({ ...value, evalsets: { ...value.evalsets, iterate: v } })
            }
            aria-label="迭代集"
          />
          <Select
            placeholder="冒烟集 smoke（可选）"
            style={{ width: 240, marginRight: 8 }}
            allowClear
            value={value.evalsets.smoke ?? undefined}
            options={options}
            onChange={(v) =>
              onChange({ ...value, evalsets: { ...value.evalsets, smoke: v ?? null } })
            }
            aria-label="冒烟集"
          />
          <Select
            placeholder="留出集 holdout（可选）"
            style={{ width: 240 }}
            allowClear
            value={value.evalsets.holdout ?? undefined}
            options={options}
            onChange={(v) =>
              onChange({ ...value, evalsets: { ...value.evalsets, holdout: v ?? null } })
            }
            aria-label="留出集"
          />
        </Form.Item>
      </Form>
    </div>
  );
}
