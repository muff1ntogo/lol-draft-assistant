<script>
  // A raised area for one self-contained thing (a form, a chart with its controls). Panels never nest: a Panel placed
  // inside another one turns into a section under a hairline rule, so boxes can't pile up by accident.
  import { getContext, setContext } from 'svelte';

  let { title = '', actions = null, flush = false, children } = $props();
  const nested = getContext('ui-panel') === true;
  setContext('ui-panel', true);
</script>

<section class="panel" class:nested class:flush>
  {#if title || actions}
    <div class="head">
      {#if title}<h2>{title}</h2>{/if}
      {#if actions}<div class="actions">{@render actions()}</div>{/if}
    </div>
  {/if}
  {@render children?.()}
</section>

<style>
  /* tables and controls inside read these, so their sticky headers and hovers match the surface they sit on */
  .panel { --table-bg: var(--surface-1); --table-hover: var(--surface-2);
           background: var(--surface-1); border-radius: var(--radius); padding: 16px; min-width: 0; }
  .panel.flush { padding: 0; overflow: hidden; }
  .panel.nested { background: none; border-radius: 0; padding: 16px 0 0; margin-top: 16px; border-top: 1px solid var(--hairline); }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; min-height: 28px; }
  .flush > .head { padding: 16px 16px 0; }
  h2 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .actions { display: flex; align-items: center; gap: 8px; }
</style>
