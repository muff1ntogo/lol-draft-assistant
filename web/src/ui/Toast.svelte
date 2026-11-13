<script>
  // Draws the app's toast (toast.svelte.js) at the foot of the window. It is announced politely and never takes focus;
  // its action is one Tab away for as long as it shows.
  import Button from './Button.svelte';
  import { rise } from '../motion.js';
  import { dismiss, toastState } from './toast.svelte.js';

  function act(t) {
    dismiss();
    t.action.onclick?.();
  }
</script>

<div class="region" role="status" aria-live="polite">
  {#if toastState.current}
    {@const t = toastState.current}
    {#key t.id}
      <div class="toast" in:rise={{ y: 8 }} out:rise={{ y: 8 }}>
        <span>{t.text}</span>
        {#if t.action}<Button variant="secondary" onclick={() => act(t)}>{t.action.label}</Button>{/if}
        <button type="button" class="x" aria-label="Dismiss" onclick={dismiss}>×</button>
      </div>
    {/key}
  {/if}
</div>

<style>
  .region { position: fixed; left: 0; right: 0; bottom: 24px; z-index: 50; display: flex; justify-content: center; pointer-events: none; }
  .toast { pointer-events: auto; display: flex; align-items: center; gap: 12px; max-width: min(560px, calc(100vw - 32px)); padding: 8px 8px 8px 16px;
           background: var(--surface-3); border: 1px solid var(--hairline-strong); border-radius: var(--radius); font-size: 13px; color: var(--text-1);
           box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5); }
  .x { width: 24px; height: 24px; padding: 0; border: 0; background: none; color: var(--text-3); font-size: 16px; line-height: 1; }
  .x:hover { color: var(--text-1); filter: none; }
</style>
