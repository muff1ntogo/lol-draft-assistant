<script>
  import { LINE, MUTED, PANEL_2, SIDE_BLUE, SIDE_RED, TEXT } from './palette.js';
  // Lane power by milestone (docs/top-lane-power.md, Stages 4-5): gold per minute vs the role's average at each
  // milestone, ours and the lane opponent's, on an even milestone axis (not time). Spikes marked. Loaded lazily with
  // ECharts, like History's charts. Colours: the sides' validated pair (ours blue #4a8ee0, theirs red #d0574e; palette.js), plus a
  // dash on theirs so the lines never rely on colour alone.
  import EChart from './EChart.svelte';

  let { power, ourName = 'You', theirName = 'Them' } = $props();
  const OURS = SIDE_BLUE, THEIRS = SIDE_RED, INK = MUTED, GRID = LINE;
  const val = (p) => (p.combined == null ? null : Math.round(p.combined));

  const option = $derived({
    animation: false,
    textStyle: { color: INK, fontFamily: 'inherit' },
    grid: { left: 44, right: 12, top: 28, bottom: 26 },
    legend: { top: 0, left: 40, itemGap: 22, textStyle: { color: INK },
              data: [{ name: ourName, icon: 'path://M0,0H18V2H0Z' }, ...(power.theirs ? [{ name: theirName, icon: 'path://M0,0H6V2H0ZM12,0H18V2H12Z' }] : [])] },
    tooltip: {
      trigger: 'axis', backgroundColor: PANEL_2, borderColor: GRID, textStyle: { color: TEXT, fontSize: 13 },
      formatter: (ps) => {
        const i = ps[0].dataIndex, a = power.ours[i], b = power.theirs?.[i];
        const line = (p, who) => p ? `<b>${val(p) == null ? '—' : (val(p) > 0 ? '+' : '') + val(p)}</b> gold/min · ${who}${p.spike ? ' · spike' : ''}`
          + `<br><span style="color:${INK}">level ${p.level}, ${p.items} item${p.items === 1 ? '' : 's'}; ${p.n ? Math.round(p.n) + ' min measured' : 'model only'}</span>` : '';
        return `<b>${a.label}</b><br>${line(a, ourName)}${b ? '<br>' + line(b, theirName) : ''}`;
      },
    },
    xAxis: { type: 'category', data: power.ours.map((p) => p.label), axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
             axisLabel: { color: INK, fontSize: 11, interval: 0, rotate: 30 } },
    yAxis: { type: 'value', axisLabel: { color: INK, formatter: (v) => `${v > 0 ? '+' : ''}${v}` },
             splitLine: { lineStyle: { color: GRID, opacity: 0.5 } } },
    series: [
      { name: ourName, type: 'line', color: OURS, data: power.ours.map(val), lineStyle: { width: 2 }, symbolSize: 6,
        markLine: { silent: true, symbol: 'none', lineStyle: { color: INK, type: 'dotted' }, label: { show: false }, data: [{ yAxis: 0 }] },
        markPoint: { symbol: 'pin', symbolSize: 26, itemStyle: { color: OURS }, label: { show: false },
                     data: power.ours.map((p, i) => (p.spike && val(p) != null ? { coord: [i, val(p)] } : null)).filter(Boolean) } },
      ...(power.theirs ? [{ name: theirName, type: 'line', color: THEIRS, data: power.theirs.map(val), symbolSize: 6,
                            lineStyle: { width: 2, type: [6, 5] } }] : []),
    ],
  });
</script>

<EChart {option} height={190} label="Lane power by milestone" />
