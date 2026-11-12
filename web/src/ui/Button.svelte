<script>
  // Buttons (docs/ui-redesign.md, colour rule 1): `primary` is lime and means "this is what the view asks of you", so
  // at most one per view; `secondary` is the everyday button; `ghost` is text only, for the least important actions.
  // A disabled primary is grey, never dim lime. `danger` marks an action that can't be undone (always with its word).
  // With `href` it renders a link that looks the same.
  let {
    variant = 'secondary', size = 'sm', danger = false, busy = false, disabled = false, href = null, type = 'button',
    children, ...rest
  } = $props();
</script>

{#if href && !disabled && !busy}
  <a class="btn {variant} {size}" class:danger {href} {...rest}>{@render children?.()}</a>
{:else}
  <button class="btn {variant} {size}" class:danger {type} disabled={disabled || busy} aria-busy={busy || undefined} {...rest}>
    {@render children?.()}
  </button>
{/if}

<style>
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 28px; padding: 0 12px;
         border: 1px solid transparent; border-radius: var(--radius-sm); font-size: 13px; font-weight: 500; line-height: 1;
         white-space: nowrap; text-decoration: none; cursor: pointer; user-select: none; }
  .btn.md { height: 32px; padding: 0 14px; font-size: 14px; }

  .secondary { background: var(--surface-2); border-color: var(--hairline); color: var(--text-1); }
  .secondary:not(:disabled):hover { background: var(--surface-3); border-color: var(--hairline-strong); filter: none; }

  .primary { background: var(--signal); border-color: var(--signal); color: var(--signal-ink); font-weight: 600; }

  .ghost { background: none; color: var(--text-2); padding: 0 8px; }
  .ghost:not(:disabled):hover { background: var(--surface-2); color: var(--text-1); filter: none; }

  .danger.secondary, .danger.ghost { color: var(--error); }
  .danger.secondary:not(:disabled):hover { border-color: var(--error); }

  .btn:disabled { cursor: default; opacity: 1; }
  .secondary:disabled, .primary:disabled { background: var(--surface-1); border-color: var(--hairline); color: var(--text-3); }
  .ghost:disabled { color: var(--hairline-strong); }
  .btn[aria-busy="true"] { cursor: progress; }
  a.btn:active { transform: scale(0.97); }
</style>
