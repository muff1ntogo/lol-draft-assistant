<script>
  import Champ from './Champ.svelte';

  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const EXAMPLE = "TOP\nK'Sante: main\nRumble: comfortable\nGnar: learning\n\nJUNGLE\nSejuani: main";

  let data = $state(null);
  let players = $state([]);
  let text = $state('');
  let importErrors = $state([]);
  let message = $state('');

  async function load() {
    data = await (await fetch('/api/pools')).json();
    players = data.players.map((p) => ({ ...p }));
  }

  async function savePlayers() {
    message = '';
    const res = await fetch('/api/players', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ players }),
    });
    const body = await res.json();
    message = res.ok ? 'Players saved.' : `Not saved: ${body.detail}`;
    if (res.ok) await load();
  }

  async function importPools() {
    importErrors = [];
    message = '';
    const res = await fetch('/api/pools/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const body = await res.json();
    if (res.ok) {
      message = 'Imported: ' + Object.entries(body.imported).map(([r, n]) => `${LABELS[r]} ${n}`).join(', ');
      text = '';
      await load();
    } else {
      importErrors = body.detail?.errors ?? [String(body.detail)];
    }
  }

  load();
</script>

<main class="page">
  <h1>Pools</h1>
  {#if message}<p class="ok">{message}</p>{/if}

  <section>
    <h2>Players</h2>
    <table class="players">
      <thead><tr><th>Role</th><th>Riot ID</th><th>Rank</th><th>Stats bracket</th><th></th></tr></thead>
      <tbody>
        {#each players as p}
          <tr>
            <td>{LABELS[p.role]}</td>
            <td><input bind:value={p.riot_id} placeholder="Name#EUW" /></td>
            <td><input bind:value={p.rank} placeholder="Gold II" /></td>
            <td>
              <select bind:value={p.bracket}>
                {#each ['iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald', 'diamond'] as b}<option value={b}>{b}</option>{/each}
              </select>
            </td>
            <td class="muted">{p.resolved ? '✓ linked' : p.riot_id ? 'links at next champ select' : ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <button onclick={savePlayers}>Save players</button>
  </section>

  <section>
    <h2>Champion pools</h2>
    {#if data?.using_placeholder_pools}
      <p class="warn">No pools imported yet: suggestions use each role's most-picked champions.</p>
    {/if}
    <div class="roles">
      {#each Object.entries(data?.pools ?? {}) as [role, entries]}
        <div class="role">
          <h3>{LABELS[role]}</h3>
          {#each entries as e}
            <div class="entry">
              <Champ champ={e.champ} size={32} />
              <span class="name">{e.champ.name}</span>
              <span class="tier {e.manual_tier}">{e.manual_tier}</span>
              <span class="scored" title="Scored tier (v2, from results)">{e.scored_tier ?? '—'}</span>
              {#if e.tier_locked}<span title="Tier locked">🔒</span>{/if}
            </div>
          {:else}
            <p class="muted">empty</p>
          {/each}
        </div>
      {/each}
    </div>
  </section>

  <section>
    <h2>Import pools</h2>
    <p class="muted">One block per role. Importing a role replaces its pool; roles you leave out stay as they are.</p>
    <textarea bind:value={text} rows="10" placeholder={EXAMPLE}></textarea>
    <button onclick={importPools} disabled={!text.trim()}>Import</button>
    {#if importErrors.length}
      <ul class="errors">{#each importErrors as e}<li>{e}</li>{/each}</ul>
    {/if}
  </section>
</main>

<style>
  .page { max-width: 1100px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 32px; margin: 8px 0 16px; }
  h2 { font-size: 20px; margin: 28px 0 10px; }
  h3 { font-size: 16px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; }
  table { border-collapse: collapse; width: 100%; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 13px; padding: 4px 8px; }
  td { padding: 4px 8px; }
  input, select, textarea { background: var(--panel); color: var(--text); border: 1px solid var(--line);
                            border-radius: 6px; padding: 6px 8px; font: inherit; }
  input { width: 100%; }
  textarea { width: 100%; font-family: Consolas, monospace; }
  button { margin-top: 10px; background: var(--blue); color: white; border: 0; border-radius: 6px;
           padding: 8px 16px; font: inherit; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  .roles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
  .role { background: var(--panel); border-radius: 8px; padding: 12px; }
  .entry { display: grid; grid-template-columns: 32px 1fr auto auto auto; align-items: center; gap: 8px; margin: 6px 0; }
  .name { font-size: 15px; }
  .tier { font-size: 12px; padding: 2px 6px; border-radius: 4px; background: var(--panel-2); }
  .tier.main { color: var(--good); }
  .tier.comfortable { color: var(--blue); }
  .tier.learning { color: var(--warn); }
  .scored { font-size: 12px; color: var(--muted); }
  .muted { color: var(--muted); }
  .warn { color: var(--gold); }
  .ok { color: var(--good); }
  .errors { color: var(--red); }
</style>
