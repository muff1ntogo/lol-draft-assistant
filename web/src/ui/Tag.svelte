<script>
  // A word about a row. By default it is quiet text after a middle dot ("· Engage"): a tag describes, it doesn't ask
  // for attention. Only a tag you can click (a filter, a review flag) gets a border, and it says whether it's on.
  // `warn` puts ⚠ before the word, so the colour is never the only signal.
  let { onclick = null, pressed = null, warn = false, bare = false, title = null, children } = $props();
</script>

{#if onclick}
  <button type="button" class="tag filter" class:warn aria-pressed={pressed ?? undefined} {title} {onclick}>
    {#if warn}<span aria-hidden="true">⚠</span>{/if}{@render children?.()}
  </button>
{:else}
  <span class="tag quiet" class:warn class:bare {title}>{#if warn}<span class="mark" aria-hidden="true">⚠</span>{/if}{@render children?.()}</span>
{/if}

<style>
  .tag { font-size: 11px; font-weight: 600; letter-spacing: 0.02em; white-space: nowrap; }
  .quiet { color: var(--text-2); }
  .quiet:not(.bare)::before { content: "· "; color: var(--text-3); }
  .quiet.warn { color: var(--warn); }
  .mark { margin-right: 4px; }

  .filter { display: inline-flex; align-items: center; gap: 4px; height: 20px; padding: 0 6px; border-radius: var(--radius-sm);
            background: none; border: 1px solid var(--hairline-strong); color: var(--text-2); }
  .filter:hover { color: var(--text-1); border-color: var(--text-3); filter: none; }
  .filter[aria-pressed="true"] { background: var(--surface-3); border-color: var(--text-3); color: var(--text-1); }
  .filter.warn { color: var(--warn); }
</style>
