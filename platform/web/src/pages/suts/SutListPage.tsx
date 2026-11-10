// 网络Agent 管理页（DD-14 §5.6/§5.8，T7；U1 合并镜像库）：
// Tab1「Agent 列表」= 原 SUT 列表（创建 Modal/验证镜像/删除，?image_ref= 预填）；
// Tab2「镜像库」= 原 /images 页整体迁入（权限 image:manage，无权显示 403）。
import { Button, Form, Input, Modal, Radio, Result, Space, Table, Tabs, Tag, Tooltip, Typography } from 'antd';
import type { TableProps } from 'antd';
import type { Sut } from '../../api/hooks';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  useCreateSut,
  useDeleteSut,
  useSuts,
  useVerifyImage,
  type ImageCheckResult,
} from '../../api/hooks';
import { QuestionCircleOutlined } from '@ant-design/icons';
import { DangerConfirm } from '../../components/DangerConfirm';
import { FieldWithHints } from '../../components/FieldWithHints';
import { ScopeEditor } from '../../components/ScopeEditor';
import { ImagesPage } from '../images/ImagesPage';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';
import { scopeFromSelection } from '../../utils/targets';
import type { ScopeSelection } from '../../utils/targets';

interface SutForm {
  name: string;
  image_ref: string;
  carrier: string;
  description?: string;
}

