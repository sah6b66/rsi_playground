// SUT FileEditor Tab（DD-14 §5.6，P0 人工模式核心）：左文件树（GET
// /suts/{id}/files → {path:content} 按 path 拆分）+ 右 Monaco 编辑 +
// 保存栏（commit Switch 默认 false=staging；message commit 时必填）。
// 冻结/越界路径 PUT → 409 → 行内错误（显示后端 detail + 未落盘提示）。
import { Alert, Button, Form, Input, Modal, Space, Switch, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { useEffect, useMemo, useState } from 'react';
import Editor from '@monaco-editor/react';
import { ApiError } from '../../api/client';
import {
  useCreateSutFile,
  useSutFiles,
  useWriteSutFile,
} from '../../api/hooks';

const langOf = (path: string): string => {
  const ext = path.split('.').pop()?.toLowerCase();
  if (ext === 'md') return 'markdown';
  if (ext === 'json') return 'json';
  if (ext === 'py') return 'python';
  if (ext === 'ts' || ext === 'tsx') return 'typescript';
  if (ext === 'js') return 'javascript';
  if (ext === 'yaml' || ext === 'yml') return 'yaml';
  return 'plaintext';
};

// path → antd Tree 数据（目录聚合为父节点）
function buildTree(paths: string[]): DataNode[] {
  const root: DataNode[] = [];
  const dirMap = new Map<string, DataNode[]>();
  for (const p of [...paths].sort()) {
    const parts = p.split('/');
    let parentKey = '';
    for (let i = 0; i < parts.length - 1; i++) {
      const key = `${parentKey}${parts[i]}/`;
      if (!dirMap.has(key)) {
        const node: DataNode = { title: parts[i], key, children: [] };
        const siblings = parentKey
          ? dirMap.get(parentKey)!
          : root;
        siblings.push(node);
        dirMap.set(key, node.children as DataNode[]);
      }
      parentKey = key;
    }
    (parentKey ? dirMap.get(parentKey)! : root).push({
      title: parts[parts.length - 1],
      key: p,
      isLeaf: true,
    });
  }
  return root;
}

export function FileEditorTab({ sutId }: { sutId: string }) {
  const { data: files, isLoading } = useSutFiles(sutId);
  const writeMut = useWriteSutFile(sutId);
  const createFileMut = useCreateSutFile(sutId);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [commit, setCommit] = useState(false);
  const [message, setMessage] = useState('');
  const [newOpen, setNewOpen] = useState(false);
  const [newForm] = Form.useForm<{ path: string }>();

  const paths = useMemo(() => Object.keys(files ?? {}), [files]);
  const tree = useMemo(() => buildTree(paths), [paths]);

  useEffect(() => {
    if (!selected && paths.length) {
      setSelected(paths[0]!);
      setDraft(files?.[paths[0]] ?? '');
    }
  }, [paths, selected, files]);

  const onSelect = (keys: React.Key[]) => {
    const key = String(keys[0] ?? '');
    if (files && key in files) {
      setSelected(key);
      setDraft(files[key] ?? '');
    }
  };

  const save = () => {
    if (!selected) return;
    writeMut.mutate(
      { path: selected, content: draft, commit, message },
      { onSuccess: () => setMessage('') },
    );
  };

  const inline409 =
    writeMut.error instanceof ApiError && writeMut.error.status === 409
      ? writeMut.error.message
      : null;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Space>
        <Button size="small" onClick={() => setNewOpen(true)}>
          新建
        </Button>
        <span>commit：</span>
        <Switch
          checked={commit}
          onChange={setCommit}
          checkedChildren="提交版本"
          unCheckedChildren="staging"
          data-testid="commit-switch"
        />
        <Input
          placeholder={commit ? 'commit message（必填）' : 'commit message'}
          style={{ width: 320 }}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          data-testid="commit-message"
        />
        <Button
          type="primary"
          size="small"
          loading={writeMut.isPending}
          disabled={!selected || (commit && !message.trim())}
          onClick={save}
          data-testid="save-file"
        >
          保存
        </Button>
      </Space>
      {inline409 && (
        <Alert
          type="error"
          showIcon
          data-testid="file-409-error"
          message={`保存被拒绝（409）：${inline409}`}
          description="路径在冻结域或白名单之外，内容未落盘。仅 suts/{id}/ 下白名单 8 项目标路径可编辑。"
          closable
        />
      )}
      <div style={{ display: 'flex', gap: 8, minHeight: 420 }}>
        <div style={{ width: 260, borderRight: '1px solid #f0f0f0', overflow: 'auto' }}>
          {isLoading ? '加载中…' : (
            <Tree
              showIcon
              defaultExpandAll
              treeData={tree}
              selectedKeys={selected ? [selected] : []}
              onSelect={onSelect}
            />
          )}
        </div>
        <div style={{ flex: 1 }}>
          <p style={{ marginTop: 0 }}>
            当前文件：<code>{selected ?? '—'}</code>
          </p>
          <Editor
            height={380}
            language={selected ? langOf(selected) : 'plaintext'}
            value={draft}
            onChange={(v) => setDraft(v ?? '')}
            theme="vs-dark"
            options={{ minimap: { enabled: false } }}
          />
        </div>
      </div>

      <Modal
        open={newOpen}
        title="新建文件（staging）"
        okText="创建"
        cancelText="取消"
        onOk={() => newForm.submit()}
        confirmLoading={createFileMut.isPending}
        onCancel={() => setNewOpen(false)}
        destroyOnHidden
      >
        <Form
          form={newForm}
          layout="vertical"
          onFinish={({ path }) => {
            createFileMut.mutate(
              { path, content: '' },
              {
                onSuccess: () => {
                  setNewOpen(false);
                  setSelected(path);
                  setDraft('');
                  newForm.resetFields();
                },
              },
            );
          }}
        >
          <Form.Item
            label="路径（相对 SUT 根，如 skills/bgp.md）"
            name="path"
            rules={[{ required: true, message: '请输入路径' }]}
          >
            <Input placeholder="skills/bgp.md" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
