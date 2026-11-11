// 创建会话向导（DD-14 §5.3 + ADR-27 双模式）：Steps 3 步 → POST /iterations → 跳列表。
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Input,
  Radio,
  Select,
  Space,
  Steps,
  Tag,
} from 'antd';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError } from '../../api/client';
import {
  useCreateSession,
  useEnvironments,
  useEvalsets,
  useOnlineEvalset,
  useSuts,
} from '../../api/hooks';
import { ProfileForm, defaultProfile, type ProfileDraft } from '../../components/ProfileForm';
import { FieldWithHints } from '../../components/FieldWithHints';
import { modeLabel } from '../../utils/labels';
import { scoreFormula } from '../../components/WeightEditor';

type PrepMode = 'cli' | 'api';

interface Basics {
  label: string;
  sut_id: string | null;
  environment: string | null;
  evalset_id: string | null;
  prep_mode: PrepMode;
  api_evalset_name: string;
}

const DEFAULT_BASICS: Basics = {
  label: '',
  sut_id: null,
  environment: null,
  evalset_id: null,
  prep_mode: 'cli',
  api_evalset_name: '',
};

export function CreateSessionWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [basics, setBasics] = useState<Basics>(DEFAULT_BASICS);
  const [profile, setProfile] = useState<ProfileDraft>(defaultProfile());
  const [error, setError] = useState<string | null>(null);
  const [previewName, setPreviewName] = useState('');
  const [previewError, setPreviewError] = useState<string | null>(null);
  const create = useCreateSession();

  const suts = useSuts();
  const envs = useEnvironments();
  const evalsets = useEvalsets();
  // 在线预览仅在用户点击「在线预览」后触发（previewName 非空）
  const online = useOnlineEvalset(previewName);

  const isApiMode = basics.prep_mode === 'api';

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

  const step1Valid =
    basics.label.trim().length > 0 &&
    basics.sut_id &&
    (isApiMode
      ? basics.api_evalset_name.trim().length > 0
      : basics.environment && basics.evalset_id);
  const sutName =
    suts.data?.items.find((s) => s.id === basics.sut_id)?.name ?? basics.sut_id;

  const submit = async () => {
    setError(null);
    try {
      await create.mutateAsync({
        sut_id: basics.sut_id!,
        label: basics.label.trim(),
        evalset_id: isApiMode ? undefined : basics.evalset_id ?? undefined,
        mode: profile.mode,
        prep_mode: basics.prep_mode,
        ...(isApiMode
          ? { api_evalset_name: basics.api_evalset_name.trim() }
          : {}),
        profile: {
          goals: profile.goals,
          guards: profile.guards,
          budget_tokens: profile.budget_tokens,
          evalsets: profile.evalsets,
          judge_model: profile.judge_model ?? undefined,
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
            {/* 双模式（ADR 文档）首字段：工作模式（CLI 离线 / API 在线） */}
            <FieldWithHints
              label="工作模式"
              required
              tooltip="CLI 模式：离线评估集 + 平台 SSH 环境准备（现有管道）；API 模式：在线评估集 + 网络准备 API，不在平台选择运行环境"
            >
              <Radio.Group
                value={basics.prep_mode}
                onChange={(e) =>
                  setBasics({ ...basics, prep_mode: e.target.value as PrepMode })
                }
                aria-label="工作模式"
                data-testid="prep-mode-radio"
              >
                <Radio value="cli">
                  CLI 模式（离线评估集 + 平台 SSH 环境准备）
                </Radio>
                <Radio value="api">API 模式（在线评估集 + 网络准备 API）</Radio>
              </Radio.Group>
            </FieldWithHints>
            {/* U7a 会话标识：必选 */}
            <FieldWithHints
              label="会话标识"
              required
              example="BGP排障优化-第一轮"
              extra="1-64 字符，中文可用"
              tooltip="便于人理解记忆的会话名称"
            >
              <Input
                value={basics.label}
                maxLength={64}
                aria-label="会话标识"
                onChange={(e) => setBasics({ ...basics, label: e.target.value })}
              />
            </FieldWithHints>
            <Select
              placeholder="网络Agent *"
              value={basics.sut_id ?? undefined}
              options={sutOptions}
              onChange={(v) => setBasics({ ...basics, sut_id: v })}
              aria-label="网络Agent"
            />
            {basics.sut_id && (
              <span style={{ color: '#8c8c8c' }}>
                镜像：
                {suts.data?.items.find((s) => s.id === basics.sut_id)?.image_ref}
              </span>
            )}
            {/* API 模式不选运行环境（由网络准备 API 管理） */}
            {!isApiMode && (
              <Select
                placeholder="运行环境 *"
                value={basics.environment ?? undefined}
                options={envOptions}
                onChange={(v) => setBasics({ ...basics, environment: v })}
                aria-label="运行环境"
              />
            )}
            {isApiMode ? (
              <FieldWithHints
                label="在线评估集名称"
                required
                example="bgp-faults-v2"
                tooltip="输入评估集名称，将从在线 API 获取"
              >
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    value={basics.api_evalset_name}
                    placeholder="bgp-faults-v2"
                    aria-label="在线评估集名称"
                    data-testid="api-evalset-input"
                    onChange={(e) => {
                      setBasics({
                        ...basics,
                        api_evalset_name: e.target.value,
                      });
                      setPreviewError(null);
                      if (previewName) setPreviewName('');
                    }}
                  />
                  <Button
                    data-testid="online-preview-btn"
                    disabled={!basics.api_evalset_name.trim()}
                    loading={online.isFetching}
                    onClick={() => {
                      setPreviewError(null);
                      setPreviewName(basics.api_evalset_name.trim());
                    }}
                  >
                    在线预览
                  </Button>
                </Space.Compact>
              </FieldWithHints>
            ) : (
              <Select
                placeholder="迭代评测集 *"
                value={basics.evalset_id ?? undefined}
                options={evalOptions}
                onChange={(v) => setBasics({ ...basics, evalset_id: v })}
                aria-label="评测集"
              />
            )}
            {isApiMode && online.isError && previewName && (
              <Alert
                type="error"
                showIcon
                data-testid="online-preview-error"
                message={`在线预览失败：${
                  online.error instanceof Error ? online.error.message : '未知错误'
                }`}
              />
            )}
            {isApiMode && online.data && previewName === basics.api_evalset_name.trim() && (
              <Alert
                type="success"
                showIcon
                data-testid="online-preview-result"
                message={`任务数 ${online.data.task_count} · 环境 ${
                  new Set(online.data.tasks.map((t) => t.network_env_name)).size
                } 个 · 首任务 ${
                  online.data.tasks[0]?.title || online.data.tasks[0]?.task_id || '—'
                }`}
              />
            )}
          </Space>
        )}
        {step === 1 && <ProfileForm value={profile} onChange={setProfile} />}
        {step === 2 && (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="工作模式">
              <Tag>{basics.prep_mode === 'api' ? 'API 模式' : 'CLI 模式'}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="会话标识">{basics.label}</Descriptions.Item>
            <Descriptions.Item label="网络Agent">{sutName}</Descriptions.Item>
            {!isApiMode && (
              <Descriptions.Item label="运行环境">
                {basics.environment}
              </Descriptions.Item>
            )}
            <Descriptions.Item label="评测集">
              {isApiMode ? basics.api_evalset_name : basics.evalset_id}
            </Descriptions.Item>
            <Descriptions.Item label="模式">
              <Tag>{modeLabel(profile.mode)}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="裁判模型">
              {profile.judge_model ?? '系统缺省'}
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
