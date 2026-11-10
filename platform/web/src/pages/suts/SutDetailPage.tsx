// SUT 详情页（DD-14 §5.6，T7）：Descriptions 头 + lineage 版本链（列表呈现，
// DAG 可视化留后续）+ 配置文件（FileEditor）+ 版本（选两版本 DiffViewer）。
import { Alert, Button, Card, Descriptions, Empty, Radio, Select, Space, Spin, Table, Tabs, Tag } from 'antd';
import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useSut, useSutLineage, useUpdateSutScope, useVersionDiff } from '../../api/hooks';
import { DiffViewer } from '../../components/DiffViewer';
import { FileEditorTab } from './FileEditorTab';
import { ScopeEditor } from '../../components/ScopeEditor';
import { TARGET_PATHS, scopeFromSelection, selectionFromScope } from '../../utils/targets';
import type { ScopeSelection } from '../../utils/targets';

function LineagePanel({ sutId }: { sutId: string }) {
  const { data, isLoading } = useSutLineage(sutId);
  const [fromV, setFromV] = useState<string | null>(null);
  const [toV, setToV] = useState<string | null>(null);
  const diff = useVersionDiff(fromV, toV);

  if (isLoading) return <Spin />;
  const nodes = data?.nodes ?? [];
  if (!nodes.length) return <Empty description="尚无版本（保存时开启 commit 产生新版本）" />;

  const options = nodes.map((n) => ({
    value: n.version_id,
    label: `${n.label}（${n.kind}）`,
  }));

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {/* 版本链（简化 DAG：按 parent 关系列表呈现；图形化 DAG 留后续迭代） */}
      <Table
        rowKey="version_id"
        size="small"
        dataSource={nodes}
        pagination={false}
        columns={[
          { title: '版本', dataIndex: 'version_id' },
          { title: '标签', dataIndex: 'label' },
          {
            title: '类型',
            dataIndex: 'kind',
            width: 110,
            render: (v: string) => <Tag>{v}</Tag>,
          },
          {
            title: '父版本',
            dataIndex: 'parent_ids',
            render: (v: string[]) => (v.length ? v.join(', ') : '—'),
          },
        ]}
      />
      <Card size="small" title="版本对比（DAG 图形化视图规划中）">
        <Space wrap>
          <span>基准：</span>
          <Select
            style={{ width: 200 }}
            placeholder="选择版本"
            value={fromV}
            onChange={setFromV}
            options={options}
            data-testid="diff-from"
          />
          <span>对比：</span>
          <Select
            style={{ width: 200 }}
            placeholder="选择版本"
            value={toV}
            onChange={setToV}
            options={options}
            data-testid="diff-to"
          />
        </Space>
        {fromV && toV && (
          <div style={{ marginTop: 12 }}>
            {diff.isLoading ? (
              <Spin />
            ) : diff.data?.length ? (
              diff.data.map((d) => (
                <div key={d.path} style={{ marginBottom: 16 }}>
                  <p style={{ margin: '4px 0' }}>
                    <code>{d.path}</code> <Tag>{d.op}</Tag>
                  </p>
                  <pre
                    style={{
                      background: '#fafafa',
                      padding: 8,
                      fontSize: 12,
                      maxHeight: 200,
                      overflow: 'auto',
                    }}
                  >
                    {d.patch}
                  </pre>
                </div>
              ))
            ) : (
              <Empty description="两版本无差异" />
            )}
          </div>
        )}
      </Card>
    </Space>
  );
}

// 优化范围 Tab（U7b）：自动/明确定义可编辑（PUT optimization-scope）。
// 仅约束 RSI 自动优化；人工编辑仍受全局安全边界（白名单+冻结域）约束。
function ScopeTab({ sutId, scope }: { sutId: string; scope: string[] | null }) {
  const saveMut = useUpdateSutScope(sutId);
  const explicit = !!scope?.length;
  const [mode, setMode] = useState<'auto' | 'explicit'>(explicit ? 'explicit' : 'auto');
  const [sel, setSel] = useState<ScopeSelection>(() => selectionFromScope(scope));
  const [scopeError, setScopeError] = useState('');
  useEffect(() => {
    setMode(scope?.length ? 'explicit' : 'auto');
    setSel(selectionFromScope(scope));
  }, [scope]);

  const items = scopeFromSelection(sel);
  const save = () => {
    saveMut.mutate(mode === 'explicit' && items.length ? items : null, {
      onSuccess: () => setScopeError(''),
      onError: (err) => setScopeError(err.message),
    });
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert
        type="info"
        showIcon
        message={`当前模式：${
          explicit ? `明确定义（${scope!.length} 项）` : '自动（由平台扫描后决定）'
        }`}
        description="仅约束 RSI 自动优化（Critic/applier 在执行层强制收窄至勾选项）；人工文件编辑不受此限制，仍受全局安全边界（白名单+冻结域）约束。"
      />
      <Card size="small" title={<b>RSI 可优化范围（可编辑）</b>}>
        <Space direction="vertical" style={{ width: '100%' }} size="small">
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            data-testid="detail-scope-mode"
          >
            <Radio value="auto">自动（由平台扫描后决定）</Radio>
            <Radio value="explicit">明确定义</Radio>
          </Radio.Group>
          {mode === 'explicit' && <ScopeEditor value={sel} onChange={setSel} />}
          {mode === 'explicit' && items.length > 0 && (
            <div>
              将提交：
              {items.map((i) => (
                <Tag key={i}>
                  <code>{i}</code>
                </Tag>
              ))}
            </div>
          )}
          {scopeError && (
            <Alert
              type="error"
              showIcon
              data-testid="scope-422-error"
              message={scopeError}
            />
          )}
          <Space>
            <Button
              type="primary"
              loading={saveMut.isPending}
              onClick={save}
              data-testid="scope-save"
            >
              保存
            </Button>
          </Space>
        </Space>
      </Card>
      <Card size="small" title={<b>全局可优化项参考（8 项白名单，只读）</b>}>
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          dataSource={TARGET_PATHS}
          columns={[
            { title: '优化项', dataIndex: 'key', width: 140 },
            { title: '路径规则', dataIndex: 'path', render: (v: string) => <code>{v}</code> },
            { title: '说明', dataIndex: 'desc' },
          ]}
        />
      </Card>
    </Space>
  );
}

export function SutDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data: sut, isLoading } = useSut(id);

  if (isLoading) return <Spin />;
  if (!sut) return <Empty description="SUT 不存在" />;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="ID">{sut.id}</Descriptions.Item>
          <Descriptions.Item label="名称">{sut.name}</Descriptions.Item>
          <Descriptions.Item label="载体">{sut.carrier}</Descriptions.Item>
          <Descriptions.Item label="镜像">
            <code>{sut.image_ref}</code>
          </Descriptions.Item>
          <Descriptions.Item label="创建时间">{sut.created_at}</Descriptions.Item>
          <Descriptions.Item label="描述">{sut.description || '—'}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Tabs
        defaultActiveKey="files"
        destroyOnHidden
        items={[
          { key: 'files', label: '配置文件', children: <FileEditorTab sutId={sut.id} /> },
          { key: 'versions', label: '版本', children: <LineagePanel sutId={sut.id} /> },
          {
            key: 'scope',
            label: '优化范围',
            children: (
              <ScopeTab sutId={sut.id} scope={sut.optimization_scope ?? null} />
            ),
          },
        ]}
      />
    </Space>
  );
}
