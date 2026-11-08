<script>
  // History's overview band (docs/history-charts.md): our games across games, under the list's queue / side / patch
  // filters. Colours: the component set validated on the panel surface #161b22 (blue #3987e5, orange #d95926,
  // aqua #199e70); every rate carries its game count and a 95% interval.
  import Champ from './Champ.svelte';
  import EChart from './EChart.svelte';

  let { filters = {} } = $props();
  let stats = $state(null);
  let open = $state(true);
  try { open = localStorage.getItem('history.overview') !== 'closed'; } catch {}
  function toggle() {
    open = !open;
    try { localStorage.setItem('history.overview', open ? 'open' : 'closed'); } catch {}
  }

  $effect(() => {
    const q = new URLSearchParams(Object.entries(filters).filter(([k, v]) => v && k !== 'result'));
    fetch(`/api/history/stats?${q}`).then((r) => r.json()).then((d) => (stats = d));
  });

  const BLUE = '#3987e5', ORANGE = '#d95926', AQUA = '#199e70';
  const INK = '#8b96a3', GRID = '#2d3540', TEXT = '#e6edf3';
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
  const ci = (r) => (r.ci ? `${pct(r.ci[0])}–${pct(r.ci[1])}` : '');
  const signed = (x) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : '±'}${Math.abs(x)}`);
  const base = {
    animation: false,
    textStyle: { color: INK, fontFamily: 'inherit' },
    tooltip: { backgroundColor: '#1f2630', borderColor: GRID, textStyle: { color: TEXT, fontSize: 13 } },
  };
  const axis = (extra = {}) => ({ axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
                                  splitLine: { lineStyle: { color: GRID, opacity: 0.5 } }, axisLabel: { color: INK }, ...extra });

  // a 95% interval as a thin whisker: data [category index, lo, hi]
  function whiskers(horizontal) {
    return {
      type: 'custom', silent: true, z: 5,
      renderItem: (params, api) => {
        const i = api.value(0), lo = api.value(1), hi = api.value(2);
        const a = horizontal ? api.coord([lo, i]) : api.coord([i, lo]);
        const b = horizontal ? api.coord([hi, i]) : api.coord([i, hi]);
        const cap = 5;
        const style = { stroke: TEXT, lineWidth: 1.5, opacity: 0.8 };
        const capAt = (p) => horizontal
          ? { type: 'line', shape: { x1: p[0], y1: p[1] - cap, x2: p[0], y2: p[1] + cap }, style }
          : { type: 'line', shape: { x1: p[0] - cap, y1: p[1], x2: p[0] + cap, y2: p[1] }, style };
        return { type: 'group', children: [
          { type: 'line', shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, style }, capAt(a), capAt(b)] };
      },
    };
  }

  const trendOption = $derived(stats && {
    ...base,
    grid: { left: 44, right: 16, top: 30, bottom: 28 },
    legend: { top: 0, left: 40, itemGap: 22, textStyle: { color: INK }, itemWidth: 18,
              data: [{ name: 'Win rate', icon: 'path://M0,0H18V2H0Z' }, { name: 'Predicted', icon: 'path://M0,0H6V2H0ZM12,0H18V2H12Z' }] },
    tooltip: { ...base.tooltip, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: INK } },
               formatter: (ps) => {
                 const t = stats.trend[ps[0].dataIndex];
                 return `<b>Game ${t.game}</b> · ${t.result}<br>${pct(t.rolling_wr)} win rate, last ${stats.rolling}<br>`
                   + `${pct(t.rolling_pred)} predicted${t.pred != null ? ` · this game ${pct(t.pred)}` : ''}`;
               } },
    xAxis: axis({ type: 'category', data: stats.trend.map((t) => t.game), splitLine: { show: false } }),
    yAxis: axis({ type: 'value', min: 0, max: 1, interval: 0.25, axisLabel: { color: INK, formatter: (v) => pct(v) } }),
    series: [
      { name: 'Win rate', type: 'line', data: stats.trend.map((t) => t.rolling_wr), color: BLUE, showSymbol: false,
        lineStyle: { width: 2 },
        markLine: { silent: true, symbol: 'none', lineStyle: { color: INK, type: 'dotted' }, label: { show: false },
                    data: [{ yAxis: 0.5 }] } },
      { name: 'Predicted', type: 'line', data: stats.trend.map((t) => t.rolling_pred), color: ORANGE, showSymbol: false,
        lineStyle: { width: 2, type: [6, 6] } },
    ],
  });

  const calibOption = $derived(stats && {
    ...base,
    grid: { left: 44, right: 16, top: 12, bottom: 36 },
    tooltip: { ...base.tooltip, trigger: 'item',
               formatter: (p) => {
                 const b = stats.calibration[p.dataIndex];
                 return b ? `<b>${pct(b.wr)} won</b> of ${b.games} game${b.games === 1 ? '' : 's'}<br>${pct(b.predicted)} predicted (${b.bin})<br>95%: ${ci(b)}` : '';
               } },
    xAxis: axis({ type: 'value', min: 0, max: 1, name: 'Predicted', nameLocation: 'middle', nameGap: 24,
                  axisLabel: { color: INK, formatter: (v) => pct(v) } }),
    yAxis: axis({ type: 'value', min: 0, max: 1, axisLabel: { color: INK, formatter: (v) => pct(v) } }),
    series: [
      { type: 'line', data: [[0, 0], [1, 1]], silent: true, showSymbol: false, lineStyle: { color: INK, type: 'dotted', width: 1 } },
      { type: 'custom', silent: true, z: 4, data: stats.calibration.filter((b) => b.ci).map((b) => [b.predicted, b.ci[0], b.ci[1]]),
        renderItem: (params, api) => {
          const a = api.coord([api.value(0), api.value(1)]), b = api.coord([api.value(0), api.value(2)]);
          return { type: 'line', shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, style: { stroke: BLUE, lineWidth: 1.5, opacity: 0.6 } };
        } },
      { type: 'scatter', color: BLUE, z: 6, data: stats.calibration.map((b) => [b.predicted, b.wr]),
        symbolSize: (v, p) => 8 + Math.min(16, 2 * Math.sqrt(stats.calibration[p.dataIndex].games)),
        itemStyle: { borderColor: '#161b22', borderWidth: 2 } },
    ],
  });

  const execOption = $derived(stats && stats.execution.length && {
    ...base,
    grid: { left: 44, right: 16, top: 30, bottom: 28 },
    legend: { top: 0, left: 40, itemGap: 22, textStyle: { color: INK }, itemWidth: 12, itemHeight: 8 },
    tooltip: { ...base.tooltip, trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,0.04)' } },
               formatter: (ps) => {
                 const e = stats.execution[ps[0].dataIndex];
                 return `<b>Game ${e.game}</b> · ${e.result}${e.verdict ? ` · ${e.verdict}` : ''}<br>`
                   + `${signed(e.draft)}pp draft<br>${signed(e.early)}pp early game<br>${signed(e.conversion)}pp conversion`;
               } },
    xAxis: axis({ type: 'category', data: stats.execution.map((e) => e.game), splitLine: { show: false } }),
    yAxis: axis({ type: 'value', axisLabel: { color: INK, formatter: (v) => `${v}pp` } }),
    series: [['Draft', 'draft', BLUE], ['Early game', 'early', ORANGE], ['Conversion', 'conversion', AQUA]].map(([name, key, color]) => ({
      name, type: 'bar', stack: 'split', stackStrategy: 'samesign', color, barMaxWidth: 18,
      itemStyle: { borderColor: '#161b22', borderWidth: 1 }, data: stats.execution.map((e) => e[key]) })),
  });

  // win rate by side, queue, following #1, plan: small horizontal bars with 95% whiskers
  function splitOption(rows) {
    const shown = rows.filter((r) => r.games);
    return {
      ...base,
      grid: { left: 150, right: 24, top: 6, bottom: 22 },
      tooltip: { ...base.tooltip, trigger: 'item',
                 formatter: (p) => { const r = shown[p.dataIndex]; return r ? `<b>${pct(r.wr)}</b> won · ${r.wins}–${r.games - r.wins}<br>95%: ${ci(r)}${r.predicted != null ? `<br>${pct(r.predicted)} predicted` : ''}` : ''; } },
      xAxis: axis({ type: 'value', min: 0, max: 1, interval: 0.5, axisLabel: { color: INK, formatter: (v) => pct(v) } }),
      yAxis: axis({ type: 'category', data: shown.map((r) => `${r.name} · ${r.games} g`), inverse: true,
                    splitLine: { show: false }, axisLabel: { color: TEXT, width: 140, overflow: 'truncate' } }),
      series: [
        { type: 'bar', color: BLUE, barMaxWidth: 14, data: shown.map((r) => r.wr),
          itemStyle: { borderRadius: [0, 4, 4, 0] },
          markLine: { silent: true, symbol: 'none', lineStyle: { color: INK, type: 'dotted' }, label: { show: false }, data: [{ xAxis: 0.5 }] } },
        { ...whiskers(true), data: shown.filter((r) => r.ci).map((r) => [shown.indexOf(r), r.ci[0], r.ci[1]]) },
      ],
    };
  }

  const splits = $derived(stats ? [
    ['Side', stats.sides], ['Queue', stats.queues], ['Our #1 suggestion', stats.followed], ['Pinned plan', stats.plans],
  ].filter(([, rows]) => rows.some((r) => r.games)) : []);
  // gold bars: teal ahead, amber behind (the validated favourability pair), scaled to the biggest lead shown
  const goldMax = $derived(stats ? Math.max(500, ...stats.players.flatMap((p) => p.champions.map((c) => Math.abs(c.gold15 ?? 0)))) : 500);
</script>

<section class="overview">
  <button class="head" onclick={toggle} aria-expanded={open}>
    <span>{open ? '▾' : '▸'} Overview</span>
    {#if stats?.games}<span class="muted">{stats.games} games · {pct(stats.overall.wr)} won ({ci(stats.overall)})</span>{/if}
  </button>
  {#if open && stats}
    {#if !stats.games}
      <p class="muted">No finished games under these filters yet.</p>
    {:else}
      <div class="grid">
        <figure class="wide">
          <figcaption>Win rate vs predicted <span>rolling, last {stats.rolling} games</span></figcaption>
          <EChart option={trendOption} label="Rolling win rate and predicted win rate by game" />
        </figure>
        <figure>
          <figcaption>Calibration <span>{stats.calibrated_games} live games; on the dotted line = well calibrated</span></figcaption>
          {#if stats.calibration.length}<EChart option={calibOption} label="Predicted against actual win rate" />
          {:else}<p class="muted">Needs games logged live with a prediction.</p>{/if}
        </figure>
        <figure class="wide">
          <figcaption>What decided each game <span>draft, early game and conversion, pp of a win</span></figcaption>
          {#if execOption}<EChart option={execOption} label="Draft, early and conversion share per game" />
          {:else}<p class="muted">Needs timelines: run Confirm games (postgame) and the execution fit.</p>{/if}
        </figure>
        {#each splits as [title, rows]}
          <figure>
            <figcaption>Win rate by {title.toLowerCase()} <span>bars: 95% interval</span></figcaption>
            <EChart option={splitOption(rows)} height={Math.max(90, 34 * rows.filter((r) => r.games).length + 30)} label="Win rate by {title}" />
          </figure>
        {/each}
      </div>

      <h3>Players <span class="muted">champions, games, win rate (95% interval) and gold lead at 15</span></h3>
      <div class="players">
        {#each stats.players as p}
          <div class="player">
            <div class="phead"><strong>{p.player}</strong> <span class="muted">{p.role_label} · {p.games} g · {pct(p.wr)}</span></div>
            <table>
              <tbody>
                {#each p.champions.slice(0, 6) as c}
                  <tr>
                    <td class="c"><Champ champ={c.champ} size={22} /> {c.champ?.name}</td>
                    <td class="n">{c.games}</td>
                    <td class="n" title="95% interval {ci(c)}">{pct(c.wr)}</td>
                    <td class="gold" title={c.gold15 == null ? 'No timeline' : `${signed(c.gold15)} gold at 15 over ${c.gold_games} game${c.gold_games === 1 ? '' : 's'}`}>
                      {#if c.gold15 != null}<span class="gbar" style="--w: {(50 * Math.abs(c.gold15)) / goldMax}%" class:neg={c.gold15 < 0}></span>{/if}
                    </td>
                    <td class="n gv">{c.gold15 == null ? '' : signed(c.gold15)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/each}
      </div>

      {#if stats.bans.length}
        <h3>Our bans <span class="muted">most banned: games and our win rate in them (overall {pct(stats.overall.wr)})</span></h3>
        <div class="bans">
          {#each stats.bans as b}
            <div class="ban" title="95% interval {ci(b)}">
              <Champ champ={b.champ} size={28} banned />
              <span>{b.games} g · {pct(b.wr)}</span>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .overview { background: var(--panel); border: 1px solid #1d232c; border-radius: 10px; padding: 10px 16px 14px; margin-bottom: 14px; }
  .head { display: flex; gap: 14px; align-items: baseline; background: none; border: 0; color: var(--text); font: inherit;
          font-size: 16px; font-weight: 600; padding: 4px 0; cursor: pointer; width: 100%; text-align: left; }
  .muted { color: var(--muted); font-size: 13px; font-weight: 400; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px 18px; margin-top: 8px; }
  figure { margin: 0; min-width: 0; }
  figure.wide { grid-column: span 2; }
  @media (max-width: 760px) { figure.wide { grid-column: auto; } }
  figcaption { font-size: 14px; color: var(--text); margin-bottom: 2px; }
  figcaption span { color: var(--muted); font-size: 12px; margin-left: 6px; }
  h3 { font-size: 15px; margin: 18px 0 8px; }
  .players { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .player { background: var(--panel-2); border-radius: 8px; padding: 8px 10px; }
  .phead { margin-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td { padding: 2px 4px; }
  td.c { display: flex; align-items: center; gap: 6px; white-space: nowrap; overflow: hidden; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; width: 3em; }
  td.gold { position: relative; width: 56px; }
  td.gold::before { content: ''; position: absolute; left: 50%; top: 3px; bottom: 3px; border-left: 1px solid var(--line); }
  td.gv { width: 3.5em; color: var(--muted); }
  .gbar { position: absolute; top: 6px; bottom: 6px; left: 50%; width: var(--w); background: #1baf7a; border-radius: 0 3px 3px 0; }
  .gbar.neg { left: auto; right: 50%; background: #c98500; border-radius: 3px 0 0 3px; }
  .bans { display: flex; flex-wrap: wrap; gap: 10px 16px; }
  .ban { display: flex; align-items: center; gap: 6px; font-size: 13px; }
</style>
