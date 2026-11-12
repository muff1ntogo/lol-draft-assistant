<script>
  // A figure with its name under it. The optional delta is the only coloured part (colour rule 3): signed, teal ahead,
  // orange behind, and it always carries its + or −. A missing value is a dash, with `note` to say why.
  import { signed } from '../format.js';

  let {
    value, label, unit = '', delta = null, deltaDigits = 1, deltaUnit = '', note = '', size = 'md', title = null,
  } = $props();
  const missing = $derived(value == null || value === '');
  const deltaText = $derived(delta == null ? '' : signed(delta, deltaDigits));
</script>

<div class="stat {size}" {title}>
  <div class="line">
    <span class="value mono" class:missing>{missing ? '—' : value}{#if unit && !missing}<small>{unit}</small>{/if}</span>
    {#if delta != null}
      <span class="delta mono" class:up={deltaText.startsWith('+')} class:down={deltaText.startsWith('−')}>{deltaText}{deltaUnit}</span>
    {/if}
  </div>
  <div class="label-caps">{label}</div>
  {#if note}<div class="note">{note}</div>{/if}
</div>

<style>
  .stat { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .line { display: flex; align-items: baseline; gap: 8px; }
  .value { font-size: 32px; font-weight: 600; line-height: 1; letter-spacing: -0.04em; color: var(--text-1); }
  .sm .value { font-size: 20px; letter-spacing: -0.02em; }
  .value small { font-size: 0.55em; font-weight: 500; color: var(--text-3); margin-left: 1px; letter-spacing: 0; }
  .value.missing { color: var(--text-3); }
  .delta { font-size: 13px; font-weight: 600; color: var(--text-3); }
  .delta.up { color: var(--up-text); }
  .delta.down { color: var(--down-text); }
  .note { font-size: 12px; color: var(--text-3); }
</style>
