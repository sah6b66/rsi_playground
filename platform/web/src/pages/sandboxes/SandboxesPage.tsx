// 仿真沙箱管理（DD-14 §5.8，T6）：平台卡片 + 探活 + 设备端点聚合 + 新增/编辑平台
import {
  Alert,
  Badge,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
  Row,
  Col,
} from 'antd';
import {
  CloudOutlined,
  CloudServerOutlined,
  ExperimentOutlined,
  HddOutlined,
  MinusCircleOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  useBackends,
  useEvalsets,
  useSaveBackend,
  type BackendStatus,
} from '../../api/hooks';
import { FieldWithHints } from '../../components/FieldWithHints';
import { QueryErrorResult } from '../../components/QueryErrorResult';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

const KIND_LABEL: Record<string, string> = {
  'ensp-pro': 'eNSP Pro',
  pnetlab: 'PNETLab',
  physical: '物理网络',
  fake: 'Fake',
};

// 类型图标（四态）：仿真平台=云、物理网络=桌面/服务器、Fake=烧瓶
const KIND_ICON: Record<string, ReactNode> = {
  'ensp-pro': <CloudServerOutlined />,
  pnetlab: <CloudOutlined />,
  physical: <HddOutlined />,
  fake: <ExperimentOutlined />,
};

// env.backend（如 ensp-pro）↔ 平台键（如 ensp-pro-1）：按键前缀匹配
function backendKeyMatches(key: string, envBackend?: string | null): boolean {
  if (!envBackend) return false;
  return key === envBackend || key.startsWith(`${envBackend}-`);
}

// 设备清单行（表单态）↔ 提交态（name → {ip, port}）
interface DeviceRow {
  name?: string;
  ip?: string;
  port?: number;
}

function devicesToRows(
  devices?: Record<string, { ip: string; port: number }>,
): DeviceRow[] {
  return Object.entries(devices ?? {}).map(([name, ep]) => ({
    name,
    ip: ep.ip,
    port: ep.port,
  }));
}

function rowsToDevices(rows: DeviceRow[] | undefined) {
  const out: Record<string, { ip: string; port: number }> = {};
  for (const r of rows ?? []) {
    if (r.name && r.ip) out[r.name] = { ip: r.ip, port: r.port ?? 22 };
  }
  return out;
}

