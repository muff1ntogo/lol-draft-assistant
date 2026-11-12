<script>
  // One row of controls above a view. Each set of controls is a ToolGroup with its label before it; the row wraps on
  // narrow widths, a group never splits from its label.
  let { label = 'View options', children } = $props();
</script>

<div class="toolbar" role="group" aria-label={label}>{@render children?.()}</div>

<style>
  .toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 20px; min-height: 28px; padding: 0 0 12px; }
</style>
