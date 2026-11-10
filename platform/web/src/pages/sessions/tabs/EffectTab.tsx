// 效果 Tab（DD-14 §5.4）：timeline summary → 三图。
import { Skeleton, Typography } from 'antd';
import { useSessionTimeline } from '../../../api/hooks';
import { MetricsCharts } from '../../../components/MetricsCharts';

export function EffectTab({ id }: { id: string }) {
  const timeline = useSessionTimeline(id);
  if (timeline.isLoading) return <Skeleton active />;
  if (timeline.isError)
    return (
      <Typography.Text type="danger">
        加载效果数据失败：{timeline.error.message}
      </Typography.Text>
    );
  const rounds = timeline.data?.rounds ?? [];
  const summary = timeline.data?.summary;
  // 噪声带取最近一轮的 scores.noise_band
  const lastBand = rounds.length
    ? rounds[rounds.length - 1].scores.noise_band
    : undefined;
  return (
    <MetricsCharts
      series={{
        accuracy_series: summary?.accuracy_series ?? [],
        tokens_series: summary?.tokens_series ?? [],
        latency_series: summary?.latency_series ?? [],
        noise_band: lastBand,
      }}
    />
  );
}
