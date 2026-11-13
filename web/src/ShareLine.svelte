<script>
  // A player's share of the early game over their last team games (docs/roster-profiles.md, the profile's lane
  // execution). One series, so the title names it and the line is neutral ink; a dashed zero line says which side of
  // "as expected" each game fell. Hover or focus a game for its value.
  import { signed } from './format.js';

  let { values, label = 'Lane execution, last team games' } = $props(); // pp of a win, oldest first
  const W = 520, H = 132, PL = 44, PR = 12, PT = 12, PB = 22;
  const span = $derived(Math.max(5, Math.ceil(Math.max(...values.map(Math.abs)) / 5) * 5));
  const x = (i) => PL + (i * (W - PL - PR)) / Math.max(values.length - 1, 1);
  const y = (v) => PT + ((span - v) * (H - PT - PB)) / (2 * span);
  const path = $derived(values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(''));
  let hover = $state(null);
  let svg;
  function move(e) {
    const r = svg.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    hover = values.reduce((best, _, i) => (Math.abs(x(i) - vx) < Math.abs(x(best) - vx) ? i : best), 0);
  }
  function key(e) {
    const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (!step) return;
    e.preventDefault();
    hover = Math.max(0, Math.min(values.length - 1, (hover ?? values.length - 1) + step));
  }
</script>

<figure>
  <div class="wrap">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
    <svg bind:this={svg} viewBox="0 0 {W} {H}" role="img" aria-label="{label}: {values.map((v) => signed(v, 1)).join(', ')} pp" tabindex="0"
         onmousemove={move} onmouseleave={() => (hover = null)} onkeydown={key} onblur={() => (hover = null)}>
      {#each [span, 0, -span] as g}
        <line class="grid" class:zero={g === 0} x1={PL} x2={W - PR} y1={y(g)} y2={y(g)} />
        <text class="tick" x={PL - 8} y={y(g) + 4} text-anchor="end">{signed(g, 0)}pp</text>
      {/each}
      <text class="tick" x={PL} y={H - 4}>oldest</text>
      <text class="tick" x={W - PR} y={H - 4} text-anchor="end">latest</text>
      <path class="line" d={path} />
      {#each values as v, i}<circle class="dot" cx={x(i)} cy={y(v)} r={hover === i ? 5 : 2.5} />{/each}
      {#if hover !== null}<line class="cross" x1={x(hover)} x2={x(hover)} y1={PT} y2={H - PB} />{/if}
    </svg>
    {#if hover !== null}
      <div class="tip" style="left:{(x(hover) / W) * 100}%">Game {hover + 1} of {values.length} · <span class="mono">{signed(values[hover], 1)}pp</span></div>
    {/if}
  </div>
</figure>

<style>
  figure { margin: 0; }
  .wrap { position: relative; max-width: 560px; }
  svg { width: 100%; height: auto; display: block; border-radius: var(--radius-sm); }
  svg:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
  .grid { stroke: var(--hairline); stroke-width: 1; }
  .grid.zero { stroke: var(--hairline-strong); stroke-dasharray: 4 4; }
  .tick { fill: var(--text-3); font-size: 10px; font-family: var(--font-mono); }
  .line { fill: none; stroke: var(--text-2); stroke-width: 2; stroke-linejoin: round; }
  .dot { fill: var(--text-1); stroke: var(--bg); stroke-width: 1.5; }
  .cross { stroke: var(--text-3); stroke-width: 1; }
  .tip { position: absolute; top: -6px; transform: translate(-50%, -100%); pointer-events: none; white-space: nowrap; padding: 4px 8px; font-size: 12px;
         background: var(--surface-2); border: 1px solid var(--hairline-strong); border-radius: var(--radius-sm); color: var(--text-1); }
</style>
