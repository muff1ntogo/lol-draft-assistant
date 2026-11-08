<script>
  // One ECharts chart. Only the chart types and components the History overview uses are registered, so the bundle
  // carries those, not all of ECharts (docs/history-charts.md).
  import * as echarts from 'echarts/core';
  import { BarChart, LineChart, ScatterChart, CustomChart } from 'echarts/charts';
  import { GridComponent, TooltipComponent, LegendComponent, MarkLineComponent } from 'echarts/components';
  import { SVGRenderer } from 'echarts/renderers';

  echarts.use([BarChart, LineChart, ScatterChart, CustomChart, GridComponent, TooltipComponent, LegendComponent,
               MarkLineComponent, SVGRenderer]);

  let { option, height = 220, label = '' } = $props();
  let el = $state();
  let chart;

  $effect(() => {
    if (!el) return;
    chart = echarts.init(el, null, { renderer: 'svg' });
    const ro = new ResizeObserver(() => chart?.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart?.dispose();
      chart = null;
    };
  });

  $effect(() => {
    if (chart && option) chart.setOption(option, true);
  });
</script>

<div bind:this={el} style="height: {height}px; width: 100%" role="img" aria-label={label}></div>
