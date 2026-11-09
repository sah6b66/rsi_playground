// SSE 全局钩子（DD-14 §7）：EventSource 订阅 /api/v1/streams/events，
// 按 SSE_INVALIDATION 映射失效 react-query 缓存；system.alert → notification。
import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { notification } from 'antd';
import { API_PREFIX, TOKEN_KEY } from '../utils/constants';

// 事件类型 → 需要失效的 query key（DD-13 §4.4 *.status_changed + system.alert）
export const SSE_INVALIDATION: Record<string, string[][]> = {
  'iteration.status_changed': [['sessions'], ['session']],
  'run.status_changed': [['instances'], ['session']],
  'batch.status_changed': [['sessions'], ['session']],
  'instance.status_changed': [['instances']],
  'sut.status_changed': [['suts'], ['labs'], ['instances']],
  'system.health': [['dependencies'], ['backends'], ['instances']],
  'system.alert': [],
};

interface SseMessage {
  event?: string;
  ts?: number;
  data?: Record<string, unknown> & { level?: string; message?: string };
}

export function useEventStream(): void {
  const queryClient = useQueryClient();
  const token = localStorage.getItem(TOKEN_KEY);

  useEffect(() => {
    if (!token) return;
    const source = new EventSource(
      `${API_PREFIX}/streams/events?token=${encodeURIComponent(token)}`,
    );
    source.onmessage = (ev: MessageEvent<string>) => {
      let msg: SseMessage;
      try {
        msg = JSON.parse(ev.data) as SseMessage;
      } catch {
        return;
      }
      if (msg.event === 'system.alert') {
        const level = msg.data?.level;
        const critical = level === 'critical' || level === 'error';
        notification[critical ? 'error' : 'warning']({
          message: '系统告警',
          description: String(msg.data?.message ?? ''),
          duration: critical ? 0 : 4.5,
        });
        queryClient.invalidateQueries({ queryKey: ['dependencies'] });
        return;
      }
      const keys = SSE_INVALIDATION[msg.event ?? ''];
      if (keys) {
        for (const key of keys) {
          queryClient.invalidateQueries({ queryKey: key });
        }
      }
    };
    source.onerror = () => {
      // EventSource 自动重连；token 失效时由 401 中间层兜底登出。
    };
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient]);
}
