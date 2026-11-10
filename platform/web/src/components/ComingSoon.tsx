// 未实施路由的占位（原 pages/placeholder.tsx，T8 收编）
import { Card, Result } from 'antd';

export function ComingSoon({ title }: { title: string }) {
  return (
    <Card title={title}>
      <Result status="info" title="施工中" subTitle={`${title} 页面将在后续版本提供`} />
    </Card>
  );
}
