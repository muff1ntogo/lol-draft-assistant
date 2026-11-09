<script>
  import { pop } from './motion.js';
  // Model P(win) after each of the 20 draft steps. One series (no legend; the title names it),
  // 2px line, recessive gridlines, the biggest swing marked with a dot and a label, and a hover
  // crosshair + tooltip. Values outside 30-70% are pinned to the edge and say so in the tooltip.
  let { points, swingStep } = $props();
  const W = 760, H = 210, PL = 44, PR = 16, PT = 14, PB = 34;
  const Y0 = 0.3, Y1 = 0.7;
  const x = (i) => PL + (i * (W - PL - PR)) / Math.max(points.length - 1, 1);
  const y = (p) => PT + ((Y1 - Math.min(Math.max(p, Y0), Y1)) * (H - PT - PB)) / (Y1 - Y0);
  const path = $derived(points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.win_prob).toFixed(1)}`).join(''));
  const swingIndex = $derived(points.findIndex((p) => p.step === swingStep));
  let hover = $state(null);
  let svg;
  const pct = (p) => `${(p * 100).toFixed(1)}%`;
  const signedPct = (d) => `${d > 0 ? '+' : d < 0 ? '−' : ''}${Math.abs(d * 100).toFixed(1)}`;

  function move(e) {
    const r = svg.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    points.forEach((_, i) => { if (Math.abs(x(i) - vx) < Math.abs(x(best) - vx)) best = i; });
    hover = best;
  }
</script>

<figure class="timeline">
  <figcaption>Win probability through the draft</figcaption>
  <div class="wrap">
    <svg bind:this={svg} viewBox="0 0 {W} {H}" role="img" aria-label="Model win probability after each draft step"
         onmousemove={move} onmouseleave={() => (hover = null)}>
      {#each [0.3, 0.4, 0.5, 0.6, 0.7] as g}
        <line class="grid" class:mid={g === 0.5} x1={PL} x2={W - PR} y1={y(g)} y2={y(g)} />
        <text class="tick" x={PL - 8} y={y(g) + 4} text-anchor="end">{g * 100}%</text>
      {/each}
      {#each points as p, i}
        <text class="xlab" class:ban={p.type === 'ban'} x={x(i)} y={H - 12} text-anchor="middle">{p.label === 'ban' ? '·' : p.label}</text>
      {/each}
      <path class="line" d={path} />
      {#if hover !== null}
        <line class="cross" x1={x(hover)} x2={x(hover)} y1={PT} y2={H - PB} />
        <circle class="hoverdot" cx={x(hover)} cy={y(points[hover].win_prob)} r="5" />
      {/if}
      {#if swingIndex >= 0}
        {@const s = points[swingIndex]}
        <circle class="swing" cx={x(swingIndex)} cy={y(s.win_prob)} r="6" />
        <text class="swinglab" x={x(swingIndex) + (swingIndex > points.length - 4 ? -10 : 0)}
              y={s.delta < 0 ? y(s.win_prob) + 20 : y(s.win_prob) - 12}
              text-anchor={swingIndex > points.length - 4 ? 'end' : swingIndex < 3 ? 'start' : 'middle'}>
          biggest swing {signedPct(s.delta)}pp
        </text>
      {/if}
      <rect x={PL} y={PT} width={W - PL - PR} height={H - PT - PB} fill="transparent" />
    </svg>
    {#if hover !== null}
      {@const p = points[hover]}
      <!-- the tip follows the pointer exactly (never tweened); only its appearance animates, from the crosshair -->
      <div class="tip" style="left:{(x(hover) / W) * 100}%" in:pop={{ base: 'translateX(-50%)', origin: '50% 100%' }}
           out:pop={{ base: 'translateX(-50%)', origin: '50% 100%' }}>
        <strong>Step {p.step} · {p.label === 'ban' ? 'ban' : p.label}</strong>
        <span>{p.champ ? p.champ.name : 'skipped'} ({p.side})</span>
        <span>P(win) {pct(p.win_prob)} ({signedPct(p.delta)}pp){#if p.win_prob < 0.3 || p.win_prob > 0.7} · off scale{/if}</span>
      </div>
    {/if}
  </div>
</figure>

<style>
  .timeline { margin: 0; }
  figcaption { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
  .wrap { position: relative; }
  svg { width: 100%; height: auto; display: block; }
  .grid { stroke: var(--line); stroke-width: 1; }
  .grid.mid { stroke: var(--line-strong); stroke-dasharray: 4 4; }
  .tick, .xlab { fill: var(--muted); font-size: 12px; }
  .xlab.ban { font-size: 16px; }
  .line { fill: none; stroke: var(--gold); stroke-width: 2; stroke-linejoin: round; }
  .cross { stroke: var(--muted); stroke-width: 1; }
  .hoverdot { fill: var(--gold); stroke: var(--panel); stroke-width: 2; }
  .swing { fill: var(--gold); stroke: var(--panel); stroke-width: 2; }
  .swinglab { fill: var(--text); font-size: 13px; font-weight: 600; }
  .tip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; display: flex; flex-direction: column;
         background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font-size: 13px; white-space: nowrap; }
</style>
