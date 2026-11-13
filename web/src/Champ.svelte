<script>
  let { champ, size = 56, faded = false, banned = false } = $props();
  let failed = $state(false);
  $effect(() => {
    champ?.icon;
    failed = false;
  });
</script>

<span class="champ" class:faded class:banned style="width:{size}px;height:{size}px" title={champ?.name}>
  {#if champ?.icon && !failed}
    <img src={champ.icon} alt={champ.name} width={size} height={size} onerror={() => (failed = true)} />
  {:else}
    <span class="initials" style="font-size:{Math.max(10, size * 0.22)}px">{champ?.name ?? ''}</span>
  {/if}
</span>

<style>
  .champ { position: relative; display: inline-grid; place-items: center; flex: none;
 border-radius: 6px; overflow: hidden; background: var(--hairline); }
  img { width: 100%; height: 100%; }
  .initials { font-weight: 700; color: var(--text-2); text-align: center; line-height: 1.05; padding: 2px; word-break: break-word; }
  .faded { opacity: 0.55; }
  .banned { filter: grayscale(1) brightness(0.7); }
  .banned::after { content: ""; position: absolute; inset: 0;
 background: linear-gradient(to top right, transparent 46%, var(--error) 48% 52%, transparent 54%); }
</style>
