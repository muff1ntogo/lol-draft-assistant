<script>
  // A choice between modes of one view (Plan / Matchup tree, Blue / Red, 10 / 20 / all). It is never lime: choosing a
  // mode is not the action the view asks for. One tab stop; the arrow keys move the choice, as in a radio group.
  // options: [{ value, label, hint?, disabled?, title? }] or [value, label] pairs.
  let { options, value = $bindable(), label, onchange = null, disabled = false } = $props();

  const items = $derived(options.map((o) => (Array.isArray(o) ? { value: o[0], label: o[1] } : o)));
  let root;

  function choose(v) {
    if (v === value) return;
    value = v;
    onchange?.(v);
  }
  function onkeydown(e) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    const usable = items.filter((o) => !o.disabled);
    if (!usable.length || (!step && e.key !== 'Home' && e.key !== 'End')) return;
    e.preventDefault();
    const at = usable.findIndex((o) => o.value === value);
    const next = e.key === 'Home' ? usable[0] : e.key === 'End' ? usable[usable.length - 1]
      : usable[(at + step + usable.length) % usable.length];
    choose(next.value);
    root.querySelector(`[data-i="${items.indexOf(next)}"]`)?.focus();
  }
  // the selected segment is the tab stop; with nothing selected, the first usable one
  const stop = $derived(items.some((o) => o.value === value && !o.disabled) ? value : items.find((o) => !o.disabled)?.value);
</script>

<div class="seg" role="radiogroup" aria-label={label} aria-disabled={disabled || undefined} bind:this={root} {onkeydown}>
  {#each items as o, i}
    <button type="button" role="radio" aria-checked={o.value === value} class:on={o.value === value} data-i={i}
            disabled={disabled || o.disabled} title={o.title} tabindex={o.value === stop ? 0 : -1}
            onclick={() => choose(o.value)}>{o.label}{#if o.hint}<kbd>{o.hint}</kbd>{/if}</button>
  {/each}
</div>

<style>
  .seg { display: inline-flex; align-items: stretch; height: 28px; padding: 2px; gap: 2px; border-radius: var(--radius-sm);
         background: var(--surface-1); box-shadow: inset 0 0 0 1px var(--hairline); }
  button { display: inline-flex; align-items: center; gap: 6px; padding: 0 10px; border: 0; border-radius: 3px;
           background: none; color: var(--text-3); font-size: 13px; font-weight: 500; white-space: nowrap; }
  button:not(:disabled):hover { color: var(--text-1); filter: none; }
  button:not(:disabled):active { transform: none; filter: none; background: var(--surface-2); }
  button.on { background: var(--surface-3); color: var(--text-1); font-weight: 600; }
  button.on:not(:disabled):active { background: var(--surface-3); }
  button:disabled { opacity: 1; color: var(--hairline-strong); }
  button.on:disabled { color: var(--text-2); }
  kbd { font-family: var(--font-mono); font-size: 10px; font-weight: 500; color: var(--text-2);
        border: 1px solid var(--hairline-strong); border-radius: 3px; padding: 0 4px; line-height: 14px; }
</style>
