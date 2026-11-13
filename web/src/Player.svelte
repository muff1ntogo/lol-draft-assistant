<script>
  // A player's profile (docs/roster-profiles.md, Stage E): #/player/<riot id>. The header says who they are and holds
  // the roster actions; then summary figures, the champion pool (the main section, editable), roles, lane execution
  // and mastery. The window picker sets which games the figures read.
  import Champ from './Champ.svelte';
  import ShareLine from './ShareLine.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';
  import Table from './ui/Table.svelte';
  import Stat from './ui/Stat.svelte';
  import Tag from './ui/Tag.svelte';
  import Sheet from './ui/Sheet.svelte';
  import { toast } from './ui/toast.svelte.js';
  import { signed } from './format.js';
  import { BRACKETS, QUEUE_LABELS, ROLES, ROLE_LABELS, TIER_OPTIONS, TREND, api, day, executionTone, fixed, healthLine, nameOf, pct,
           playerPath, pp, rankText, tagOf } from './roster.js';

  let { param = '' } = $props(); // the Riot ID from the address
  const riotId = $derived(decodeURIComponent(param));
  let data = $state(null);
  let failed = $state('');
  let window_ = $state('60d');
  try { window_ = localStorage.getItem('player.window') === '20' ? '20' : '60d'; } catch {}
  let note = $state('');
  let noteFailed = $state(false);
  function say(text, bad = false) { note = text; noteFailed = bad; }

  let request = 0;
  async function load() {
    const mine = ++request;
    try {
      const body = await api(`${playerPath(riotId)}/profile?window=${window_}`);
      if (mine !== request) return;
      data = body;
      failed = '';
    } catch (e) {
      if (mine === request) failed = e.message === 'no player with that Riot ID' ? `Nobody called ${riotId} is on the roster.` : `Couldn't read the profile: ${e.message}`;
    }
  }
  $effect(() => { riotId; window_; load(); }); // at once on open, and again when the window or the player changes
  function setWindow(w) {
    window_ = w;
    try { localStorage.setItem('player.window', w); } catch {}
  }
  const applied = (r) => (r?.applied === false ? ' Live uses it after this draft.' : '');

  // ---- the pool ----
  async function setTier(role, row, tier) {
    try {
      const r = await api(`${playerPath(riotId)}/pool/${role}/${row.champ_id}`, { method: 'PUT', body: { tier } });
      say(`${row.champ.name} is ${tier} now.${applied(r)}`);
      await load();
    } catch (e) { say(`Not saved: ${e.message}`, true); }
  }
  async function remove(role, row) {
    try {
      const r = await api(`${playerPath(riotId)}/pool/${role}/${row.champ_id}`, { method: 'DELETE' });
      await load();
      say(`${row.champ.name} removed from the ${ROLE_LABELS[role]} pool.${applied(r)}`);
      toast(`${row.champ.name} removed`, { seconds: 10, action: { label: 'Undo', onclick: () => setTier(role, row, row.tier) } });
    } catch (e) { say(`Not removed: ${e.message}`, true); }
  }
  async function accept(role, row) {
    try {
      await api('/api/pools/accept', { method: 'POST', body: { role, champ_id: row.champ_id } });
      say(`${row.champ.name} is ${row.suggested_tier} now.`);
      await load();
    } catch (e) { say(`Not accepted: ${e.message}`, true); }
  }
  async function acceptAll(role) {
    try {
      const r = await api('/api/pools/accept', { method: 'POST', body: { role, champ_id: null } });
      say(`${r.changed} tier${r.changed === 1 ? '' : 's'} updated.`);
      await load();
    } catch (e) { say(`Not accepted: ${e.message}`, true); }
  }
  async function lock(role, row) {
    try {
      await api('/api/pools/lock', { method: 'POST', body: { role, champ_id: row.champ_id, locked: !row.locked } });
      await load();
    } catch (e) { say(`Not saved: ${e.message}`, true); }
  }
  let adding = $state(Object.fromEntries(ROLES.map((r) => [r, { name: '', tier: 'learning', error: '' }])));
  const addState = (role) => adding[role];
  async function addChampion(role) {
    const a = addState(role);
    a.error = '';
    const champ = data.champions.find((c) => c.name.toLowerCase() === a.name.trim().toLowerCase());
    if (!champ) { a.error = `No champion called "${a.name.trim()}"`; return; }
    try {
      const r = await api(`${playerPath(riotId)}/pool/${role}/${champ.id}`, { method: 'PUT', body: { tier: a.tier } });
      a.name = '';
      say(`${champ.name} added to the ${ROLE_LABELS[role]} pool.${applied(r)}`);
      await load();
    } catch (e) { a.error = e.message; }
  }
  // "Import from text": read, then add each line to this player's pool
  let importOpen = $state(false);
  let importRole = $state('top');
  let importText = $state('');
  let importErrors = $state([]);
  let importing = $state(false);
  function openImport(role) { importRole = role; importText = ''; importErrors = []; importOpen = true; }
  async function runImport() {
    importing = true;
    importErrors = [];
    try {
      const out = await api('/api/pools/parse', { method: 'POST', body: { text: importText, role: importRole } });
      if (out.errors.length) { importErrors = out.errors; return; }
      for (const e of out.entries) await api(`${playerPath(riotId)}/pool/${importRole}/${e.champ_id}`, { method: 'PUT', body: { tier: e.tier } });
      importOpen = false;
      say(`${out.entries.length} champion${out.entries.length === 1 ? '' : 's'} read into the ${ROLE_LABELS[importRole]} pool.`);
      await load();
    } catch (e) {
      importErrors = [e.message];
    } finally {
      importing = false;
    }
  }

  // ---- header actions ----
  async function setBracket(bracket) {
    try {
      await api(`${playerPath(riotId)}/bracket`, { method: 'PUT', body: { bracket } });
      say(`Live reads ${nameOf(riotId)}'s win rates in ${bracket[0].toUpperCase() + bracket.slice(1)} now.`);
      await load();
    } catch (e) { say(`Not saved: ${e.message}`, true); }
  }
  async function bench() {
    try {
      await api(`${playerPath(riotId)}/bench`, { method: 'POST' });
      say(`${nameOf(riotId)} is on the bench with their pool.`);
      toast(`${nameOf(riotId)} benched`, { seconds: 10, action: { label: 'Undo', onclick: () => swapIn(data.role) } });
      await load();
    } catch (e) { say(`Not benched: ${e.message}`, true); }
  }
  let swapOpen = $state(false);
  let swapRole = $state('top');
  async function swapIn(role) {
    try {
      const out = await api(`${playerPath(riotId)}/activate`, { method: 'POST', body: { role } });
      swapOpen = false;
      say(out.undo?.riot_id ? `Swapped in for ${nameOf(out.undo.riot_id)}, who is on the bench.` : `${nameOf(riotId)} is our ${ROLE_LABELS[role]} now.`);
      await load();
    } catch (e) { say(`Not swapped: ${e.message}`, true); }
  }
  let deleteOpen = $state(false);
  let deleting = $state(false);
  async function confirmDelete() {
    deleting = true;
    try {
      await api(`${playerPath(riotId)}?confirm=${encodeURIComponent(riotId)}`, { method: 'DELETE' });
      location.hash = '#/pools';
    } catch (e) {
      say(`Not deleted: ${e.message}`, true);
      deleteOpen = false;
    } finally {
      deleting = false;
    }
  }

  const health = $derived(healthLine(data?.health));
  const ex = $derived(data?.execution);
  const tiles = $derived(data?.tiles);
  const games = (n) => `${n} game${n === 1 ? '' : 's'}`;
  const about = $derived.by(() => {
    if (!data) return '';
    const ranks = ['solo', 'flex'].filter((q) => data.rank[q]).map((q) => `${QUEUE_LABELS[q]} ${rankText(data.rank[q])}, ${games(data.rank[q].wins + data.rank[q].losses)}`);
    const since = data.since_days == null ? '' : data.since_days === 0 ? ' since today' : ` for ${data.since_days} day${data.since_days === 1 ? '' : 's'}`;
    return `${data.active ? `Our ${data.role_label}${since}` : `On the bench (${data.role_label})`} · ${ranks.length ? ranks.join(' · ') : data.has_key ? 'rank not read yet' : 'no Riot key: rank and mastery not read'}`;
  });
