// 列表查询错误态：403 → 无权访问 Result；其他错误 → 通用失败 Result（M1）。
import { Result } from 'antd';
import { ApiError } from '../api/client';

export function QueryErrorResult({
  error,
  needPerm,
}: {
  error: unknown;
  needPerm: string;
}) {
  if (error instanceof ApiError && error.status === 403) {
    return (
      <Result
        status="403"
        title="403 无权访问"
        subTitle={`当前角色不具备 ${needPerm} 权限，无法查看该资源列表。请联系管理员授权。`}
      />
    );
  }
  return (
    <Result
      status="error"
      title="加载失败"
      subTitle={error instanceof Error ? error.message : '未知错误'}
    />
  );
}
