// 评测集管理（DD-14 §5.5，T6）：列表 + 导入向导 + 克隆 + 删除 + 导出
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Tag,
  Upload,
  message,
} from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useCloneEvalset,
  useDeleteEvalset,
  useEvalsets,
  useImportEvalset,
  type Evalset,
} from '../../api/hooks';
import { getToken } from '../../api/client';
import { DangerConfirm } from '../../components/DangerConfirm';
import { FieldWithHints } from '../../components/FieldWithHints';
import { QueryErrorResult } from '../../components/QueryErrorResult';
import { can } from '../../utils/permissions';
import { useUiStore } from '../../stores/ui';

type EndpointRow = { dev: string; ip?: string; port?: number };

export function EvalsetListPage() {
  const { data, isLoading, error } = useEvalsets();
  const user = useUiStore((s) => s.user);
  const canManage = can(user?.role, 'evalset:manage');
  const delMut = useDeleteEvalset();
  const cloneMut = useCloneEvalset();
  const importMut = useImportEvalset();
  const [delTarget, setDelTarget] = useState<Evalset | null>(null);
  const [cloneTarget, setCloneTarget] = useState<Evalset | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [imported, setImported] = useState<Evalset | null>(null);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const [cloneForm] = Form.useForm<{
    new_name: string;
    endpoints: EndpointRow[];
  }>();

  const openClone = (es: Evalset) => {
    setCloneError(null);
    cloneForm.setFieldsValue({
      new_name: `${es.name}-copy`,
      endpoints: Object.entries(es.env?.ssh_access ?? {}).map(([dev, sa]) => ({
        dev,
        ip: sa.ip,
        port: sa.port,
      })),
    });
    setCloneTarget(es);
  };

  const exportEvalset = async (es: Evalset) => {
    const resp = await fetch(`/api/v1/evalsets/${es.id}:export`, {
      headers: { Authorization: `Bearer ${getToken() ?? ''}` },
    });
    if (!resp.ok) {
      void message.error('导出失败');
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${es.name}.zip`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const preview = useMemo(() => {
    if (!imported) return null;
    return {
      name: imported.name,
      task_count: imported.tasks.length,
      env_summary: imported.env
        ? `backend=${String(imported.env.backend ?? '?')}，设备 ${
            Object.keys(imported.env.ssh_access ?? {}).length
          } 台`
        : '无 env.yaml',
    };
  }, [imported]);

  if (error) {
    return <QueryErrorResult error={error} needPerm="view:read" />;
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        {canManage && (
          <Button type="primary" onClick={() => setImportOpen(true)}>
            导入评测集
          </Button>
        )}
      </Space>
      <Table
        rowKey="id"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={false}
        columns={[
          {
            title: '名称',
            dataIndex: 'name',
            // U7a：名称改为详情页链接
            render: (name: string, rec) => (
              <Link to={`/evalsets/${rec.id}`} data-testid={`evalset-link-${rec.id}`}>
                {name}
              </Link>
            ),
          },
          {
            title: '用途',
            dataIndex: 'purpose',
            render: (p: string) => <Tag>{p}</Tag>,
          },
          { title: '任务数', render: (_, rec) => rec.tasks.length },
          {
            title: '设备数',
            render: (_, rec) => Object.keys(rec.env?.ssh_access ?? {}).length,
          },
          {
            title: '冻结',
            dataIndex: 'frozen',
            render: (f: boolean) => (f ? <Tag color="blue">已冻结</Tag> : <Tag>可编辑</Tag>),
          },
          {
            title: '操作',
            render: (_, rec: Evalset) => (
              <Space>
                {canManage && (
                  <a role="button" onClick={() => openClone(rec)}>
                    克隆
                  </a>
                )}
                <a role="button" onClick={() => void exportEvalset(rec)}>
                  导出
                </a>
                {canManage && (
                  <a
                    role="button"
                    style={{ color: '#cf1322' }}
                    onClick={() => setDelTarget(rec)}
                  >
                    删除
                  </a>
                )}
              </Space>
            ),
          },
        ]}
      />

      {/* 导入向导：Dragger → :import → 预览卡 → 确认入库（导入即入库，预览确认关闭） */}
      <Modal
        open={importOpen}
        title="导入评测集（三件套 zip）"
        footer={
          imported
            ? [
                <Button key="ok" type="primary" onClick={() => setImportOpen(false)}>
                  确认入库
                </Button>,
              ]
            : null
        }
        onCancel={() => {
          setImportOpen(false);
          setImported(null);
        }}
      >
        {!imported ? (
          <Upload.Dragger
            name="file"
            accept=".zip"
            maxCount={1}
            customRequest={async ({ file, onSuccess, onError }) => {
              try {
                const out = await importMut.mutateAsync({
                  file: file as File,
                });
                setImported(out);
                onSuccess?.(out);
              } catch (e) {
                onError?.(e as Error);
              }
            }}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">拖拽 evalset zip 到此处</p>
            <p className="ant-upload-hint">env.yaml + tasks.jsonl + configs/**</p>
          </Upload.Dragger>
        ) : (
          <div data-testid="import-preview">
            <Alert type="success" message="解析并入库成功，请核对预览" />
            <Descriptions column={1} size="small" style={{ marginTop: 12 }}>
              <Descriptions.Item label="名称">{preview?.name}</Descriptions.Item>
              <Descriptions.Item label="任务数">
                {preview?.task_count}
              </Descriptions.Item>
              <Descriptions.Item label="环境">
                {preview?.env_summary}
              </Descriptions.Item>
              <Descriptions.Item label="告警">无</Descriptions.Item>
            </Descriptions>
          </div>
        )}
      </Modal>

      {/* 克隆：仅开放环境覆盖项（设备 ip/port） */}
      <Modal
        open={!!cloneTarget}
        title={`克隆评测集：${cloneTarget?.name ?? ''}`}
        okText="克隆"
        confirmLoading={cloneMut.isPending}
        onCancel={() => setCloneTarget(null)}
        onOk={async () => {
          const v = await cloneForm.validateFields();
          const device_endpoints: Record<string, { ip?: string; port?: number }> = {};
          (v.endpoints ?? []).forEach((row) => {
            if (row && row.dev && (row.ip || row.port)) {
              device_endpoints[row.dev] = { ip: row.ip, port: row.port };
            }
          });
          try {
            await cloneMut.mutateAsync({
              id: cloneTarget!.id,
              new_name: v.new_name,
              overrides: {
                ...(Object.keys(device_endpoints).length ? { device_endpoints } : {}),
              },
            });
          } catch (e) {
            setCloneError(e instanceof Error ? e.message : '克隆失败');
            return;
          }
          setCloneTarget(null);
        }}
      >
        {cloneError && (
          <Alert type="error" showIcon message={cloneError} style={{ marginBottom: 12 }} />
        )}
        <p style={{ color: 'rgba(0,0,0,0.45)' }}>
          任务集与配置文件只读复制（跨集可比性），仅环境覆盖项可修改
        </p>
        <Form form={cloneForm} layout="vertical">
          <FieldWithHints
            label="新名称"
            required
            name="new_name"
            example="netops-bgp-site2"
            tooltip="名称需唯一，与已有评测集重名将无法创建"
          >
            <Input />
          </FieldWithHints>
          {(cloneForm.getFieldValue('endpoints') as EndpointRow[] | undefined)?.length ? (
            <Form.List name="endpoints">
              {(fields) => (
                <>
                  {fields.map((field) => (
                    <Space key={field.key} align="baseline" style={{ display: 'flex' }}>
                      <Form.Item
                        name={[field.name, 'dev']}
                        label="设备"
                        style={{ marginBottom: 8 }}
                      >
                        <Input disabled />
                      </Form.Item>
                      <Form.Item
                        name={[field.name, 'ip']}
                        label="IP"
                        style={{ marginBottom: 8 }}
                      >
                        <Input style={{ width: 160 }} />
                      </Form.Item>
                      <Form.Item
                        name={[field.name, 'port']}
                        label="端口"
                        style={{ marginBottom: 8 }}
                      >
                        <InputNumber min={1} max={65535} />
                      </Form.Item>
                    </Space>
                  ))}
                </>
              )}
            </Form.List>
          ) : null}
        </Form>
      </Modal>

      <DangerConfirm
        open={!!delTarget}
        title="删除评测集"
        name={delTarget?.name ?? ''}
        description={`将删除评测集 ${delTarget?.name ?? ''}`}
        loading={delMut.isPending}
        onCancel={() => setDelTarget(null)}
        onOk={() => {
          if (delTarget) delMut.mutate(delTarget.id);
          setDelTarget(null);
        }}
      />
    </div>
  );
}
