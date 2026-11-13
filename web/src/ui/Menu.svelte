<script>
  // A "more" button that opens a short list of the actions that shouldn't sit one click away (Delete).
  // items: [{ label, onclick, danger?, disabled?, title? }]. Arrow keys move, Escape closes, a click outside closes.
  import { pop } from '../motion.js';

  let { items, label = 'More actions', align = 'right' } = $props();
  let open = $state(false);
  let root = $state();
  const uid = $props.id();

  function toggle() {
    open = !open;
    if (open) queueMicrotask(() => root?.querySelector('[role="menuitem"]:not(:disabled)')?.focus());
  }
  function close(refocus = true) {
    open = false;
    if (refocus) root?.querySelector('.more')?.focus();
  }
  function onkeydown(e) {
    if (!open) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const els = [...root.querySelectorAll('[role="menuitem"]:not(:disabled)')];
    els[(els.indexOf(document.activeElement) + step + els.length) % els.length]?.focus();
  }
  function choose(item) {
    close();
    item.onclick?.();
  }
</script>

<svelte:window onpointerdown={(e) => open && !root?.contains(e.target) && close(false)} />

<span class="menu" bind:this={root} {onkeydown} role="presentation">
  <button type="button" class="more" aria-haspopup="menu" aria-expanded={open} aria-controls="menu-{uid}" aria-label={label} title={label}
          onclick={toggle}><span aria-hidden="true">⋯</span></button>
  {#if open}
    <div class="list {align}" id="menu-{uid}" role="menu" aria-label={label} transition:pop={{ origin: align === 'right' ? 'top right' : 'top left' }}>
      {#each items as item}
        <button type="button" role="menuitem" class:danger={item.danger} disabled={item.disabled} title={item.title} onclick={() => choose(item)}>{item.label}</button>
      {/each}
    </div>
  {/if}
</span>

<style>
  .menu { position: relative; display: inline-flex; }
  .more { width: 28px; height: 28px; padding: 0; border: 1px solid transparent; background: none; color: var(--text-2); font-size: 16px; line-height: 1; }
  .more:hover, .more[aria-expanded="true"] { background: var(--surface-2); color: var(--text-1); filter: none; }
  .list { position: absolute; top: calc(100% + 4px); z-index: 30; min-width: 160px; padding: 4px; display: flex; flex-direction: column;
          background: var(--surface-2); border: 1px solid var(--hairline-strong); border-radius: var(--radius-sm); box-shadow: var(--shadow); }
  .list.right { right: 0; }
  .list.left { left: 0; }
  .list button { height: 28px; padding: 0 10px; border: 0; border-radius: 3px; background: none; color: var(--text-1); font-size: 13px; text-align: left; white-space: nowrap; }
  .list button:not(:disabled):hover, .list button:focus-visible { background: var(--surface-3); filter: none; }
  .list button.danger { color: var(--error); }
  .list button:disabled { color: var(--text-3); opacity: 1; }
</style>
