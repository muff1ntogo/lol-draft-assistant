<script>
  let { champ, size = 56, faded = false, banned = false } = $props();
</script>

<span class="champ" class:faded class:banned style="width:{size}px;height:{size}px" title={champ?.name}>
  {#if champ?.icon}
    <img src={champ.icon} alt={champ.name} width={size} height={size} />
  {:else}
    <span class="initials" style="font-size:{size * 0.35}px">{champ?.name?.slice(0, 2) ?? ''}</span>
  {/if}
</span>

<style>
  .champ { position: relative; display: inline-grid; place-items: center; flex: none;
           border-radius: 6px; overflow: hidden; background: #2d3540; }
  img { width: 100%; height: 100%; }
  .initials { font-weight: 700; color: #8b96a3; }
  .faded { opacity: 0.55; }
  .banned { filter: grayscale(1) brightness(0.7); }
  .banned::after { content: ""; position: absolute; inset: 0;
                   background: linear-gradient(to top right, transparent 46%, #ff6b6b 48% 52%, transparent 54%); }
</style>
