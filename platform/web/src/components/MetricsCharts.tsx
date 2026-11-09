// 效果三图（DD-14 §5.4）：accuracy 折线（含 noise_band 色带）/
// tokens 折线 / latency 散点+均值线。ECharts 在 jsdom 不渲染 canvas，
// 测试通过 vi.mock 捕获 option。
import ReactECharts from 'echarts-for-react';

export interface ChartSeries {
  accuracy_series: { round: number; value: number }[];
  tokens_series: { round: number; value: number }[];
  latency_series: { round: number; value: number }[];
  noise_band?: [number | null, number | null];
}

const axisOf = (s: { round: number }[]) => s.map((p) => `第 ${p.round} 轮`);
const valOf = (s: { value: number }[]) => s.map((p) => p.value);

export function MetricsCharts({ series }: { series: ChartSeries }) {
  const rounds = axisOf(series.accuracy_series);
  const acc = valOf(series.accuracy_series);
  const band = series.noise_band ?? [null, null];
  const lat = series.latency_series;
  const meanLat =
    lat.length > 0
      ? lat.reduce((sum, p) => sum + p.value, 0) / lat.length
      : null;

  return (
    <div>
      <div data-testid="chart-accuracy">
        <ReactECharts
          style={{ height: 260 }}
          option={{
            title: { text: '准确率（含噪声带）' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: rounds },
            yAxis: { type: 'value', min: 0, max: 1 },
            series: [
              { name: 'accuracy', type: 'line', data: acc },
              // noise_band 近似：上下边界线 + 区间色带（stacked area）
              {
                name: '噪声带下界',
                type: 'line',
                stack: 'band',
                data: acc.map(() => band[0] ?? 0),
                lineStyle: { opacity: 0 },
                symbol: 'none',
                silent: true,
              },
              {
                name: '噪声带',
                type: 'line',
                stack: 'band',
                data: acc.map(() =>
                  band[0] != null && band[1] != null ? band[1] - band[0] : 0,
                ),
                lineStyle: { opacity: 0 },
                areaStyle: { opacity: 0.15 },
                symbol: 'none',
                silent: true,
              },
              {
                name: '噪声带上界',
                type: 'line',
                data: acc.map(() => band[1] ?? null),
                lineStyle: { type: 'dashed', opacity: 0.5 },
                symbol: 'none',
              },
            ],
          }}
        />
      </div>
      <div data-testid="chart-tokens">
        <ReactECharts
          style={{ height: 260 }}
          option={{
            title: { text: 'Token 消耗' },
            tooltip: { trigger: 'axis' },
            xAxis: { type: 'category', data: axisOf(series.tokens_series) },
            yAxis: { type: 'value' },
            series: [
              {
                name: 'tokens',
                type: 'line',
                data: valOf(series.tokens_series),
              },
            ],
          }}
        />
      </div>
      <div data-testid="chart-latency">
        <ReactECharts
          style={{ height: 260 }}
          option={{
            title: { text: '平均延迟（ms）' },
            tooltip: { trigger: 'item' },
            xAxis: { type: 'category', data: axisOf(lat) },
            yAxis: { type: 'value' },
            series: [
              {
                name: 'latency_ms',
                type: 'scatter',
                data: valOf(lat),
                markLine: meanLat != null
                  ? {
                      silent: true,
                      data: [{ yAxis: meanLat, name: '均值' }],
                    }
                  : undefined,
              },
            ],
          }}
        />
      </div>
    </div>
  );
}
