<script>
  import Champ from './Champ.svelte';

  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const EXAMPLE = "TOP\nK'Sante: main\nRumble: comfortable\nGnar: learning\n\nJUNGLE\nSejuani: main";

  let data = $state(null);
  let players = $state([]);
  let text = $state('');
  let importErrors = $state([]);
  let message = $state('');
  const TIERS = ['main', 'comfortable', 'learning'];
  let adding = $state(Object.fromEntries(Object.keys(LABELS).map((r) => [r, { name: '', tier: 'learning' }])));

  async function editPool(method, role, champId, body) {
    const res = await fetch(`/api/pools/${role}/${champId}`, {
      method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
    });
    const r = await res.json();
    if (!res.ok) { message = `Not saved: ${r.detail}`; return false; }
    data = { ...data, pools: r.pools };
    message = r.applied ? 'Pool saved.' : 'Pool saved: suggestions use it after this draft.';
    return true;
  }

  async function addChamp(role) {
    const name = adding[role].name.trim().toLowerCase();
    const champ = data.champions.find((c) => c.name.toLowerCase() === name);
    if (!champ) { message = `No champion called "${adding[role].name}".`; return; }
    if (await editPool('PUT', role, champ.id, { tier: adding[role].tier })) adding[role].name = '';
  }

  const setTier = (role, champId, tier) => editPool('PUT', role, champId, { tier });
  const removeChamp = (role, e) => editPool('DELETE', role, e.champ.id);

  async function load() {
    data = await (await fetch('/api/pools')).json();
    players = data.players.map((p) => ({ ...p }));
  }

  async function post(url, body) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return res.json();
  }

  async function acceptTier(role, champ_id = null) {
    const r = await post('/api/pools/accept', { role, champ_id });
    message = `${r.changed} tier${r.changed === 1 ? '' : 's'} updated.`;
    await load();
  }

  async function toggleLock(role, e) {
    await post('/api/pools/lock', { role, champ_id: e.champ.id, locked: !e.tier_locked });
    await load();
  }

  async function learn() {
    message = '';
    await post('/api/learning/run', {});
    await load();
    const poll = setInterval(async () => {
      const state = await (await fetch('/api/learning')).json();
      if (!state.running) {
        clearInterval(poll);
        await load();
        const s = state.summary ?? {};
        message = s.error ?? `Updated from games: ${s.proposals ?? 0} tier change${s.proposals === 1 ? '' : 's'} to review` +
          (s.history_error ? ` (match history unavailable: ${s.history_error})` : '');
      }
    }, 2000);
  }

  const record = (e) => (e.games ? `${e.wins}W ${e.games - e.wins}L` : 'no games');
  const proposals = (entries) => entries.filter((e) => e.proposal).length;

  // The bench (docs/player-roster.md): one active player per role; a swapped-out player keeps their pool
  let sub = $state({ role: 'top', riot_id: '' });

  async function swap(role, riot_id) {
    message = '';
    const res = await fetch('/api/players/swap', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role, riot_id }),
    });
    const body = await res.json();
    if (!res.ok) { message = `Not swapped: ${body.detail}`; return; }
    await load();
    sub.riot_id = '';
    message = `${riot_id.split('#')[0]} is our ${LABELS[role]} now` +
      (body.applied ? '.' : ': suggestions use their pool after this draft.');
  }

  async function forget(p) {
    if (!confirm(`Forget ${p.riot_id || 'this player'} and their pool for good?`)) return;
    const res = await fetch(`/api/players/bench?riot_id=${encodeURIComponent(p.riot_id)}`, { method: 'DELETE' });
    if (res.ok) { await load(); message = `${p.riot_id} forgotten.`; }
  }

  const poolSize = (p) => Object.values(p.pools).reduce((n, champs) => n + champs.length, 0);

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
    <p class="muted small">
      A linked player whose Riot ID you change goes to the bench with their pool; one never linked is just renamed.
    </p>

    <h3 class="subhead">Swap in a sub</h3>
    <form class="swap" onsubmit={(ev) => { ev.preventDefault(); swap(sub.role, sub.riot_id.trim()); }}>
      <select bind:value={sub.role}>{#each Object.entries(LABELS) as [r, l]}<option value={r}>{l}</option>{/each}</select>
      <input list="bench-ids" bind:value={sub.riot_id} placeholder="Sub#EUW (new, or from the bench)" />
      <datalist id="bench-ids">{#each data?.bench ?? [] as b}{#if b.riot_id}<option value={b.riot_id}></option>{/if}{/each}</datalist>
      <button type="submit" disabled={!sub.riot_id.includes('#')}>Swap in</button>
    </form>

    {#if data?.bench?.length}
      <h3 class="subhead">Bench</h3>
      <div class="bench">
        {#each data.bench as b}
          <div class="benched">
            <div class="who">
              <strong>{b.riot_id || '(no Riot ID)'}</strong>
              <span class="muted">{LABELS[b.role]}{b.rank ? ` · ${b.rank}` : ''} · {poolSize(b)} champion{poolSize(b) === 1 ? '' : 's'}</span>
            </div>
            <div class="chips">
              {#each Object.entries(b.pools) as [r, champs]}
                {#each champs.slice(0, 6) as c}<span title="{c.champ.name} ({LABELS[r]}, {c.tier})"><Champ champ={c.champ} size={24} /></span>{/each}
              {/each}
            </div>
            <div class="actions">
              {#if b.riot_id}<button class="small-btn" onclick={() => swap(b.role, b.riot_id)}>Swap in as {LABELS[b.role]}</button>{/if}
              <button class="small-btn ghost" onclick={() => forget(b)} disabled={!b.riot_id}>Forget</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </section>

  <section>
    <div class="poolhead">
      <h2>Champion pools</h2>
      <button class="secondary" onclick={learn} disabled={data?.learning?.running}>
        {data?.learning?.running ? 'Updating from games…' : 'Update from games'}
      </button>
    </div>
    <p class="muted">
      Tiers are re-scored from each player's last 60 days of ranked games (Solo/Duo, Flex, Ranked 5s) after every
      logged game. A suggested change waits for you to accept it; a locked tier is never changed.
    </p>
    {#if data?.using_placeholder_pools}
      <p class="warn">No pools imported yet: suggestions use each role's most-picked champions.</p>
    {/if}
    <datalist id="champ-names">{#each data?.champions ?? [] as c}<option value={c.name}></option>{/each}</datalist>
    <div class="roles">
      {#each Object.entries(data?.pools ?? {}) as [role, entries]}
        <div class="role">
          <h3>{LABELS[role]}
            {#if proposals(entries) > 1}<button class="link" onclick={() => acceptTier(role)}>accept all {proposals(entries)}</button>{/if}
          </h3>
          {#if data?.health?.[role]}
            {@const h = data.health[role]}
            <div class="health">
              <div class:missing={!h.blind.ok}>Blind pick: {h.blind.ok ? h.blind.name : `none safe${h.blind.name ? ` (best ${h.blind.name})` : ''}`}</div>
              {#if h.setup.applies}<div class:missing={!h.setup.ok}>Setup: {h.setup.ok ? h.setup.name : 'none'}</div>{/if}
              {#if h.counter.main}<div class:missing={!h.counter.ok}
                title={h.counter.worst.map((w) => `${w.name} ${w.main_pp}pp${w.answered ? `: ${w.answer} ${w.answer_pp}pp` : ': no answer'}`).join(' · ')}>
                Counters to {h.counter.main}'s worst: {h.counter.worst.filter((w) => w.answered).length} of {h.counter.worst.length}</div>{/if}
            </div>
          {/if}
          {#each entries as e}
            <div class="entry">
              <Champ champ={e.champ} size={32} />
              <span class="name" title={e.champ.name}>{e.champ.name}</span>
              <select class="tier {e.manual_tier}" value={e.manual_tier} title="Tier (set by hand)"
                      onchange={(ev) => setTier(role, e.champ.id, ev.currentTarget.value)}>
                {#each TIERS as t}<option value={t}>{t}</option>{/each}
              </select>
              <button class="lock" class:on={e.tier_locked} title={e.tier_locked ? 'Tier locked: click to unlock' : 'Lock this tier'}
                      onclick={() => toggleLock(role, e)}>{e.tier_locked ? '🔒' : '🔓'}</button>
              <button class="remove" title="Remove {e.champ.name} from the {LABELS[role]} pool"
                      onclick={() => removeChamp(role, e)}>×</button>
              <span class="rec" title="Last 60 days in this role · comfort {e.comfort}">{record(e)} · comfort {e.comfort}</span>
              {#if e.badge?.text}<span class="badge" title="Comp type and fight style (Champions page)">{e.badge.text}</span>{/if}
              {#if e.execution_hint}
                <span class="lanehint {e.execution_hint.kind}" title={e.execution_hint.text}>
                  {e.execution_hint.kind === 'behind_in_lane' ? 'Wins, behind in lane' : 'Loses, ahead in lane'}</span>
              {/if}
              {#if e.proposal}
                <button class="proposal" title="Scored from recent games: click to accept" onclick={() => acceptTier(role, e.champ.id)}>
                  → {e.proposal}
                </button>
              {/if}
            </div>
          {:else}
            <p class="muted">empty</p>
          {/each}
          <form class="add" onsubmit={(ev) => { ev.preventDefault(); addChamp(role); }}>
            <input list="champ-names" placeholder="Add champion" bind:value={adding[role].name} />
            <select bind:value={adding[role].tier}>{#each TIERS as t}<option value={t}>{t}</option>{/each}</select>
            <button type="submit" disabled={!adding[role].name.trim()}>Add</button>
          </form>
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
  .small { font-size: 12px; margin: 6px 0 0; }
  .subhead { margin: 20px 0 8px; }
  .swap { display: flex; gap: 8px; align-items: center; max-width: 560px; }
  .swap select { width: auto; }
  .swap button { margin: 0; white-space: nowrap; }
  .bench { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
  .benched { background: var(--panel); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
  .who { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .who strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .who .muted { font-size: 12px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; min-height: 24px; }
  .actions { display: flex; gap: 6px; }
  .small-btn { margin: 0; padding: 4px 10px; font-size: 13px; }
  .small-btn.ghost { background: var(--panel-2); border: 1px solid var(--line); }
  .health { font-size: 12px; color: var(--muted); margin: 0 0 8px; display: flex; flex-direction: column; gap: 2px; }
  .health .missing { color: #e0a640; }
  .page { max-width: 1440px; margin: 0 auto; padding: 20px 28px 60px; }
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
  .roles { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }
  .role { background: var(--panel); border-radius: 8px; padding: 12px; }
  .entry { display: grid; grid-template-columns: 32px 1fr auto auto auto; grid-auto-rows: auto; align-items: center; gap: 4px 8px;
           margin: 8px 0; }
  .rec { grid-column: 2 / -1; margin-top: -4px; font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { grid-column: 2 / -1; justify-self: start; margin-top: -2px; padding: 0 5px; font-size: 11px; color: #c9d1d9;
           background: #2d3540; border-radius: 4px; }
  .lock { background: none; border: 0; padding: 0; margin: 0; cursor: pointer; font-size: 14px; opacity: 0.45; }
  .lock.on { opacity: 1; }
  .proposal { grid-column: 2 / -1; justify-self: start; margin: 0; padding: 2px 8px; font-size: 12px; background: #3d3214;
              color: var(--gold); border: 1px solid #6b5a1f; border-radius: 4px; }
  .poolhead { display: flex; align-items: center; justify-content: space-between; }
  .secondary { margin: 0; background: var(--panel-2); border: 1px solid var(--line); }
  .link { margin: 0 0 0 8px; padding: 0; background: none; color: var(--gold); font-size: 12px; text-transform: none; }
  .name { font-size: 15px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tier { font-size: 12px; padding: 2px 4px; border-radius: 4px; background: var(--panel-2); border: 0; width: auto; }
  .remove { background: none; border: 0; padding: 0 2px; margin: 0; color: var(--muted); font-size: 16px; cursor: pointer; }
  .remove:hover { color: var(--red); }
  .add { display: flex; gap: 6px; margin-top: 10px; }
  .add input { flex: 1; min-width: 0; padding: 4px 6px; font-size: 13px; }
  .add select { font-size: 12px; padding: 4px; }
  .add button { margin: 0; padding: 4px 10px; font-size: 13px; }
  .tier.main { color: var(--good); }
  .tier.comfortable { color: var(--blue); }
  .tier.learning { color: var(--warn); }
  /* docs/draft-vs-execution.md, Stage 6: a hint only. Teal / amber as validated; the text is always written */
  .lanehint { grid-column: 2 / -1; justify-self: start; margin-top: -2px; font-size: 11px; border-radius: 4px; padding: 0 6px;
              background: var(--panel-2); }
  .lanehint.behind_in_lane { color: #c98500; }
  .lanehint.ahead_in_lane { color: #1baf7a; }
  .muted { color: var(--muted); }
  .warn { color: var(--gold); }
  .ok { color: var(--good); }
  .errors { color: var(--red); }
</style>
