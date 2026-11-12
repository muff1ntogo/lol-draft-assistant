<script>
  // A labelled set of controls inside a Toolbar. `end` pushes it (and what follows) to the right edge.
  let { label = '', end = false, children } = $props();
</script>

<div class="group" class:end role={label ? 'group' : undefined} aria-label={label || undefined}>
  {#if label}<span class="label-caps">{label}</span>{/if}
  {@render children?.()}
</div>

<style>
  .group { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .group.end { margin-left: auto; }
  .label-caps { white-space: nowrap; }
</style>
