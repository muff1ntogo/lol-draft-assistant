<script>
  // Share of a ban's value per component (Threat / Matchup / Comp). Colours: categorical slots 1-3
  // (dark steps), validated on the panel surface #161b22. Negative components shrink the total but
  // have no segment; the tooltip lists all three signed values, so colour is never the only signal.
  let { components } = $props();
  const NAMES = [
    ['threat', 'Threat'],
    ['matchup', 'Matchup'],
    ['comp', 'Comp'],
  ];
  const positive = $derived(NAMES.map(([k]) => Math.max(components?.[k]?.weighted ?? 0, 0)));
  const total = $derived(positive.reduce((a, b) => a + b, 0));
  const fmt = (x) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(3)}`;
  const tip = $derived(
    NAMES.map(([k, label]) => `${label} ${fmt(components?.[k]?.weighted ?? 0)}`).join(' · ') +
      (components?.own?.weighted ? ` · Self-ban −${Math.abs(components.own.weighted).toFixed(3)}` : '') +
      ' (log-odds, weighted)',
  );
</script>

{#if components && total > 0}
  <div class="bar" title={tip} aria-label={tip} role="img">
    {#each NAMES as [key, label], i}
      {#if positive[i] > 0}
        <span class="seg {key}" style="flex-grow:{positive[i] / total}"></span>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .bar { display: flex; gap: 2px; height: 6px; margin-top: 8px; width: 100%; max-width: 320px; cursor: help; }
  .seg { min-width: 3px; }
  .seg:last-child { border-radius: 0 4px 4px 0; }
  .threat { background: #3987e5; }
  .matchup { background: #d95926; }
  .comp { background: #199e70; }
</style>
