// 创建会话向导（DD-14 §5.3）：Steps 3 步 → POST /iterations → 跳列表。
import { Alert, Button, Card, Descriptions, Select, Space, Steps, Tag } from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError } from '../../api/client';
import {
  useCreateSession,
  useEnvironments,
  useEvalsets,
  useSuts,
} from '../../api/hooks';
import { ProfileForm, defaultProfile, type ProfileDraft } from '../../components/ProfileForm';
import { scoreFormula } from '../../components/WeightEditor';

interface Basics {
  sut_id: string | null;
  environment: string | null;
  evalset_id: string | null;
}

const DEFAULT_BASICS: Basics = { sut_id: null, environment: null, evalset_id: null };

export function CreateSessionWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [basics, setBasics] = useState<Basics>(DEFAULT_BASICS);
  const [profile, setProfile] = useState<ProfileDraft>(defaultProfile());
  const [error, setError] = useState<string | null>(null);
  const create = useCreateSession();

  const suts = useSuts();
  const envs = useEnvironments();
  const evalsets = useEvalsets();

  const sutOptions = (suts.data?.items ?? []).map((s) => ({
    value: s.id,
    label: s.name,
  }));
  const envOptions = (envs.data?.items ?? []).map((h) => ({
    value: h.key,
    label: h.key,
  }));
  const evalOptions = (evalsets.data?.items ?? []).map((e) => ({
    value: e.id,
    label: e.name,
  }));

  const step1Valid = basics.sut_id && basics.environment && basics.evalset_id;
  const sutName =
    suts.data?.items.find((s) => s.id === basics.sut_id)?.name ?? basics.sut_id;

  const submit = async () => {
    setError(null);
    try {
      await create.mutateAsync({
        sut_id: basics.sut_id!,
        evalset_id: basics.evalset_id ?? undefined,
        mode: profile.mode,
        profile: {
          goals: profile.goals,
          guards: profile.guards,
          budget_tokens: profile.budget_tokens,
          evalsets: profile.evalsets,
        },
      });
      navigate('/sessions');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '创建失败');
    }
  };

  return (
    <Card title="创建会话" size="small">
      <Steps
        current={step}
        items={[{ title: '基础' }, { title: '目标与策略' }, { title: '确认' }]}
      />
      <div style={{ marginTop: 16 }}>
        {step === 0 && (
          <Space direction="vertical" size="middle" style={{ width: 360 }}>
            <Select
              placeholder="SUT *"
              value={basics.sut_id ?? undefined}
              options={sutOptions}
              onChange={(v) => setBasics({ ...basics, sut_id: v })}
              aria-label="SUT"
            />
            {basics.sut_id && (
              <span style={{ color: '#8c8c8c' }}>
                镜像：
                {suts.data?.items.find((s) => s.id === basics.sut_id)?.image_ref}
              </span>
            )}
            <Select
              placeholder="运行环境 *"
              value={basics.environment ?? undefined}
              options={envOptions}
              onChange={(v) => setBasics({ ...basics, environment: v })}
              aria-label="运行环境"
            />
            <Select
              placeholder="迭代评测集 *"
              value={basics.evalset_id ?? undefined}
              options={evalOptions}
              onChange={(v) => setBasics({ ...basics, evalset_id: v })}
              aria-label="评测集"
            />
          </Space>
        )}
        {step === 1 && <ProfileForm value={profile} onChange={setProfile} />}
        {step === 2 && (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="SUT">{sutName}</Descriptions.Item>
            <Descriptions.Item label="运行环境">
              {basics.environment}
            </Descriptions.Item>
            <Descriptions.Item label="评测集">
              {basics.evalset_id}
            </Descriptions.Item>
            <Descriptions.Item label="模式">
              <Tag>{profile.mode}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="目标公式">
              {scoreFormula(profile.goals)}
            </Descriptions.Item>
            <Descriptions.Item label="预算（tokens）">
              {profile.budget_tokens?.toLocaleString()}
            </Descriptions.Item>
          </Descriptions>
        )}
      </div>

      {error && (
        <Alert
          data-testid="create-error"
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message={error}
        />
      )}

      <div style={{ marginTop: 16 }}>
        <Space>
          <Button disabled={step === 0} onClick={() => setStep(step - 1)}>
            上一步
          </Button>
          {step < 2 ? (
            <Button
              type="primary"
              disabled={step === 0 && !step1Valid}
              onClick={() => setStep(step + 1)}
            >
              下一步
            </Button>
          ) : (
            <Button
              type="primary"
              loading={create.isPending}
              onClick={() => void submit()}
            >
              提交创建
            </Button>
          )}
        </Space>
      </div>
    </Card>
  );
}
