<script>
  // The Roster page (docs/roster-profiles.md, Stage D): the lineup as five profile summaries, the bench, and the three
  // things you do here: add a player, swap, bench or delete. A player's pool and detail live on their profile page.
  import Champ from './Champ.svelte';
  import AddPlayer from './AddPlayer.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Table from './ui/Table.svelte';
  import Sheet from './ui/Sheet.svelte';
  import Menu from './ui/Menu.svelte';
  import Segmented from './ui/Segmented.svelte';
  import RadioList from './ui/RadioList.svelte';
  import { toast } from './ui/toast.svelte.js';
  import { ROLES, ROLE_LABELS, TIER_CODES, TIER_LABELS, TREND, api, day, executionTone, fixed, healthLine, nameOf, pct, playerPath, pp,
           profileHref, rankText, tagOf } from './roster.js';

  let roster = $state(null);
  let failed = $state('');
  let note = $state('');
  let noteFailed = $state(false);

  async function load() {
    try {
      roster = await api('/api/roster');
      failed = '';
    } catch (e) {
      failed = `Couldn't read the roster: ${e.message}`;
    }
  }
  load();
  function say(text, bad = false) { note = text; noteFailed = bad; }
  const later = () => (roster?.in_draft ? ' Live uses it after this draft.' : '');

  // ---- add a player ----
  let adding = $state(false);
  let addRole = $state('top');
  let addActive = $state(true);
  function openAdd(role = null, active = true) {
    addRole = role ?? roster.lineup.find((r) => !r.riot_id)?.role ?? 'top';
    addActive = active;
    adding = true;
  }
  function added(next, riotId, role, active) {
    roster = next;
    say(`${nameOf(riotId)} added${active ? ` as our ${ROLE_LABELS[role]}` : ' to the bench'}.${active ? later() : ''}`);
  }

  // ---- swap: from a lineup row (choose who comes in) or from a benched player (choose the role) ----
  let swapping = $state(null); // { role, player: riot id or '' }
  let swapOpen = $state(false);
  const swapOut = $derived(swapping ? roster.lineup.find((r) => r.role === swapping.role)?.riot_id ?? '' : '');
  const candidates = $derived.by(() => {
    if (!swapping) return [];
    const withPool = roster.bench.filter((b) => b.pool_roles.includes(swapping.role));
    return [...withPool, ...roster.bench.filter((b) => !withPool.includes(b))];
  });
  function openSwap(role, player = '') {
    // the likeliest choice is already made: the first benched player with a pool in this role
    swapping = { role, player, pick: roster.bench.find((b) => b.pool_roles.includes(role))?.riot_id ?? null };
    swapOpen = true;
  }
  const benchedRoles = (b) => (b.pool_roles.length ? b.pool_roles : [b.role]);
  let busy = $state(false);

  async function activate(riotId, role, undoable = true) {
    busy = true;
    try {
      const out = await api(`${playerPath(riotId)}/activate`, { method: 'POST', body: { role } });
      roster = out.roster;
      swapOpen = false;
      const undo = out.undo;
      const text = undo?.riot_id ? `Swapped ${nameOf(riotId)} in for ${nameOf(undo.riot_id)}` : `${nameOf(riotId)} is our ${ROLE_LABELS[role]} now`;
      say(`${text}.${later()}`);
      if (undo && undoable) {
        toast(text, { seconds: 10, action: { label: 'Undo', onclick: () => (undo.riot_id ? activate(undo.riot_id, undo.role, false) : bench(undo.bench, false)) } });
      }
    } catch (e) {
      say(`Not swapped: ${e.message}`, true);
    } finally {
      busy = false;
    }
  }
  async function bench(riotId, undoable = true) {
    const row = roster.lineup.find((r) => r.riot_id === riotId);
    try {
      roster = await api(`${playerPath(riotId)}/bench`, { method: 'POST' });
      say(`${nameOf(riotId)} is on the bench with their pool.${later()}`);
      if (undoable && row) toast(`${nameOf(riotId)} benched`, { seconds: 10, action: { label: 'Undo', onclick: () => activate(riotId, row.role, false) } });
    } catch (e) {
      say(`Not benched: ${e.message}`, true);
    }
  }

  // ---- delete: its own dialog, never one click away ----
  let deleting = $state(null);
  let deleteOpen = $state(false);
  function askDelete(p) { deleting = p; deleteOpen = true; }
  async function confirmDelete() {
    busy = true;
    try {
      roster = await api(`${playerPath(deleting.riot_id)}?confirm=${encodeURIComponent(deleting.riot_id)}`, { method: 'DELETE' });
      say(`${deleting.riot_id} deleted.`);
      deleteOpen = false;
    } catch (e) {
      say(`Not deleted: ${e.message}`, true);
    } finally {
      busy = false;
    }
  }

  // ---- update from match history (the learning job) ----
  let learning = $derived(roster?.learning?.running ?? false);
  async function learn() {
    try {
      await api('/api/learning/run', { method: 'POST', body: {} });
      roster.learning = { running: true };
      const poll = setInterval(async () => {
        const state = await api('/api/learning').catch(() => null);
        if (!state || state.running) return;
        clearInterval(poll);
        await load();
        const s = state.summary ?? {};
        if (s.error) say(s.error, true);
        else say(`Updated from games: ${s.proposals ?? 0} tier change${s.proposals === 1 ? '' : 's'} to review`
          + (s.history_error ? `. Match history unavailable: ${s.history_error}` : '.'), !!s.history_error);
      }, 2000);
    } catch (e) {
      say(`Couldn't start the update: ${e.message}`, true);
    }
  }
