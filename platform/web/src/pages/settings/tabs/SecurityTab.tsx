// 安全边界 Tab（DD-14 §5.10⑤ / §5.2 G3 只读页）：白名单 8 优化项 +
// 3 冻结域 + 三层保障说明。后端 GET /system/freeze 仅返回 {frozen}——
// 清单来自代码级常量（rsi/optimizer/targets.py TARGET_PATHS/FROZEN_PATHS，
// 后端未提供 registry 端点时的前端兜底）。
import { Alert, Card, Space, Table, Tag } from 'antd';
import { LockOutlined, SafetyOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../../../api/client';
import { FROZEN_PATHS, TARGET_PATHS } from '../../../utils/targets';

// 代码级常量（TARGET_PATHS 8 项 / FROZEN_PATHS 3 项）→ 共享常量
const TARGET_WHITELIST = TARGET_PATHS;
const FROZEN_DOMAINS = FROZEN_PATHS;

export function SecurityTab() {
  const { data } = useQuery({
    queryKey: ['system-freeze'],
    queryFn: () => apiFetch<{ frozen: boolean }>('GET', '/system/freeze'),
  });

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert
        type={data?.frozen ? 'warning' : 'success'}
        showIcon
        message={
          data?.frozen
            ? '平台处于冻结状态：所有变更（含人工）暂停'
            : '平台运行中：变更仅限白名单执行层校验通过后生效'
        }
      />
      <Card
        size="small"
        title={
          <Space>
            <SafetyOutlined style={{ color: '#52c41a' }} />
            <b>可优化目标白名单（8 项）</b>
            <Tag color="green">只读</Tag>
          </Space>
        }
      >
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          dataSource={TARGET_WHITELIST}
          columns={[
            { title: '优化项', dataIndex: 'key', width: 140 },
            { title: '路径规则', dataIndex: 'path', render: (v: string) => <code>{v}</code> },
            { title: '说明', dataIndex: 'desc' },
          ]}
        />
      </Card>
      <Card
        size="small"
        title={
          <Space>
            <LockOutlined style={{ color: '#faad14' }} />
            <b>冻结域（3 项）</b>
            <Tag color="orange">任何变更不可触碰</Tag>
          </Space>
        }
      >
        <Table
          rowKey="path"
          size="small"
          pagination={false}
          dataSource={FROZEN_DOMAINS}
          columns={[
            { title: '路径规则', dataIndex: 'path', render: (v: string) => <code>{v}</code> },
            { title: '说明', dataIndex: 'desc' },
          ]}
        />
      </Card>
      <Card size="small" title="三层保障（G3）">
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          <li>
            <b>白名单执行层校验</b>：变更在执行与自动评审两个环节均会校验白名单，
            越界路径一律拒绝。
          </li>
          <li>
            <b>冻结域</b>：评审器、评审提示词与评测集属于固定保护内容，
            任何修改（包括人工提交）都无法通过。
          </li>
          <li>
            <b>平台冻结开关</b>：必要时管理员可一键暂停全部变更通道。
          </li>
        </ul>
        <p style={{ marginBottom: 0, color: 'rgba(0,0,0,0.45)' }}>
          本页清单与平台内置安全规则一致，仅作展示，不支持在此修改。
        </p>
      </Card>
    </Space>
  );
}