function SutListTab({ presetImageRef }: { presetImageRef: string }) {
  const { data, isLoading } = useSuts();
  const createMut = useCreateSut();
  const delMut = useDeleteSut();
  const verifyMut = useVerifyImage();
  const navigate = useNavigate();
  const [createOpen, setCreateOpen] = useState(false);
  const [delTarget, setDelTarget] = useState<{ id: string; name: string } | null>(null);
  const [verifyResult, setVerifyResult] = useState<ImageCheckResult | null>(null);
  // 验证状态仅存前端会话内存（SutOut 无 verify 字段）——刷新后回"未验证"
  const [verified, setVerified] = useState<Record<string, boolean>>({});
  // U7b 可优化范围：自动（默认）/明确定义 + 域选择
  const [scopeMode, setScopeMode] = useState<'auto' | 'explicit'>('auto');
  const [scopeSel, setScopeSel] = useState<ScopeSelection>({});
  const [form] = Form.useForm<SutForm>();

  useEffect(() => {
    if (presetImageRef) setCreateOpen(true);
  }, [presetImageRef]);

  const badge = (id: string) => {
    const v = verified[id];
    if (v === undefined) return <Tag>未验证</Tag>;
    return v ? (
      <Tag color="green">镜像已验证</Tag>
    ) : (
      <Tag color="red">验证未通过</Tag>
    );
  };

  const columns: TableProps<Sut>['columns'] = [
      { title: 'ID', dataIndex: 'id', width: 140 },
      {
        title: '名称',
        dataIndex: 'name',
        render: (v: string, r) => (
          <a
            onClick={() => navigate(`/suts/${r.id}`)}
            data-testid={`sut-link-${r.id}`}
          >
            {v}
          </a>
        ),
      },
      {
        title: '镜像引用',
        render: (_, r) => (
          <Space>
            <code>{r.image_ref}</code>
            {badge(r.id)}
          </Space>
        ),
      },
      { title: '载体', dataIndex: 'carrier', width: 110 },
      {
        title: '操作',
        width: 220,
        render: (_, r) => (
          <Space>
            <Button
              size="small"
              loading={verifyMut.isPending && verifyMut.variables === r.id}
              onClick={() =>
                verifyMut.mutate(r.id, {
                  onSuccess: (res) => {
                    setVerified((m) => ({ ...m, [r.id]: !!res.ok }));
                    setVerifyResult(res);
                  },
                })
              }
            >
              验证镜像
            </Button>
            <Button size="small" danger onClick={() => setDelTarget({ id: r.id, name: r.name })}>
              删除
            </Button>
          </Space>
        ),
      },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Button
        type="primary"
        onClick={() => {
          form.setFieldsValue(
            presetImageRef ? { carrier: 'opencode', image_ref: presetImageRef } : { carrier: 'opencode' },
          );
          setCreateOpen(true);
        }}
      >
        创建 Agent
      </Button>
      <Table
        rowKey="id"
        size="small"
        loading={isLoading}
        dataSource={data?.items ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        open={createOpen}
        title="创建 Agent"
        okText="创建"
        cancelText="取消"
        onOk={() => form.submit()}
        confirmLoading={createMut.isPending}
        onCancel={() => {
          setCreateOpen(false);
          if (presetImageRef) navigate('/suts', { replace: true });
        }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ carrier: 'opencode' }}
          onFinish={(v) => {
            // U7b：自动模式不提交 optimization_scope（后端默认自动）；
            // 明确定义但未勾选任何域同样按自动处理（避免空范围锁死优化）
            const scope =
              scopeMode === 'explicit' ? scopeFromSelection(scopeSel) : [];
            createMut.mutate(
              scope.length ? { ...v, optimization_scope: scope } : v,
              {
                onSuccess: () => {
                  setCreateOpen(false);
                  form.resetFields();
                  setScopeMode('auto');
                  setScopeSel({});
                },
              },
            );
          }}
        >
          <FieldWithHints
            label="名称"
            required
            example="ipops-agent"
            name="name"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints
            label="镜像引用"
            required
            example="ipops-agent:v1.2"
            extra="需先 docker load 到执行主机"
            tooltip="G5 镜像接入：创建后可在列表行执行“验证镜像”（docker inspect + run --version）"
            name="image_ref"
            rules={[{ required: true, message: '请输入镜像引用' }]}
          >
            <Input placeholder="ipops-agent:v1.2" />
          </FieldWithHints>
          {/* U7a 载体：只读展示（提交值固定 opencode），不再提供 Radio 选择 */}
          <Form.Item
            label={
              <span>
                载体&nbsp;
                <Tooltip title="载体是容器内 Agent 的运行框架（提供 Agent 循环与轨迹采集）；镜像=打包了载体与插件的容器映像。当前支持 OpenCode，其他载体后续开放">
                  <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                </Tooltip>
              </span>
            }
            required
          >
            <Tag color="blue" data-testid="carrier-readonly">OpenCode</Tag>
          </Form.Item>
          <Form.Item name="carrier" hidden initialValue="opencode">
            <Input />
          </Form.Item>
          <FieldWithHints label="描述" name="description">
            <Input.TextArea rows={2} />
          </FieldWithHints>
          {/* U7b 可优化范围：自动（平台扫描后自行决定=现状）/明确定义（RSI 只碰定义项） */}
          <Form.Item
            label={
              <span>
                可优化范围&nbsp;
                <Tooltip title="自动=平台扫描 Agent 文件系统后自行决定优化哪些 skill/工具/脚本；明确定义=RSI 自动优化仅限勾选项（人工编辑不受此限制，仍受全局安全边界约束）">
                  <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                </Tooltip>
              </span>
            }
          >
            <Radio.Group
              value={scopeMode}
              onChange={(e) => setScopeMode(e.target.value)}
              data-testid="scope-mode"
            >
              <Radio value="auto">自动（由平台扫描后决定）</Radio>
              <Radio value="explicit">明确定义</Radio>
            </Radio.Group>
            {scopeMode === 'explicit' && (
              <div style={{ marginTop: 8 }}>
                <ScopeEditor value={scopeSel} onChange={setScopeSel} />
              </div>
            )}
          </Form.Item>
        </Form>
      </Modal>

      <DangerConfirm
        open={!!delTarget}
        title="删除 Agent"
        name={delTarget?.name ?? ''}
        description="删除后其 staging 文件与版本引用将不可恢复。"
        loading={delMut.isPending}
        onOk={() => {
          if (delTarget) delMut.mutate(delTarget.id);
          setDelTarget(null);
        }}
        onCancel={() => setDelTarget(null)}
      />

      <Modal
        open={!!verifyResult}
        title={`镜像验证：${verifyResult?.image_ref ?? ''}`}
        footer={null}
        onCancel={() => setVerifyResult(null)}
        destroyOnHidden
        data-testid="verify-result-modal"
      >
        {verifyResult && (
          <div>
            <p>
              整体结果：{' '}
              {verifyResult.ok === null ? (
                <Tag>未知（执行主机离线）</Tag>
              ) : verifyResult.ok ? (
                <Tag color="green">通过</Tag>
              ) : (
                <Tag color="red">未通过</Tag>
              )}
            </p>
            <ul data-testid="verify-checks">
              {verifyResult.checks.map((c, i) => (
                <li key={i}>
                  {verifyResult.ok === null ? '—' : verifyResult.ok ? '✓' : '✗'} {c}
                </li>
              ))}
            </ul>
            {verifyResult.detail && (
              <p style={{ color: 'rgba(0,0,0,0.45)' }}>{verifyResult.detail}</p>
            )}
          </div>
        )}
      </Modal>
    </Space>
  );
}

export function SutListPage() {
  const [params, setSearchParams] = useSearchParams();
  const user = useUiStore((s) => s.user);
  const canManageImages = can(user?.role, 'image:manage');
  const presetImageRef = params.get('image_ref') ?? '';
  // ?tab=images（/images 重定向入口）→ 镜像库；?image_ref= → Agent 列表（预填创建）
  const activeKey =
    params.get('tab') === 'images' && canManageImages && !presetImageRef
      ? 'images'
      : 'agents';

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        网络Agent 管理
      </Typography.Title>
      <Tabs
        activeKey={activeKey}
        onChange={(key) =>
          setSearchParams(key === 'images' ? { tab: 'images' } : {}, {
            replace: true,
          })
        }
        items={[
          {
            key: 'agents',
            label: 'Agent 列表',
            children: <SutListTab presetImageRef={presetImageRef} />,
          },
          {
            key: 'images',
            label: '镜像库',
            children: canManageImages ? (
              <ImagesPage embedded />
            ) : (
              <Result
                status="403"
                title="无权访问"
                subTitle="镜像库需要镜像管理权限（image:manage）。"
              />
            ),
          },
        ]}
      />
    </Space>
  );
}