export function SandboxesPage() {
  const { data, error } = useBackends();
  const user = useUiStore((s) => s.user);
  const canManage = can(user?.role, 'sandbox:manage');
  const { data: evalsets } = useEvalsets();
  const saveMut = useSaveBackend();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const [form] = Form.useForm<{
    key: string;
    kind: string;
    url: string;
    verify_tls: boolean;
    subnets: string[];
    devices: DeviceRow[];
  }>();
  // physical 类型隐藏 URL 输入（在线判定=设备 SSH 可达，无需平台地址）
  const formKind = Form.useWatch('kind', form);
  const formUrl = Form.useWatch('url', form);
  // 设备清单编辑器：物理网络类型必显；其他类型仅在 URL 为空时显示
  const showDevices = formKind === 'physical' || !formUrl;

  const openEdit = (b?: BackendStatus) => {
    form.setFieldsValue({
      key: b?.name ?? '',
      kind: b?.kind ?? 'fake',
      url: '', // GET 不回传 URL（静态配置隐私面），编辑时留空重填
      verify_tls: false,
      subnets: [],
      devices: devicesToRows(b?.devices),
    });
    setOpen(true);
  };

  if (error) {
    return <QueryErrorResult error={error} needPerm="view:read" />;
  }

  return (
    <div>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        网络仿真平台
      </Typography.Title>
      <Space style={{ marginBottom: 16 }}>
        {canManage && (
          <a role="button" onClick={() => openEdit()}>
            新增平台
          </a>
        )}
        <Button
          size="small"
          onClick={() => {
            void qc.invalidateQueries({ queryKey: ['backends'] });
            void message.info('已刷新探活状态');
          }}
        >
          手动刷新
        </Button>
      </Space>
      {saved && (
        <Alert
          data-testid="restart-alert"
          type="info"
          showIcon
          message="已写入配置，重启平台后生效"
          description="平台清单属于静态配置，保存后需重启平台才会生效；运行中的沙箱服务不会自动重建"
          closable
          style={{ marginBottom: 16 }}
        />
      )}
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {(data?.items ?? []).map((b) => {
          const related = (evalsets?.items ?? []).filter((e) =>
            backendKeyMatches(b.name, e.env?.backend),
          );
          return (
            <Card
              key={b.name}
              title={
                <Space>
                  <b>{b.name}</b>
                  <Tag color="purple">
                    <Space size={4}>
                      {KIND_ICON[b.kind]}
                      {KIND_LABEL[b.kind] ?? b.kind}
                    </Space>
                  </Tag>
                  {b.kind === 'fake' && <Tag>离线开发用</Tag>}
                </Space>
              }
              style={{ width: '100%' }}
              extra={
                <Space>
                  <Badge
                    status={b.available ? 'success' : 'error'}
                    text={b.available ? '在线' : '离线'}
                  />
                  {canManage && (
                    <a role="button" onClick={() => openEdit(b)}>
                      编辑
                    </a>
                  )}
                </Space>
              }
            >
              <Row gutter={16}>
                <Col span={10}>
                  <Descriptions column={1} size="small">
                    <Descriptions.Item label="类型">
                      {KIND_LABEL[b.kind] ?? b.kind}
                    </Descriptions.Item>
                    <Descriptions.Item label="状态详情">
                      {b.detail ?? (b.available ? '探活正常' : '不可用')}
                    </Descriptions.Item>
                    {(b.device_count ?? 0) > 0 && (
                      <Descriptions.Item label="设备清单">
                        <span data-testid="device-count">
                          设备 {b.device_count} 台
                        </span>
                      </Descriptions.Item>
                    )}
                    <Descriptions.Item label="活跃实验室">
                      {b.active_labs}
                    </Descriptions.Item>
                  </Descriptions>
                </Col>
                <Col span={14}>
                  <Collapse
                    ghost
                    items={[
                      {
                        key: 'endpoints',
                        label: `设备端点（关联评测集 ${related.length}）`,
                        children: related.length ? (
                          related.map((e) => (
                            <div key={e.id} style={{ marginBottom: 8 }}>
                              <Typography.Text strong>{e.name}</Typography.Text>{' '}
                              <Link to="/evalsets">去编辑凭证</Link>
                              <div style={{ paddingLeft: 12 }}>
                                {Object.entries(e.env?.ssh_access ?? {}).map(
                                  ([dev, sa]) => (
                                    <div key={dev} data-testid="device-endpoint">
                                      {dev}: {sa.ip}:{sa.port}
                                    </div>
                                  ),
                                )}
                              </div>
                            </div>
                          ))
                        ) : (
                          <Empty
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                            description="暂无评测集使用该平台"
                          />
                        ),
                      },
                    ]}
                  />
                </Col>
              </Row>
            </Card>
          );
        })}
      </Space>

      <Modal
        open={open}
        title="新增 / 编辑平台"
        okText="保存"
        confirmLoading={saveMut.isPending}
        onCancel={() => setOpen(false)}
        onOk={async () => {
          const v = await form.validateFields();
          try {
            await saveMut.mutateAsync({
              key: v.key,
              kind: v.kind,
              url: v.url ?? '',
              verify_tls: v.verify_tls,
              subnets: v.subnets ?? [],
              devices: rowsToDevices(v.devices),
            });
          } catch {
            return;
          }
          setOpen(false);
          setSaved(true);
        }}
      >
        <Form form={form} layout="vertical">
          <FieldWithHints
            label="平台标识"
            required
            name="key"
            example="ensp-pro-1"
            extra="仅小写字母、数字与连字符；创建后不可修改"
            tooltip="用于在平台内唯一标识这台网络仿真平台，评测集按名称前缀匹配到它"
          >
            <Input />
          </FieldWithHints>
          <FieldWithHints label="类型" required name="kind">
            <Select
              options={[
                {
                  value: 'ensp-pro',
                  label: (
                    <Space>
                      <CloudServerOutlined />
                      eNSP Pro
                    </Space>
                  ),
                },
                {
                  value: 'pnetlab',
                  label: (
                    <Space>
                      <CloudOutlined />
                      PNETLab
                    </Space>
                  ),
                },
                {
                  value: 'physical',
                  label: (
                    <Space>
                      <HddOutlined />
                      物理网络
                    </Space>
                  ),
                },
                {
                  value: 'fake',
                  label: (
                    <Space>
                      <ExperimentOutlined />
                      Fake（离线开发）
                    </Space>
                  ),
                },
              ]}
            />
          </FieldWithHints>
          {formKind === 'physical' ? (
            <Typography.Text
              type="secondary"
              style={{ display: 'block', marginBottom: 16 }}
            >
              物理网络无需平台地址，以设备 SSH 可达判断在线
            </Typography.Text>
          ) : (
            <FieldWithHints
              label="URL"
              name="url"
              example="https://192.168.56.101:8443"
              tooltip="网络仿真平台的网页管理入口地址，平台通过此地址监控它是否在线"
            >
              <Input />
            </FieldWithHints>
          )}
          <FieldWithHints
            label="校验 TLS 证书"
            name="verify_tls"
            valuePropName="checked"
            extra="缺省关闭（多数仿真平台使用自签名证书）"
            tooltip="平台使用自签名证书时保持关闭，否则探活会因证书校验失败而误报离线"
          >
            <Switch />
          </FieldWithHints>
          <FieldWithHints
            label="网段"
            name="subnets"
            example="10.101.0.0/16"
            extra="回车添加多个"
            tooltip="该平台实验室使用的子网池"
          >
            <Select mode="tags" open={false} suffixIcon={null} />
          </FieldWithHints>
          {showDevices && (
            <div data-testid="device-editor" style={{ marginBottom: 8 }}>
              <Typography.Text strong>设备清单</Typography.Text>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                平台在线判定：任一设备 SSH 可达即在线（与在用实验室无关）。
                设备名需与评测集中的设备名一致；登录凭证仍在评测集中配置。
              </Typography.Paragraph>
              <Form.List name="devices">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...restField }) => (
                      <Space
                        key={key}
                        style={{ display: 'flex', marginBottom: 4 }}
                        align="baseline"
                      >
                        <Form.Item
                          {...restField}
                          name={[name, 'name']}
                          style={{ marginBottom: 4 }}
                        >
                          <Input placeholder="R1" style={{ width: 100 }} />
                        </Form.Item>
                        <Form.Item
                          {...restField}
                          name={[name, 'ip']}
                          style={{ marginBottom: 4 }}
                        >
                          <Input placeholder="192.168.56.10" style={{ width: 160 }} />
                        </Form.Item>
                        <Form.Item
                          {...restField}
                          name={[name, 'port']}
                          initialValue={22}
                          style={{ marginBottom: 4 }}
                        >
                          <InputNumber min={1} max={65535} placeholder="22" />
                        </Form.Item>
                        <MinusCircleOutlined onClick={() => remove(name)} />
                      </Space>
                    ))}
                    <Button
                      type="dashed"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() => add({ port: 22 })}
                    >
                      添加设备
                    </Button>
                  </>
                )}
              </Form.List>
            </div>
          )}
        </Form>
      </Modal>
    </div>
  );
}
