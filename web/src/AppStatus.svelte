<script>
  // Banners across the app: an update to install (updates.py), the first-run setup (first_run.py), a missing Riot key.
  let { inDraft = false } = $props();
  let update = $state(null);
  let setup = $state(null);
  let installing = $state(false);
  let message = $state('');
  let hideKey = $state(false);

  async function load() {
    try {
      [update, setup] = await Promise.all([(await fetch('/api/update')).json(), (await fetch('/api/setup')).json()]);
    } catch {
      /* the app is restarting */
    }
  }
  load();
  // Settings' "Check for updates" tells the banner straight away, instead of at the next 10-minute look
  $effect(() => {
    const onUpdate = (e) => (update = e.detail);
    window.addEventListener('draftassist:update', onUpdate);
    return () => window.removeEventListener('draftassist:update', onUpdate);
  });
  $effect(() => {
    const every = setup?.bootstrap?.state === 'running' ? 3000 : 10 * 60 * 1000;
    const t = setInterval(load, every);
    return () => clearInterval(t);
  });

  async function install() {
    installing = true;
    message = '';
    const res = await fetch('/api/update/install', { method: 'POST' });
    if (!res.ok) {
      message = (await res.json()).detail;
      installing = false;
    }
  }
</script>

{#if update?.available}
  <div class="banner update">
    {#if installing}
      Installing Draft Assistant {update.latest}: the app closes and opens again in a moment.
    {:else}
      Draft Assistant {update.latest} is available (you have {update.current}).
      <button onclick={install} disabled={inDraft} title={inDraft ? 'After the game' : update.notes}>Update</button>
      {#if inDraft}<span class="muted">after the game</span>{/if}
      {#if message}<span class="error">{message}</span>{/if}
    {/if}
  </div>
{/if}
{#if setup?.bootstrap?.state === 'running' && setup.bootstrap.pull}
  <div class="banner setup">Pulling the team's stats: {setup.bootstrap.pull.done.toLocaleString()} of
    {setup.bootstrap.pull.total ? setup.bootstrap.pull.total.toLocaleString() : '…'} blocks
    <span class="pbar"><span style="width:{setup.bootstrap.pull.total ? Math.round(100 * setup.bootstrap.pull.done / setup.bootstrap.pull.total) : 0}%"></span></span>
    · Live uses OP.GG's stats until it's done</div>
{:else if setup?.bootstrap?.state === 'running'}
  <div class="banner setup">Setting up: fetching champions and starting stats (about a minute)…</div>
{:else if setup?.bootstrap?.state === 'done' && Object.values(setup.bootstrap.jobs).some((s) => s !== 'done')}
  <div class="banner setup">First-run setup couldn't fetch everything (no internet?): it tries again at the next start.</div>
{/if}
{#if setup && !setup.riot_key_set && !hideKey}
  <div class="banner key">
    Add your Riot API key in <a href="#/settings">Settings</a> to scout opponents and read match history.
    <button class="link" onclick={() => (hideKey = true)}>Hide</button>
  </div>
{/if}

<style>
  .banner { padding: 6px 16px; font-size: 14px; text-align: center; display: flex; gap: 10px; justify-content: center;
            align-items: center; flex-wrap: wrap; }
  .update { background: var(--side-blue-bg); color: var(--text); }
  .setup { background: var(--gold-bg); color: var(--gold); }
  .pbar { display: inline-block; width: 160px; height: 6px; border-radius: 3px; background: var(--panel-2); overflow: hidden; }
  .pbar span { display: block; height: 100%; background: var(--gold); }
  .key { background: var(--panel-2); color: var(--muted); }
  .key a { color: var(--link); }
  button { background: var(--gold); color: var(--on-gold); font-weight: 600; border: 0; border-radius: 6px; padding: 3px 12px; font: inherit; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.link { background: none; color: var(--muted); padding: 0; text-decoration: underline; }
  .muted { color: var(--muted); }
  .error { color: var(--down-text); }
</style>