</script>

<main class="console">
  <PageHeader title="Roster" description="The five who play, the bench, and how each player and their pool are doing. Open a profile for the detail.">
    {#snippet actions()}
      <Button onclick={learn} disabled={!roster || learning} busy={learning}
              title="Reads our logged games and, with a Riot key, each player's ranked games of the last 60 days, then re-scores comfort and tiers">
        {learning ? 'Updating from games…' : 'Update from games'}</Button>
      <Button variant="primary" size="md" disabled={!roster} onclick={() => openAdd()}>Add player</Button>
    {/snippet}
  </PageHeader>
  {#if note}<p class="note" class:failed={noteFailed} role={noteFailed ? 'alert' : 'status'}>{noteFailed ? '⚠ ' : ''}{note}</p>{/if}

  {#if failed}
    <p class="note failed" role="alert">⚠ {failed} <Button variant="ghost" onclick={load}>Try again</Button></p>
  {:else if roster}
    <section aria-label="Lineup">
      <h2>Lineup</h2>
      <Table sticky={false}>
        <thead><tr>
          <th scope="col">Role</th><th scope="col">Player</th><th scope="col">Pool</th>
          <th scope="col" class="num" title="Ranked games in this role, last 60 days">Games</th>
          <th scope="col" class="num">Win rate</th><th scope="col" class="num wideonly">KDA</th>
          <th scope="col" class="num" title="Their share of our early games, pp of a win a game, over the team games they played">Lane execution</th>
          <th scope="col" class="wideonly">Pool health</th><th scope="col"><span class="sr">Actions</span></th>
        </tr></thead>
        <tbody>
          {#each roster.lineup as p (p.role)}
            {@const health = healthLine(p.health)}
            <tr class="lineup">
              <td class="role mono">{p.role_label}</td>
              {#if p.riot_id}
                <td>
                  <a class="who" href={profileHref(p.riot_id)}><strong>{nameOf(p.riot_id)}</strong><span class="tag">{tagOf(p.riot_id)}</span></a>
                  <div class="sub">{[p.rank ? rankText(p.rank) : 'Rank not read', p.since_days == null ? null : p.since_days === 0 ? 'since today' : `${p.since_days} day${p.since_days === 1 ? '' : 's'} in the role`].filter(Boolean).join(' · ')}</div>
                </td>
                <td>
                  {#if p.pool.size}
                    <span class="strip">
                      {#each p.pool.champs as c}
                        <span class="pc" title="{c.champ?.name}: {TIER_LABELS[c.tier]}"><Champ champ={c.champ} size={28} /><span class="code mono">{TIER_CODES[c.tier]}</span></span>
                      {/each}
                      {#if p.pool.more}<span class="more mono" title="{p.pool.more} more in the pool">+{p.pool.more}</span>{/if}
                    </span>
                  {:else}<span class="quiet">Empty pool</span>{/if}
                  {#if p.pending}<div class="sub"><a href="{profileHref(p.riot_id)}">{p.pending} tier change{p.pending === 1 ? '' : 's'} suggested</a></div>{/if}
                </td>
                <td class="num mono">{p.games}</td>
                <td class="num mono">{pct(p.win_rate)}</td>
                <td class="num mono wideonly" title={p.stat_games ? `From ${p.stat_games} game${p.stat_games === 1 ? '' : 's'} with figures` : 'No games with figures yet'}>{fixed(p.kda)}</td>
                <td class="num">
                  {#if p.execution.enough}
                    <span class="mono {executionTone(p.execution) ?? ''}">{pp(p.execution.mean_pp)}</span>
                    {#if p.execution.trend}<span class="trend mono" title="Trend: {TREND[p.execution.trend][1]}">{TREND[p.execution.trend][0]}</span>{/if}
                    <div class="sub">{p.execution.label} · {p.execution.games} team games</div>
                  {:else}
                    <span class="quiet" title="Lane execution is read from team games with a timeline">Needs {p.execution.needed} team games</span>
                  {/if}
                </td>
                <td class="wideonly">{#if health}<span class:warn={health.warn}>{health.warn ? '⚠ ' : ''}{health.text}</span>{:else}<span class="quiet">—</span>{/if}</td>
                <td class="acts">
                  <span class="actrow">
                    <span class="wideonly"><Button variant="ghost" href={profileHref(p.riot_id)}>Profile</Button></span>
                    <Button onclick={() => openSwap(p.role)}>Swap</Button>
                    <Button variant="ghost" onclick={() => bench(p.riot_id)} title="Off the lineup; their pool waits on the bench">Bench</Button>
                    <Menu label="More for {p.riot_id}" items={[{ label: 'Delete…', danger: true, onclick: () => askDelete(p) }]} />
                  </span>
                </td>
              {:else}
                <td colspan="7"><span class="quiet">No player</span></td>
                <td class="acts">
                  <span class="actrow">
                    {#if roster.bench.length}<Button onclick={() => openSwap(p.role)}>Swap in</Button>{/if}
                    <Button onclick={() => openAdd(p.role)}>Add player</Button>
                  </span>
                </td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </Table>
    </section>

    <section aria-label="Bench">
      <h2>Bench <span class="sub2">players swapped out keep their pool here</span></h2>
      <Table sticky={false}>
        <thead><tr>
          <th scope="col">Player</th><th scope="col">Pools in</th><th scope="col">Rank</th>
          <th scope="col" class="num" title="Ranked games in their role, last 60 days">Games</th><th scope="col" class="num">Last played</th>
          <th scope="col"><span class="sr">Actions</span></th>
        </tr></thead>
        <tbody>
          {#each roster.bench as b (b.riot_id)}
            <tr>
              <td><a class="who" href={profileHref(b.riot_id)}><strong>{nameOf(b.riot_id)}</strong><span class="tag">{tagOf(b.riot_id)}</span></a></td>
              <td>{#if b.pool_roles.length}{b.pool_roles.map((r) => ROLE_LABELS[r]).join(', ')}{:else}<span class="quiet">No pool yet</span>{/if}</td>
              <td>{#if b.rank}{rankText(b.rank)}{:else}<span class="quiet">—</span>{/if}</td>
              <td class="num mono">{b.games}</td>
              <td class="num mono">{day(b.last_played_ms)}</td>
              <td class="acts">
                <span class="actrow">
                  <Button variant="ghost" href={profileHref(b.riot_id)}>Profile</Button>
                  <Button onclick={() => openSwap(benchedRoles(b)[0], b.riot_id)}>Swap in</Button>
                  <Menu label="More for {b.riot_id}" items={[{ label: 'Delete…', danger: true, onclick: () => askDelete(b) }]} />
                </span>
              </td>
            </tr>
          {:else}
            <tr><td colspan="6" class="quiet">Nobody on the bench. A player you swap out waits here with their pool.</td></tr>
          {/each}
        </tbody>
      </Table>
    </section>

    <AddPlayer bind:open={adding} role={addRole} active={addActive} lineup={roster.lineup} champions={roster.champions} oncreated={added} />

    <!-- the swap sheet -->
    <Sheet bind:open={swapOpen} title={swapping?.player ? `Swap ${nameOf(swapping.player)} in` : `Swap ${ROLE_LABELS[swapping?.role] ?? ''}`}
           description={swapOut ? `${nameOf(swapOut)} goes to the bench with their pool.` : 'This role is empty: nobody goes to the bench.'}>
      {#if swapping}
        {#if swapping.player}
          {@const b = roster.bench.find((x) => x.riot_id === swapping.player)}
          <p class="label-caps">Role</p>
          <Segmented label="Role to play" bind:value={swapping.role} options={ROLES.map((r) => [r, ROLE_LABELS[r]])} />
          <p class="hint">{b?.pool_roles.includes(swapping.role) ? `Their ${ROLE_LABELS[swapping.role]} pool comes with them.` : `They have no ${ROLE_LABELS[swapping.role]} pool yet: they start with an empty one.`}</p>
        {:else}
          <p class="label-caps">Who comes in</p>
          <RadioList label="Who comes in" bind:value={swapping.pick} options={[
            ...candidates.map((c) => ({ value: c.riot_id, label: c.riot_id,
              note: (c.pool_roles.includes(swapping.role) ? `${ROLE_LABELS[swapping.role]} pool ready` : `no ${ROLE_LABELS[swapping.role]} pool yet`)
                + (c.games ? ` · ${c.games} games, ${pct(c.win_rate)} won` : '') + (c.rank ? ` · ${c.rank.label}` : '') })),
            { value: '', label: 'Someone new…', note: 'add a player with their Riot ID' }]} />
        {/if}
        {#if roster.in_draft}<p class="hint">A champ select is on: Live keeps the current lineup until this draft ends.</p>{/if}
      {/if}
      {#snippet footer()}
        <Button variant="ghost" onclick={() => (swapOpen = false)}>Cancel</Button>
        {#if swapping?.player}
          <Button variant="primary" size="md" busy={busy} onclick={() => activate(swapping.player, swapping.role)}>Swap in as {ROLE_LABELS[swapping.role]}</Button>
        {:else if swapping?.pick === ''}
          <Button variant="primary" size="md" onclick={() => { swapOpen = false; openAdd(swapping.role, true); }}>Add a new player</Button>
        {:else}
          <Button variant="primary" size="md" disabled={!swapping?.pick} busy={busy} onclick={() => activate(swapping.pick, swapping.role)}>
            {swapping?.pick ? `Swap ${nameOf(swapping.pick)} in` : 'Choose who comes in'}</Button>
        {/if}
      {/snippet}
    </Sheet>

    <!-- delete: says exactly what goes and what stays -->
    <Sheet dialog bind:open={deleteOpen} title={`Delete ${deleting?.riot_id ?? ''}?`}>
      <p class="body">Their pools and the games fetched for them are removed for good. The team's history stays: drafts,
        and the games they played with us, keep their place in History and Team trends.</p>
      <p class="body"><strong>This can't be undone.</strong> To keep their pool for later, bench them instead.</p>
      {#snippet footer()}
        <Button variant="ghost" onclick={() => (deleteOpen = false)}>Cancel</Button>
        <Button danger size="md" busy={busy} onclick={confirmDelete}>Delete {nameOf(deleting?.riot_id ?? '')}</Button>
      {/snippet}
    </Sheet>
  {:else}
    <p class="note">Loading…</p>
  {/if}
</main>

<style>
  .note { margin: -8px 0 12px; font-size: 13px; color: var(--text-2); display: flex; align-items: center; gap: 8px; }
  .note.failed { color: var(--error); }
  section { margin-bottom: 32px; }
  h2 { margin: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .sub2 { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }

  /* a lineup row is a profile summary: two lines, so it is taller than a table row */
  main :global(tr.lineup > td) { height: 60px; padding-top: 8px; padding-bottom: 8px; }
  .role { font-size: 11px; text-transform: uppercase; color: var(--text-3); width: 72px; }
  .who { color: var(--text-1); text-decoration: none; white-space: nowrap; }
  .who strong { font-size: 14px; font-weight: 600; }
  .who:hover strong { text-decoration: underline; text-underline-offset: 3px; }
  .tag { color: var(--text-3); font-size: 12px; margin-left: 2px; }
  .sub { font-size: 12px; color: var(--text-3); font-family: var(--font-ui); white-space: nowrap; }
  .sub a { color: var(--text-2); }
  .quiet { color: var(--text-3); }
  .warn { color: var(--warn); }
  .strip { display: inline-flex; align-items: center; gap: 4px; }
  .pc { position: relative; display: inline-flex; }
  .code { position: absolute; right: -2px; bottom: -3px; padding: 0 2px; font-size: 9px; font-weight: 600; line-height: 11px; color: var(--text-2);
          background: var(--bg); border-radius: 2px; }
  .more { margin-left: 4px; font-size: 12px; color: var(--text-3); }
  .trend { margin-left: 4px; color: var(--text-3); font-size: 12px; }
  .acts { width: 1%; }
  .actrow { display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px; white-space: nowrap; }
  /* narrow windows: KDA, pool health and the Profile button give way (the name is the link to the profile) */
  @media (max-width: 1180px) { main :global(.wideonly) { display: none; } .sub { white-space: normal; } }

  /* sheets */
  .hint { margin: 12px 0 0; font-size: 13px; color: var(--text-3); }
  .label-caps { margin: 8px 0 8px; }
  .body { margin: 0 0 12px; font-size: 13px; line-height: 1.5; color: var(--text-2); }
  .body strong { color: var(--text-1); }
</style>
