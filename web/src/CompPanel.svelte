<script>
  // One team's comp: comp type, fight-style shares over the five identities and confidence (picks / 5).
  // Identity colours: categorical slots 4-8 in the documented order (slots 1-3 are Threat / Matchup / Comp on
  // this screen), validated as an adjacent stack on the panel surface #161b22 and again on #101a30 (palette.js) (dataviz validator: all checks
  // pass). Every segment is also labelled with its share in text and listed in the tooltip, so colour is
  // never the only signal.
 import Stat from './ui/Stat.svelte';
  let { comp, title } = $props();
  const STYLES = [
    ['engage', 'Engage', 'Eng'],
    ['poke', 'Poke', 'Poke'],
    ['pick', 'Pick', 'Pick'],
    ['protect', 'Protect', 'Prot'],
    ['split', 'Split', 'Split'],
  ];
  const pct = (x) => `${Math.round(x * 100)}%`;
  const tip = $derived(STYLES.map(([k, label]) => `${label} ${pct(comp.shares[k] ?? 0)}`).join(' · '));
</script>

<section class="comp">
  <div class="head">
    <span class="t">{title}</span>
    <span class="conf" title="{comp.picks} of 5 picks: labels settle as the comp fills">
      {#each [0, 1, 2, 3, 4] as i}<i class:on={i < comp.picks}></i>{/each}
    </span>
  </div>
 <div class="labels">
 <Stat size="sm" label="Type" value={comp.picks && comp.timing ? comp.timing_label : '—'} tone={comp.timing === 'early' ? 'down' : null}
 title={comp.curve == null ? 'No timing data yet' : `Mean power curve ${comp.curve > 0 ? '+' : ''}${comp.curve}`} />
 <Stat size="sm" label="Style" value={comp.picks ? comp.style_label : '—'} />
  </div>
  {#if comp.picks}
    <div class="bar" title={tip} aria-label={tip} role="img">
      {#each STYLES as [k]}
        {#if comp.shares[k] > 0}<span class="seg {k}" style="flex-grow:{comp.shares[k]}"></span>{/if}
      {/each}
    </div>
    <div class="legend">
      {#each STYLES.filter(([k]) => (comp.shares[k] ?? 0) >= 0.005) as [k, , short]}<span><i class="key {k}"></i>{short} {pct(comp.shares[k] ?? 0)}</span>{/each}
    </div>
  {/if}
  {#if comp.target}
    <div class="target" title={comp.target.source === 'team' ? 'Set on the Team page' : 'The style our comp is heading for'}>
      Picks lean {comp.target.label} <small>({comp.target.source === 'team' ? 'team style' : 'from our picks'})</small>
    </div>
  {/if}
</section>

<style>
 .comp { padding: 12px 0 0; margin-top: 12px; border-top: 1px solid var(--hairline); }
  .head { display: flex; justify-content: space-between; align-items: center; }
 .t { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; }
  .conf { display: inline-flex; gap: 3px; }
 .conf i { width: 10px; height: 4px; background: var(--hairline); }
 .conf i.on { background: var(--text-3); }
 .labels { display: flex; gap: 24px; align-items: baseline; margin-top: 8px; }
  .bar { display: flex; gap: 2px; height: 6px; margin-top: 6px; cursor: help; }
  .seg { min-width: 3px; }
  .seg:first-child { border-radius: 4px 0 0 4px; }
  .seg:last-child { border-radius: 0 4px 4px 0; }
 .legend { display: flex; flex-wrap: wrap; column-gap: 10px; margin-top: 4px; font-size: 11px; color: var(--text-3); font-variant-numeric: tabular-nums; }
 .target { margin-top: 6px; font-size: 12px; color: var(--text-3); }
  .target small { font-size: 11px; }
  .key { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; }
 .engage { background: var(--viz-engage); }
 .poke { background: var(--viz-poke); }
 .pick { background: var(--viz-pick); }
 .protect { background: var(--viz-protect); }
 .split { background: var(--viz-split); }
</style>