</script>

<main class="console">
  <a class="back" href="#/pools">← Roster</a>
  {#if failed}
    <PageHeader title={riotId} />
    <p class="note failed" role="alert">⚠ {failed}</p>
  {:else if data}
    <PageHeader title={riotId} description={about}>
      {#snippet actions()}
        {#if data.active}
          <Field label="Stats bracket" inline width="auto" value={data.bracket} onchange={(e) => setBracket(e.currentTarget.value)}
                 options={BRACKETS.map((b) => [b, b[0].toUpperCase() + b.slice(1)])} title="The tier Live reads this player's win rates and matchups in" />
          <Button onclick={bench} title="Off the lineup; their pool waits on the bench">Bench</Button>
        {:else}
          <Button onclick={() => { swapRole = data.role; swapOpen = true; }}>Swap in</Button>
        {/if}
        <Button danger variant="ghost" onclick={() => (deleteOpen = true)}>Delete…</Button>
      {/snippet}
    </PageHeader>
    {#if note}<p class="note" class:failed={noteFailed} role={noteFailed ? 'alert' : 'status'}>{noteFailed ? '⚠ ' : ''}{note}</p>{/if}

    <Toolbar label="Profile options">
      <ToolGroup label="Figures from"><Segmented label="Games the figures read" value={window_} onchange={setWindow} options={[['20', 'Last 20 games'], ['60d', '60 days']]} /></ToolGroup>
      {#if health}<ToolGroup label="Pool health"><span class="health" class:warn={health.warn}>{health.warn ? '⚠ ' : ''}{health.text}</span></ToolGroup>{/if}
    </Toolbar>

    <section class="tiles" aria-label="Summary">
      <Stat value={tiles.games} label="Games as {data.role_label}" />
      <Stat value={tiles.win_rate == null ? null : Math.round(tiles.win_rate * 100)} unit="%" label="Win rate" note={tiles.games ? `${tiles.wins}W ${tiles.games - tiles.wins}L` : 'no games'} />
      <Stat value={tiles.kda == null ? null : fixed(tiles.kda)} label="KDA" note={tiles.kda == null ? 'no games with figures' : `${fixed(tiles.kills)} / ${fixed(tiles.deaths)} / ${fixed(tiles.assists)}`} />
      <Stat value={tiles.cs_min == null ? null : fixed(tiles.cs_min)} label="CS / min" />
      <Stat value={tiles.vision_min == null ? null : fixed(tiles.vision_min, 2)} label="Vision / min" />
      <Stat value={tiles.execution.enough ? signed(tiles.execution.mean_pp, 1) : null} unit="pp" label="Lane execution" tone={executionTone(tiles.execution)}
            note={tiles.execution.enough ? tiles.execution.label : `needs ${tiles.execution.needed} team games`} />
      <Stat value={fixed(tiles.pool_size, 2).replace(/\.?0+$/, '')} label="Pool size, weighted" note="{tiles.pool_champs} champions"
            title="Main counts 1, comfortable 0.5, learning 0.25" />
    </section>
    {#if tiles.games && tiles.stat_games < tiles.games}<p class="cap">KDA, CS and vision are from the {games(tiles.stat_games)} with figures; logged games without a match have none.</p>{/if}

    {#each data.pools as group, gi (group.role)}
      {@const a = addState(group.role)}
      {@const pending = group.pool.filter((r) => r.suggested_tier).length}
      <section>
        <div class="sechead">
          <h2>{group.role_label} pool{#if gi > 0} <span class="sub">not their current role</span>{/if}</h2>
          {#if pending && group.editable_tiers}<Button onclick={() => acceptAll(group.role)}>Accept {pending} suggested tier{pending === 1 ? '' : 's'}</Button>{/if}
          <Button variant="ghost" onclick={() => openImport(group.role)}>Import from text</Button>
        </div>
        <div class="pooltable">
        <Table sticky={false}>
          <thead><tr>
            <th scope="col">Champion</th><th scope="col">Tier</th>
            <th scope="col" class="num">Games</th><th scope="col" class="num">Win rate</th><th scope="col" class="num wideonly">KDA</th>
            <th scope="col" class="num wideonly">CS / min</th><th scope="col" class="num wideonly">Last played</th><th scope="col" class="num wideonly">Mastery</th>
            <th scope="col" class="num wideonly" title="How safe it is to pick blind, in pp: the worst open counter's lane edge and how flexible the pick is">Blind</th>
            <th scope="col"><span class="sr">Actions</span></th>
          </tr></thead>
          <tbody>
            {#each group.pool as row (row.champ_id)}
              <tr>
                <td><span class="c"><Champ champ={row.champ} size={24} /><strong>{row.champ.name}</strong>
                  {#if row.badge?.text}<Tag>{row.badge.text}</Tag>{/if}
                  {#if row.execution_hint}<Tag title={row.execution_hint.text}>{row.execution_hint.kind === 'behind_in_lane' ? 'wins, behind in lane' : 'loses, ahead in lane'}</Tag>{/if}</span></td>
                <td><span class="tiercell">
                  <Field label="Tier for {row.champ.name}" hideLabel width="132px" value={row.tier} options={TIER_OPTIONS} onchange={(e) => setTier(group.role, row, e.currentTarget.value)} />
                  {#if group.editable_tiers}
                    <Tag onclick={() => lock(group.role, row)} pressed={row.locked} title={row.locked ? 'Locked: games never suggest another tier. Click to unlock' : 'Lock this tier, so games never suggest another'}>{row.locked ? 'locked' : 'lock'}</Tag>
                    {#if row.suggested_tier}<Button onclick={() => accept(group.role, row)} title="What their recent games suggest">Accept {row.suggested_tier}</Button>{/if}
                  {/if}</span></td>
                <td class="num mono">{row.games}</td>
                <td class="num mono">{pct(row.win_rate)}</td>
                <td class="num mono wideonly">{fixed(row.kda)}</td>
                <td class="num mono wideonly">{fixed(row.cs_min)}</td>
                <td class="num mono wideonly">{day(row.last_played_ms)}</td>
                <td class="num mono wideonly">{row.mastery ? row.mastery.toLocaleString() : '—'}</td>
                <td class="num mono wideonly {row.blind_safety > 0.5 ? 'up' : row.blind_safety < -0.5 ? 'down' : ''}">{signed(row.blind_safety, 1)}</td>
                <td class="acts"><Button variant="ghost" onclick={() => remove(group.role, row)} aria-label="Remove {row.champ.name} from the pool">Remove</Button></td>
              </tr>
            {:else}
              <tr><td colspan="10" class="quiet">No champions in this pool yet. Add one below, or import a list.</td></tr>
            {/each}
          </tbody>
          {#if group.not_in_pool.length}
            <tbody class="outside">
              <tr class="divider"><th colspan="10" scope="colgroup">Played as {group.role_label}, not in the pool</th></tr>
              {#each group.not_in_pool as row (row.champ_id)}
                <tr>
                  <td><span class="c"><Champ champ={row.champ} size={24} faded /><span>{row.champ.name}</span></span></td>
                  <td></td>
                  <td class="num mono">{row.games}</td><td class="num mono">{pct(row.win_rate)}</td><td class="num mono wideonly">{fixed(row.kda)}</td>
                  <td class="num mono wideonly">{fixed(row.cs_min)}</td><td class="num mono wideonly">{day(row.last_played_ms)}</td>
                  <td class="num mono wideonly">{row.mastery ? row.mastery.toLocaleString() : '—'}</td><td class="wideonly"></td>
                  <td class="acts"><Button onclick={() => setTier(group.role, { ...row, tier: null }, row.games >= 5 ? 'comfortable' : 'learning')}
                                           title="Add to the pool as {row.games >= 5 ? 'comfortable' : 'learning'}">Add</Button></td>
                </tr>
              {/each}
            </tbody>
          {/if}
        </Table>
        </div>
        <form class="addrow" onsubmit={(e) => { e.preventDefault(); addChampion(group.role); }}>
          <Field label="Add a champion" hideLabel bind:value={a.name} list="player-champs" placeholder="Add a champion" autocomplete="off" width="220px" error={a.error} />
          <Field label="Tier" hideLabel bind:value={a.tier} options={TIER_OPTIONS} width="140px" />
          <Button type="submit" disabled={!a.name.trim()}>Add</Button>
        </form>
      </section>
    {/each}
    <datalist id="player-champs">{#each data.champions as c}<option value={c.name}></option>{/each}</datalist>

    <div class="two">
      <section>
        <h2>Roles <span class="sub">{window_ === '20' ? 'last 20 games' : 'last 60 days'}</span></h2>
        {#if data.roles.length}
          <Table dense sticky={false}>
            <thead><tr><th scope="col">Role</th><th scope="col" colspan="2">Share of their games</th><th scope="col" class="num">Games</th><th scope="col" class="num">Win rate</th></tr></thead>
            <tbody>
              {#each data.roles as r}
                <tr><td>{r.role_label}</td>
                  <td class="barcell"><span class="track"><span style:width="{r.share * 100}%"></span></span></td>
                  <td class="num mono share">{pct(r.share)}</td><td class="num mono">{r.games}</td><td class="num mono">{pct(r.win_rate)}</td></tr>
              {/each}
            </tbody>
          </Table>
        {:else}<p class="quiet">No ranked games in this window{data.has_key ? '' : ': add a Riot key in Settings to read them'}.</p>{/if}
      </section>

      <section>
        <h2>Lane execution <span class="sub">their part of our early games, as {data.role_label}</span></h2>
        {#if ex.enough}
          <div class="exline">
            <Stat size="sm" value={signed(ex.mean_pp, 1)} unit="pp" label="Mean a game" tone={executionTone(ex)} note="± {fixed(ex.se_pp)} · {ex.label}" />
            <Stat size="sm" value={ex.games} label="Team games" />
            {#if ex.trend}<Stat size="sm" value={TREND[ex.trend][0]} label="Trend" note={TREND[ex.trend][1]} />{/if}
          </div>
          <ShareLine values={ex.last} />
          <p class="cap">{Object.entries(ex.labels).map(([l, n]) => `${l} ${n}`).join(' · ')}. Coloured only when the mean's band is clear of 0; single games are noisy.</p>
        {:else}
          <p class="quiet">Needs {ex.needed} team games with a timeline: {ex.games} so far. Lane execution is read from games we played together, not from solo queue.</p>
        {/if}
      </section>
    </div>

    <section>
      <h2>Mastery <span class="sub">their ten highest</span></h2>
      {#if data.mastery.length}
        <div class="narrow">
          <Table dense sticky={false}>
            <thead><tr><th scope="col">Champion</th><th scope="col" class="num">Points</th><th scope="col" class="num">Last played</th><th scope="col">Pool</th></tr></thead>
            <tbody>
              {#each data.mastery as m}
                <tr><td><span class="c"><Champ champ={m.champ} size={20} />{m.champ?.name}</span></td>
                  <td class="num mono">{m.points.toLocaleString()}</td><td class="num mono">{day(m.last_played_ms)}</td>
                  <td>{#if m.in_pool}<Tag bare>in pool</Tag>{:else}<span class="quiet">not in a pool</span>{/if}</td></tr>
              {/each}
            </tbody>
          </Table>
        </div>
      {:else}<p class="quiet">{data.has_key ? 'Mastery not read yet: it comes with the next update from games.' : 'No Riot key: mastery and rank are not read. Add a key in Settings.'}</p>{/if}
    </section>

    <Sheet bind:open={importOpen} title="Import {ROLE_LABELS[importRole]} pool from text" description="One champion a line, with its tier. Champions already in the pool get the tier you give here.">
      <Field label="Pool" hideLabel type="textarea" mono bind:value={importText} placeholder={"K'Sante: main\nRumble: comfortable\nGnar: learning"} rows="10" />
      {#each importErrors as e}<p class="note failed" role="alert">⚠ {e}</p>{/each}
      {#snippet footer()}
        <Button variant="ghost" onclick={() => (importOpen = false)}>Cancel</Button>
        <Button variant="primary" size="md" disabled={!importText.trim()} busy={importing} onclick={runImport}>Read into the pool</Button>
      {/snippet}
    </Sheet>

    <Sheet dialog bind:open={swapOpen} title="Swap {nameOf(riotId)} in" description="Whoever plays the role now goes to the bench with their pool.">
      <Segmented label="Role to play" bind:value={swapRole} options={ROLES.map((r) => [r, ROLE_LABELS[r]])} />
      <p class="quiet pad">{data.pools.some((g) => g.role === swapRole && g.pool.length) ? `Their ${ROLE_LABELS[swapRole]} pool comes with them.` : `They have no ${ROLE_LABELS[swapRole]} pool yet: they start with an empty one.`}</p>
      {#snippet footer()}
        <Button variant="ghost" onclick={() => (swapOpen = false)}>Cancel</Button>
        <Button variant="primary" size="md" onclick={() => swapIn(swapRole)}>Swap in as {ROLE_LABELS[swapRole]}</Button>
      {/snippet}
    </Sheet>

    <Sheet dialog bind:open={deleteOpen} title="Delete {riotId}?">
      <p class="body">Their pools and the games fetched for them are removed for good. The team's history stays: drafts,
        and the games they played with us, keep their place in History and Team trends.</p>
      <p class="body"><strong>This can't be undone.</strong> To keep their pool for later, bench them instead.</p>
      {#snippet footer()}
        <Button variant="ghost" onclick={() => (deleteOpen = false)}>Cancel</Button>
        <Button danger size="md" busy={deleting} onclick={confirmDelete}>Delete {nameOf(riotId)}</Button>
      {/snippet}
    </Sheet>
  {:else}
    <PageHeader title={nameOf(riotId) + tagOf(riotId)} />
    <p class="note">Loading…</p>
  {/if}
</main>

<style>
  .back { display: inline-block; margin-top: 16px; margin-bottom: -16px; font-size: 13px; color: var(--text-3); text-decoration: none; }
  .back:hover { color: var(--text-1); }
  .note { margin: -8px 0 12px; font-size: 13px; color: var(--text-2); }
  .note.failed { color: var(--error); }
  .cap { margin: 8px 0 0; font-size: 12px; color: var(--text-3); }
  .quiet { color: var(--text-3); font-size: 13px; margin: 0; }
  .quiet.pad { margin-top: 12px; }
  .health { font-size: 13px; color: var(--text-2); }
  .health.warn { color: var(--warn); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }

  .tiles { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 20px 48px; padding: 8px 0 4px; }
  section { margin-top: 28px; padding-top: 20px; border-top: 1px solid var(--hairline); min-width: 0; }
  section.tiles { margin-top: 0; border-top: 0; padding-top: 8px; }
  .sechead { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  h2 { margin: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .sechead h2 { margin: 0 auto 0 0; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; }
  .c { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  .c strong { font-weight: 600; font-size: 14px; }
  .pooltable :global(td) { height: 40px; }
  .tiercell { display: inline-flex; align-items: center; gap: 8px; }
  .acts { width: 1%; white-space: nowrap; text-align: right; }
  /* champions they played but haven't put in the pool: quieter, under their own divider */
  .pooltable :global(tr.divider th) { height: 36px; vertical-align: bottom; padding-bottom: 6px; background: none; box-shadow: none; }
  .pooltable :global(tbody.outside td) { color: var(--text-3); }
  .addrow { display: flex; align-items: flex-start; gap: 8px; margin-top: 12px; }
  @media (max-width: 1180px) { main :global(.wideonly) { display: none; } .tiercell { flex-wrap: wrap; padding: 4px 0; } }

  .two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 48px; }
  @media (max-width: 1180px) { .two { grid-template-columns: minmax(0, 1fr); } }
  .barcell { width: 40%; }
  .track { display: block; height: 4px; border-radius: 2px; background: var(--surface-2); overflow: hidden; }
  .track span { display: block; height: 100%; background: var(--text-2); }
  .share { width: 56px; }
  .exline { display: flex; flex-wrap: wrap; gap: 12px 40px; margin-bottom: 16px; }
  .narrow { max-width: 640px; }
  .body { margin: 0 0 12px; font-size: 13px; line-height: 1.5; color: var(--text-2); }
  .body strong { color: var(--text-1); }
</style>
