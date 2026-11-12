<script>
  // The top of every console page (docs/ui-redesign.md, step 1): the page's name, one line saying what it is for, and
  // its actions on the right. The page's one primary action lives here.
  let { title, description = '', actions = null } = $props();
</script>

<header class="ph">
  <div class="words">
    <h1>{title}</h1>
    {#if description}<p>{description}</p>{/if}
  </div>
  {#if actions}<div class="actions">{@render actions()}</div>{/if}
</header>

<style>
  .ph { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px 24px; flex-wrap: wrap;
        padding: 24px 0 16px; }
  .words { min-width: 0; }
  h1 { margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; }
  p { margin: 4px 0 0; font-size: 13px; color: var(--text-3); max-width: 96ch; }
  .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
</style>
