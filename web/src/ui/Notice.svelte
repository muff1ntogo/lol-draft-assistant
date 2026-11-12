<script module>
  // Most urgent first (docs/ui-redesign.md, step 2; the order of setup and update was decided on 12 Nov 2026)
  export const NOTICE_ORDER = ['offline', 'error', 'setup', 'update', 'fixture'];
</script>

<script>
  // The app's messages as one strip at the top of the content column. Only the most urgent shows; the rest wait
  // behind "+N more", which opens them underneath. Offline and errors carry ⚠ and are announced at once.
  // items: [{ id, kind, text, detail?, progress? (0..1), actions?: [{ label, onclick?, href?, disabled?, title? }],
  //           ondismiss? }]
  // `compact` is the one-line form for Live, under the Draft / Strategy switch.
  import Button from './Button.svelte';
  import { reveal } from '../motion.js';

  let { items = [], compact = false } = $props();
  const rank = (kind) => { const i = NOTICE_ORDER.indexOf(kind); return i < 0 ? NOTICE_ORDER.length : i; };
  const sorted = $derived([...items].sort((a, b) => rank(a.kind) - rank(b.kind)));
  let open = $state(false);
  const visible = $derived(open ? sorted : sorted.slice(0, 1));
  const urgent = (kind) => kind === 'offline' || kind === 'error';
  $effect(() => { if (sorted.length < 2) open = false; });
</script>

{#if sorted.length}
  <div class="notices" class:compact>
    {#each visible as n, i (n.id)}
      <div class="notice {n.kind}" role={urgent(n.kind) ? 'alert' : 'status'} transition:reveal>
        <span class="text">{#if urgent(n.kind)}<span class="mark" aria-hidden="true">⚠</span>{/if}{n.text}{#if n.detail}<span class="detail">· {n.detail}</span>{/if}</span>
        {#if n.progress != null}
          <span class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(n.progress * 100)}>
            <span style:transform="scaleX({Math.max(0, Math.min(1, n.progress))})"></span></span>
        {/if}
        <span class="acts">
          {#each n.actions ?? [] as a}
            <Button variant="secondary" href={a.href} disabled={a.disabled} title={a.title} onclick={a.onclick}>{a.label}</Button>
          {/each}
          {#if n.ondismiss}<Button variant="ghost" onclick={n.ondismiss}>Hide</Button>{/if}
          {#if i === 0 && sorted.length > 1}
            <Button variant="ghost" aria-expanded={open} onclick={() => (open = !open)}>{open ? 'Show less' : `+${sorted.length - 1} more`}</Button>
          {/if}
        </span>
      </div>
    {/each}
  </div>
{/if}

<style>
  .notices { background: var(--surface-1); border-bottom: 1px solid var(--hairline); }
  .notice { display: flex; align-items: center; gap: 12px; min-height: 36px; padding: 4px 24px; font-size: 13px; color: var(--text-2); }
  .notice + .notice { border-top: 1px solid var(--hairline); }
  @media (max-width: 1180px) { .notice { padding: 4px 16px; } }
  .text { min-width: 0; }
  .mark { margin-right: 6px; }
  .detail { color: var(--text-3); margin-left: 6px; }
  .offline .text, .error .text { color: var(--error); }
  .offline .detail, .error .detail { color: var(--text-2); }
  .fixture .text { color: var(--text-3); }
  .acts { display: flex; align-items: center; gap: 4px; margin-left: auto; flex: none; }
  .bar { flex: none; width: 160px; height: 4px; border-radius: 2px; background: var(--surface-3); overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--text-2); transform-origin: left; transition: transform 200ms linear; }

  .compact { background: none; border: 0; }
  .compact .notice { min-height: 28px; padding: 0; }
  .compact .text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
