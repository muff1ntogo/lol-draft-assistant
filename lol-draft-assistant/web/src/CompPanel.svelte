<script>
  // One team's comp (docs/comp-style.md): comp type, fight-style shares and confidence (picks / 5).
  // Style colours: categorical slots 5-7 (slots 1-3 are taken by Threat / Matchup / Comp on this screen),
  // validated all-pairs on the panel surface #161b22. Every segment is also labelled with its share in text
  // colour and listed in the tooltip, so colour is never the only signal.
  let { comp, title } = $props();
  const STYLES = [
    ['front_to_back', 'Front to back', 'FtB'],
    ['dive', 'Dive', 'Dive'],
    ['poke', 'Poke', 'Poke'],
  ];
  const pct = (x) => `${Math.round(x * 100)}%`;
  const tip = $derived(STYLES.map(([k, label]) => `${label} ${pct(comp.shares[k] ?? 0)}`).join(' · '));
</script>

<div class="comp">
  <div class="head">
    <span class="t">{title}</span>
    <span class="conf" title="{comp.picks} of 5 picks: labels settle as the comp fills">
      {#each [0, 1, 2, 3, 4] as i}<i class:on={i < comp.picks}></i>{/each}
    </span>
  </div>
  <div class="labels">
    <span class="field"><small>Type</small>
      <span class="timing {comp.timing ?? ''}" title={comp.curve == null ? 'No timing data yet' : `Mean power curve ${comp.curve > 0 ? '+' : ''}${comp.curve}`}>
        {comp.picks && comp.timing ? comp.timing_label : '—'}</span></span>
    <span class="field"><small>Style</small>
      <span class="style" class:mixed={comp.style === 'mixed'}>{comp.picks ? comp.style_label : '—'}</span></span>
  </div>
  {#if comp.picks}
    <div class="bar" title={tip} aria-label={tip} role="img">
      {#each STYLES as [k]}
        {#if comp.shares[k] > 0}<span class="seg {k}" style="flex-grow:{comp.shares[k]}"></span>{/if}
      {/each}
    </div>
    <div class="legend">
      {#each STYLES as [k, , short]}<span><i class="key {k}"></i>{short} {pct(comp.shares[k] ?? 0)}</span>{/each}
    </div>
  {/if}
  {#if comp.target}
    <div class="target" title={comp.target.source === 'team' ? 'Set on the Team page' : 'The style our comp is heading for'}>
      Picks lean {comp.target.label} <small>({comp.target.source === 'team' ? 'team style' : 'from our picks'})</small>
    </div>
  {/if}
</div>

<style>
  .comp { background: var(--panel); border-radius: 8px; padding: 8px 12px; margin-top: 8px; }
  .head { display: flex; justify-content: space-between; align-items: center; }
  .t { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .conf { display: inline-flex; gap: 3px; }
  .conf i { width: 10px; height: 6px; border-radius: 2px; background: #2d3540; }
  .conf i.on { background: var(--muted); }
  .labels { display: flex; gap: 18px; align-items: baseline; margin-top: 2px; font-size: 20px; font-weight: 700; }
  .field { display: flex; flex-direction: column; }
  .field small { font-size: 11px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .timing.early { color: #e0a640; }
  .timing.late { color: #79b8ff; }
  .style.mixed { color: var(--muted); font-weight: 500; }
  .bar { display: flex; gap: 2px; height: 6px; margin-top: 6px; cursor: help; }
  .seg { min-width: 3px; }
  .seg:first-child { border-radius: 4px 0 0 4px; }
  .seg:last-child { border-radius: 0 4px 4px 0; }
  .legend { display: flex; gap: 10px; margin-top: 4px; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .target { margin-top: 4px; font-size: 12px; color: var(--muted); }
  .target small { font-size: 11px; }
  .key { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; }
  .front_to_back { background: #9085e9; }
  .dive { background: #d55181; }
  .poke { background: #008300; }
</style>
