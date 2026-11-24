import { useEffect, useRef } from 'react';
import { init, use, type EChartsCoreOption } from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent, MarkLineComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';

use([LineChart, GridComponent, LegendComponent, TooltipComponent, MarkLineComponent, SVGRenderer]);

export function Chart({ title, options }: { title: string; options: EChartsCoreOption }) {
  const node = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!node.current) return;
    const instance = init(node.current, undefined, { renderer: 'svg' });
    instance.setOption(options);
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(node.current);
    return () => {
      observer.disconnect();
      instance.dispose();
    };
  }, [options]);
  return <div ref={node} className="chart" role="img" aria-label={title} />;
}

export function lines(categories: string[], baseline: (number | null)[], candidate: (number | null)[],
                      unit: string): EChartsCoreOption {
  return {
    animation: false,
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'ui-monospace, monospace' },
    color: ['#365be8', '#d2604b'],
    grid: { left: 60, right: 22, top: 33, bottom: 36 },
    legend: { top: 0, right: 11, textStyle: { color: '#4b576f', fontSize: 11 } },
    tooltip: { trigger: 'axis', backgroundColor: '#17223a', borderColor: '#38465d',
      textStyle: { color: '#e6ebf4' }, valueFormatter: (value: unknown) =>
        typeof value === 'number' ? new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(value) + ' ' + unit : '—' },
    xAxis: { type: 'category', data: categories, boundaryGap: false,
      axisLabel: { color: '#59667d', fontSize: 11 }, axisLine: { lineStyle: { color: '#d1d9e6' } } },
    yAxis: { type: 'value', name: unit, scale: true, nameTextStyle: { color: '#59667d' },
      axisLabel: { color: '#59667d', fontSize: 11 }, splitLine: { lineStyle: { color: '#e7ebf2' } } },
    series: [{ name: 'Baseline', data: baseline, type: 'line', symbolSize: 8, lineStyle: { width: 2.5 } },
      { name: 'Candidate', data: candidate, type: 'line', symbolSize: 8, lineStyle: { width: 2.5 } }],
  };
}
