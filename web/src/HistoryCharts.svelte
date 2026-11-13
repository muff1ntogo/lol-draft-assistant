<script>
  // History's overview band (docs/history-charts.md): our games across games, under the list's queue / side / patch
  // filters. Figures first (Stat), then the charts. Colours come from palette.js: the validated series set COMPONENT
  // where a chart has more than one series, neutral ink where it has one; every rate carries its game count and a 95%
  // interval.
  import Champ from './Champ.svelte';
  import EChart from './EChart.svelte';
  import { reveal } from './motion.js';
  import { COMPONENT, FONT_MONO, FONT_UI, LINE, MUTED, PANEL, PANEL_2, TEXT, TEXT_2 } from './palette.js';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Stat from './ui/Stat.svelte';
  import Table from './ui/Table.svelte';

  let { filters = {}, check = null } = $props(); // check: /api/model-check (Brier score and its verdict)
  let stats = $state(null);
  let open = $state(true);
  try { open = localStorage.getItem('history.overview') !== 'closed'; } catch {}
  function toggle() {
    open = !open;
    try { localStorage.setItem('history.overview', open ? 'open' : 'closed'); } catch {}
  }

  // Timeframe: the last N games (after the filters), or every game; remembered per browser
  const TIMEFRAMES = [[10, 'Last 10'], [20, '20'], [50, '50'], [100, '100'], [0, 'All']].map(([n, label]) => ({ value: n, label, title: n ? `Only the last ${n} games` : 'Every game' }));
  let last = $state(0);
  try { last = Number(localStorage.getItem('history.timeframe')) || 0; } catch {}
  function setLast(n) {
    last = n;
    try { localStorage.setItem('history.timeframe', String(n)); } catch {}
  }

  let request = 0;
  $effect(() => {
    const q = new URLSearchParams(Object.entries(filters).filter(([k, v]) => v && k !== 'result'));
    if (last) q.set('last', String(last));
    const mine = ++request; // a quick second click wins over a slow first answer
    fetch(`/api/history/stats?${q}`).then((r) => r.json()).then((d) => { if (mine === request) stats = d; });
  });

  const BLUE = COMPONENT.threat, ORANGE = COMPONENT.matchup, AQUA = COMPONENT.comp;
  const INK = MUTED, GRID = LINE;
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
  const ci = (r) => (r.ci ? `${pct(r.ci[0])}–${pct(r.ci[1])}` : '');
  const signed = (x) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : '±'}${Math.abs(x)}`);
  const base = {
    animation: false,
    textStyle: { color: INK, fontFamily: FONT_UI },
    tooltip: { backgroundColor: PANEL_2, borderColor: GRID, textStyle: { color: TEXT, fontSize: 13, fontFamily: FONT_UI } },
  };
  const axis = (extra = {}) => ({ axisLine: { lineStyle: { color: GRID } }, axisTick: { show: false },
                                  splitLine: { lineStyle: { color: GRID, opacity: 0.5 } }, axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11 }, ...extra });

  // a 95% interval as a thin whisker: data [category index, lo, hi]
  function whiskers(horizontal) {
    return {
      type: 'custom', silent: true, z: 5,
      renderItem: (params, api) => {
        const i = api.value(0), lo = api.value(1), hi = api.value(2);
        const a = horizontal ? api.coord([lo, i]) : api.coord([i, lo]);
        const b = horizontal ? api.coord([hi, i]) : api.coord([i, hi]);
        const cap = 5;
        const style = { stroke: TEXT, lineWidth: 1.5, opacity: 0.9 };
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
    yAxis: axis({ type: 'value', min: 0, max: 1, interval: 0.25, axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11, formatter: (v) => pct(v) } }),
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
                  axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11, formatter: (v) => pct(v) } }),
    yAxis: axis({ type: 'value', min: 0, max: 1, axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11, formatter: (v) => pct(v) } }),
    series: [
      { type: 'line', data: [[0, 0], [1, 1]], silent: true, showSymbol: false, lineStyle: { color: INK, type: 'dotted', width: 1 } },
      { type: 'custom', silent: true, z: 4, data: stats.calibration.filter((b) => b.ci).map((b) => [b.predicted, b.ci[0], b.ci[1]]),
        renderItem: (params, api) => {
          const a = api.coord([api.value(0), api.value(1)]), b = api.coord([api.value(0), api.value(2)]);
          return { type: 'line', shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, style: { stroke: TEXT_2, lineWidth: 1.5, opacity: 0.6 } };
        } },
      { type: 'scatter', color: TEXT, z: 6, data: stats.calibration.map((b) => [b.predicted, b.wr]),
        symbolSize: (v, p) => 8 + Math.min(16, 2 * Math.sqrt(stats.calibration[p.dataIndex].games)),
        itemStyle: { borderColor: PANEL, borderWidth: 2 } },
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
    yAxis: axis({ type: 'value', axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11, formatter: (v) => `${v}pp` } }),
    series: [['Draft', 'draft', BLUE], ['Early game', 'early', ORANGE], ['Conversion', 'conversion', AQUA]].map(([name, key, color]) => ({
      name, type: 'bar', stack: 'split', stackStrategy: 'samesign', color, barMaxWidth: 18,
      itemStyle: { borderColor: PANEL, borderWidth: 1 }, data: stats.execution.map((e) => e[key]) })),
  });

  // win rate by side, queue, following #1, plan: small horizontal bars with 95% whiskers
  function splitOption(rows) {
    const shown = rows.filter((r) => r.games);
    return {
      ...base,
      grid: { left: 150, right: 24, top: 6, bottom: 22 },
      tooltip: { ...base.tooltip, trigger: 'item',
                 formatter: (p) => { const r = shown[p.dataIndex]; return r ? `<b>${pct(r.wr)}</b> won · ${r.wins}–${r.games - r.wins}<br>95%: ${ci(r)}${r.predicted != null ? `<br>${pct(r.predicted)} predicted` : ''}` : ''; } },
      xAxis: axis({ type: 'value', min: 0, max: 1, interval: 0.5, axisLabel: { color: INK, fontFamily: FONT_MONO, fontSize: 11, formatter: (v) => pct(v) } }),
      yAxis: axis({ type: 'category', data: shown.map((r) => `${r.name} · ${r.games} g`), inverse: true,
                    splitLine: { show: false }, axisLabel: { color: TEXT, width: 140, overflow: 'truncate' } }),
      series: [
        { type: 'bar', color: TEXT_2, barMaxWidth: 10, data: shown.map((r) => r.wr),
          itemStyle: { borderRadius: [0, 2, 2, 0] },
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
  <div class="headrow">
    <h2>Overview</h2>
    {#if open}<Segmented label="Timeframe" value={last} onchange={setLast} options={TIMEFRAMES} />{/if}
    <Button variant="ghost" aria-expanded={open} onclick={toggle}>{open ? 'Hide' : 'Show'}</Button>
  </div>
  {#if stats}
    <div class="stats">
      <Stat value={stats.games || null} label="Games" note={stats.last && stats.total_games > stats.games ? `of ${stats.total_games}` : stats.games ? '' : 'none under these filters'} />
      <Stat value={stats.overall.wr == null ? null : Math.round(stats.overall.wr * 100)} unit="%" label="Win rate" note={stats.overall.ci ? `95% interval ${ci(stats.overall)}` : ''} />
      {#if check}
        <Stat value={check.brier == null ? null : check.brier.toFixed(3)} label="Brier score" note={check.brier == null ? check.verdict : 'a coin flip scores 0.250; lower is better'}
              title="Mean squared error of our win predictions over {check.games} game{check.games === 1 ? '' : 's'}. {check.verdict}" />
      {/if}
    </div>
    {#if check?.brier != null}<p class="cap">{check.verdict}</p>{/if}
  {/if}
  {#if open && stats}
  <div transition:reveal>
    {#if !stats.games}
      <p class="none">No finished games under these filters yet.</p>
    {:else}
      <div class="grid">
        <figure class="wide">
          <figcaption>Win rate vs predicted <span>rolling, last {stats.rolling} games</span></figcaption>
          <EChart option={trendOption} label="Rolling win rate and predicted win rate by game" />
        </figure>
        <figure>
          <figcaption>Calibration <span>{stats.calibrated_games} live games; on the dotted line = well calibrated</span></figcaption>
          {#if stats.calibration.length}<EChart option={calibOption} label="Predicted against actual win rate" />
          {:else}<p class="none">Needs games logged live with a prediction.</p>{/if}
        </figure>
        <figure class="wide">
          <figcaption>What decided each game <span>draft, early game and conversion, pp of a win</span></figcaption>
          {#if execOption}<EChart option={execOption} label="Draft, early and conversion share per game" />
          {:else}<p class="none">Needs timelines: run Confirm games (postgame) and the execution fit.</p>{/if}
        </figure>
        {#each splits as [title, rows]}
          <figure>
            <figcaption>Win rate by {title.toLowerCase()} <span>whiskers: 95% interval</span></figcaption>
            <EChart option={splitOption(rows)} height={Math.max(90, 34 * rows.filter((r) => r.games).length + 30)} label="Win rate by {title}" />
          </figure>
        {/each}
      </div>

      <h3>Players <span>roster players and the champions in their pool: games, win rate and gold lead at 15</span></h3>
      <div class="players">
        {#each stats.players as p}
          <div class="player">
            <div class="phead"><strong>{p.player}</strong> <span>{p.role_label} · <span class="mono">{p.games}</span> games · <span class="mono">{pct(p.wr)}</span></span></div>
            <Table dense sticky={false}>
              <thead><tr><th scope="col">Champion</th><th scope="col" class="num">Games</th><th scope="col" class="num">Won</th><th scope="col" class="num" colspan="2">Gold at 15</th></tr></thead>
              <tbody>
                {#each p.champions.slice(0, 6) as c}
                  <tr>
                    <td><span class="c"><Champ champ={c.champ} size={20} />{c.champ?.name}</span></td>
                    <td class="num mono">{c.games}</td>
                    <td class="num mono" title="95% interval {ci(c)}">{pct(c.wr)}</td>
                    <td class="gold" title={c.gold15 == null ? 'No timeline' : `${signed(c.gold15)} gold at 15 over ${c.gold_games} game${c.gold_games === 1 ? '' : 's'}`}>
                      {#if c.gold15 != null}<span class="gbar" style="--w: {(50 * Math.abs(c.gold15)) / goldMax}%" class:neg={c.gold15 < 0}></span>{/if}
                    </td>
                    <td class="num mono gv" class:up={c.gold15 > 0} class:down={c.gold15 < 0}>{c.gold15 == null ? '—' : signed(c.gold15)}</td>
                  </tr>
                {:else}
                  <tr><td colspan="5" class="quiet">No games on champions in their {p.role_label} pool</td></tr>
                {/each}
              </tbody>
            </Table>
            {#if p.other_games}<p class="other">{p.other_games === 1 ? '1 game' : `${p.other_games} games`} on champions outside their pool</p>{/if}
          </div>
        {:else}
          <p class="other">No games by players on the Roster in these games.</p>
        {/each}
      </div>

      {#if stats.scouting}
        <h3>Scouting <span>Ranked 5s games where the enemy was scouted</span></h3>
        <p class="scoutline">{[
          `${stats.scouting.all_done} of ${stats.scouting.games} games with all five fully read (${stats.scouting.all_found} with all five found)`,
          stats.scouting.median_found_s != null ? `all five had data after ${Math.round(stats.scouting.median_found_s)} s` : null,
          stats.scouting.median_done_s != null ? `fully read after ${Math.round(stats.scouting.median_done_s)} s (median)` : null,
          stats.scouting.missing ? `${stats.scouting.missing} names not found` : null,
          stats.scouting.errors ? `${stats.scouting.errors} fetch errors` : null].filter(Boolean).join(' · ')}</p>
      {/if}

      {#if stats.bans.length}
        <h3>Our bans <span>most banned: games and our win rate in them (overall {pct(stats.overall.wr)})</span></h3>
        <div class="bans">
          {#each stats.bans as b}
            <div class="ban" title="95% interval {ci(b)}">
              <Champ champ={b.champ} size={28} banned />
              <span class="mono">{b.games} g · {pct(b.wr)}</span>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
  </div>
  {/if}
</section>

<style>
  .overview { padding-bottom: 24px; margin-bottom: 20px; border-bottom: 1px solid var(--hairline); }
  .headrow { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  h2 { flex: 1; margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .stats { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 16px 48px; }
  .cap { margin: 8px 0 0; font-size: 12px; color: var(--text-3); }
  .none { margin: 12px 0 0; font-size: 13px; color: var(--text-3); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px 32px; margin-top: 24px; }
  figure { margin: 0; min-width: 0; }
  figure.wide { grid-column: span 2; }
  @media (max-width: 820px) { figure.wide { grid-column: auto; } }
  figcaption { font-size: 13px; font-weight: 600; color: var(--text-1); margin-bottom: 4px; }
  figcaption span, h3 span, .phead > span { color: var(--text-3); font-size: 12px; font-weight: 400; margin-left: 6px; }
  h3 { font-size: 14px; font-weight: 650; margin: 28px 0 8px; }
  .players { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px 32px; }
  .phead { margin-bottom: 2px; font-size: 13px; }
  .phead strong { font-weight: 600; }
  .phead .mono { color: var(--text-2); }
  .c { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  td.gold { position: relative; width: 64px; padding: 0; }
  td.gold::before { content: ''; position: absolute; left: 50%; top: 6px; bottom: 6px; border-left: 1px solid var(--hairline-strong); }
  td.gv { width: 4.5em; color: var(--text-3); }
  .gbar { position: absolute; top: 11px; bottom: 11px; left: 50%; width: var(--w); background: var(--up); border-radius: 0 2px 2px 0; }
  .gbar.neg { left: auto; right: 50%; background: var(--down); border-radius: 2px 0 0 2px; }
  .scoutline { font-size: 13px; margin: 0; color: var(--text-2); }
  .bans { display: flex; flex-wrap: wrap; gap: 10px 20px; }
  .ban { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-2); }
  .other { margin: 6px 0 0; font-size: 12px; color: var(--text-3); }
  .quiet { color: var(--text-3); }
</style>
