<script>
  // A sheet that slides in from the right for one task (add a player, swap, confirm a delete), or with `dialog` a
  // small box in the middle for a yes / no question. The page behind is inert while it is open. Escape and the
  // backdrop close it, Tab stays inside it, and focus goes back to whatever opened it.
  import { DUR, reducedMotion } from '../motion.js';

  // `step`: for a sheet with several steps, pass the step so focus moves to each new step's first field.
  let { open = $bindable(false), title, description = '', dialog = false, width = 480, step = null, onclose = null, footer = null, children } = $props();
  let panel = $state();
  let opener = null;

  function close() {
    open = false;
    onclose?.();
  }
  const focusable = () => [...panel.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')]
    .filter((el) => el.offsetParent !== null);
  // Escape closes it wherever focus is (a click on the sheet's own background leaves focus on the page)
  function onwindowkey(e) {
    if (open && e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); close(); }
  }
  function onkeydown(e) {
    if (e.key !== 'Tab') return;
    const els = focusable();
    if (!els.length) { e.preventDefault(); return; }
    const first = els[0], last = els[els.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  // the first field if there is one, else the sheet itself (so a screen reader starts at its title)
  const focusFirst = () => (panel?.querySelector('.body input:not([type="checkbox"]):not([type="radio"]):not(:disabled), .body select:not(:disabled), .body textarea:not(:disabled), .body input[type="radio"]:checked') ?? panel)?.focus();
  $effect(() => {
    if (!open || !panel) return;
    opener = document.activeElement;
    focusFirst();
    return () => { if (opener?.isConnected) opener.focus(); };
  });
 let lastStep = $state();
  $effect(() => {
 if (lastStep === undefined) { lastStep = step; return; }
    if (step === lastStep) return;
    lastStep = step;
    if (open) queueMicrotask(focusFirst); // after the new step's fields are in the page
  });
  function slide(node, _, { direction }) {
    const out = direction === 'out';
    return { duration: reducedMotion() ? 0 : out ? DUR.menu * 0.8 : DUR.enter,
             css: (t) => (dialog ? `opacity: ${t}; transform: scale(${0.97 + 0.03 * t});` : `opacity: ${Math.min(1, t * 2)}; transform: translateX(${(1 - t) * 24}px);`) };
  }
  const fade = (node, _, { direction }) => ({ duration: reducedMotion() ? 0 : direction === 'out' ? 120 : 160, css: (t) => `opacity: ${t};` });
</script>

<svelte:window onkeydown={onwindowkey} />

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
  <div class="backdrop" class:dialog transition:fade onclick={(e) => e.target === e.currentTarget && close()}>
    <div class="sheet" class:dialog bind:this={panel} role="dialog" aria-modal="true" aria-label={title} tabindex="-1"
         style:width={dialog ? null : `${width}px`} {onkeydown} transition:slide>
      <header>
        <div><h2>{title}</h2>{#if description}<p>{description}</p>{/if}</div>
        <button type="button" class="x" aria-label="Close" onclick={close}>×</button>
      </header>
      <div class="body">{@render children?.()}</div>
      {#if footer}<footer>{@render footer()}</footer>{/if}
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; z-index: 40; display: flex; justify-content: flex-end; background: var(--scrim); }
  .backdrop.dialog { align-items: center; justify-content: center; padding: 16px; }
  .sheet { display: flex; flex-direction: column; max-width: 100vw; height: 100%; background: var(--surface-1);
           border-left: 1px solid var(--hairline-strong); outline: none; }
  .sheet.dialog { width: min(440px, 100%); height: auto; max-height: 90vh; border: 1px solid var(--hairline-strong); border-radius: var(--radius); }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 20px 20px 12px; }
  h2 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  header p { margin: 4px 0 0; font-size: 13px; color: var(--text-3); }
  .x { flex: none; width: 28px; height: 28px; margin: -4px -6px 0 0; padding: 0; border: 0; background: none; color: var(--text-3); font-size: 18px; line-height: 1; }
  .x:hover { color: var(--text-1); background: var(--surface-2); filter: none; }
  .body { flex: 1; min-height: 0; overflow: auto; padding: 4px 20px 20px; --table-bg: var(--surface-1); --table-hover: var(--surface-2); }
  footer { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 12px 20px; border-top: 1px solid var(--hairline); }
</style>
