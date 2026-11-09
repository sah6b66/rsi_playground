// 占位页面（Task 1 过渡用，后续任务逐个替换，Task 8 清理）
import { Card, Result } from 'antd';

export function ComingSoon({ title }: { title: string }) {
  return (
    <Card title={title}>
      <Result status="info" title="施工中" subTitle={`${title} 页面将在后续版本提供`} />
    </Card>
  );
}
