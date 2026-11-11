// 目标与策略表单（DD-14 §5.3 创建向导 Step2）：WeightEditor + 守卫 + 预算 +
// mode（P2 未开放）+ 评测集三绑定。
import { Checkbox, Form, InputNumber, Radio, Select } from 'antd';
import { useEvalsets, useSystemSettings } from '../api/hooks';
import { FieldWithHints } from './FieldWithHints';
import { WeightEditor, type Goal } from './WeightEditor';

export interface ProfileDraft {
  mode: string;
  judge_model: string | null;
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
  judge_model: null,
  goals: [{ metric: 'accuracy', direction: 'max', weight: 0.7 }],
  guards: { missed_check_worsen: true, safety_any_worsen: true, high_risk_actions: true },
  budget_tokens: 1_000_000,
  evalsets: { iterate: null, smoke: null, holdout: null },
});

// 迭代模式选项（用户第 4 轮反馈第 2 条）：p2 未开放禁用。
const MODE_OPTIONS = [
  {
    value: 'p0',
    label: '人工模式',
    desc: '方向 / 假设 / 预审 / 决策四步均由人工执行与确认',
  },
  {
    value: 'p1',
    label: '半自动模式',
    desc: '自动执行迭代流程，四个关键节点（门）等待人工审批',
  },
  {
    value: 'p2',
    label: '全自动模式（未开放）',
    desc: '系统自主迭代，暂未开放',
    disabled: true,
  },
];

const GUARDS: { key: keyof ProfileDraft['guards']; label: string }[] = [
  { key: 'missed_check_worsen', label: 'missed_check 恶化即否决' },
  { key: 'safety_any_worsen', label: 'safety 任意恶化即否决' },
  { key: 'high_risk_actions', label: '高危动作守卫' },
];

interface Props {
  value: ProfileDraft;
  onChange: (draft: ProfileDraft) => void;
}

// 裁判模型候选：从系统参数的 LLM 段提取模型名；
// 取不到时静态兜底（引导到系统参数页配置）。
function useJudgeModelOptions() {
  const settings = useSystemSettings();
  const models = new Set<string>();
  (settings.data?.items ?? []).forEach((it) => {
    const isModelKey =
      /^llm\.defaults\.[^.]+\.model$/.test(it.path) || it.path === 'judge.llm.model';
    if (isModelKey && typeof it.value === 'string' && it.value.trim()) {
      models.add(it.value.trim());
    }
  });
  const options = [...models].map((m) => ({ value: m, label: m }));
  return {
    options,
    fallback: options.length === 0,
  };
}

export function ProfileForm({ value, onChange }: Props) {
  const evalsets = useEvalsets();
  const judge = useJudgeModelOptions();
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
          tooltip="综合得分 S = Σ 权重×(各指标变化量/基线噪声)；σ 为基线噪声标准差，避免把噪声误判为提升"
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

        <Form.Item
          label="迭代模式"
          tooltip="人工模式四步均由人工；半自动模式自动执行、四个关键节点等待人工审批；全自动暂未开放"
        >
          <Radio.Group
            value={value.mode}
            onChange={(e) => onChange({ ...value, mode: e.target.value })}
            data-testid="mode-radio-group"
          >
            {MODE_OPTIONS.map((m) => (
              <Radio key={m.value} value={m.value} disabled={m.disabled}>
                {m.label}
                <span
                  style={{ fontSize: 12, color: 'rgba(0,0,0,0.45)', marginLeft: 4 }}
                >
                  （{m.desc}）
                </span>
              </Radio>
            ))}
          </Radio.Group>
        </Form.Item>

        <FieldWithHints
          label="裁判模型"
          tooltip="评测打分所用的模型，不同会话可选不同模型；未选时用系统缺省"
          extra={
            judge.fallback
              ? '暂未读取到已配置模型，可在系统参数页配置后选择；未选时用系统缺省'
              : '未选时使用系统缺省裁判模型'
          }
        >
          <Select
            allowClear
            placeholder="使用系统缺省裁判模型"
            style={{ width: 280 }}
            value={value.judge_model ?? undefined}
            options={judge.options}
            onChange={(v) => onChange({ ...value, judge_model: v ?? null })}
            aria-label="裁判模型"
            data-testid="judge-model-select"
          />
        </FieldWithHints>

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
