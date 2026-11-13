<script>
  // Add player (docs/roster-profiles.md): four steps in one sheet, each with Back.
  //   1 Who (Riot ID and role; the account is looked up)   2 Reading their games (can be skipped)
  //   3 Review the proposed pool (edit, add, paste)        4 Where they go (active or bench)
  // Nothing is saved until the last step's confirm. Without a Riot key steps 1-2 are skipped over: the pool is built
  // by hand.
  import Champ from './Champ.svelte';
  import Sheet from './ui/Sheet.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';
  import Table from './ui/Table.svelte';
  import Tag from './ui/Tag.svelte';
  import RadioList from './ui/RadioList.svelte';
  import { ROLES, ROLE_LABELS, TIER_OPTIONS, api, nameOf, pct, profileHref, rankText } from './roster.js';

  let { open = $bindable(false), role = 'top', active = true, lineup = [], champions = [], oncreated = () => {} } = $props();

  let step = $state(1);
  let riotId = $state('');
  let chosenRole = $state('top');
  let goActive = $state(true);
  let found = $state(null); // the lookup's answer
  let looking = $state(false);
  let lookupError = $state('');
  let job = $state(null); // the onboarding job's id
  let reading = $state(null); // its latest status
  let rows = $state([]); // the pool under review: { champ_id, champ, tier, source, games, wins, mastery, include }
  let alsoPlays = $state([]);
  let addName = $state('');
  let addTier = $state('learning');
  let addError = $state('');
  let pasted = $state('');
  let pasteErrors = $state([]);
  let saving = $state(false);
  let saveError = $state('');
  let poll;

  // a fresh sheet each time it opens
  $effect(() => {
    if (!open) { clearInterval(poll); return; }
    step = 1; riotId = ''; chosenRole = role; goActive = active; found = null; lookupError = ''; job = null; reading = null;
    rows = []; alsoPlays = []; addName = ''; addError = ''; pasted = ''; pasteErrors = []; saveError = '';
  });
  const validId = $derived(/^[^#\s][^#]*#[^#\s]+$/.test(riotId.trim()));
  const currentInRole = $derived(lineup.find((r) => r.role === chosenRole)?.riot_id ?? '');
  const included = $derived(rows.filter((r) => r.include));

  async function lookup() {
    if (!validId || looking || found?.for === riotId.trim().toLowerCase()) return;
    looking = true;
    lookupError = '';
    try {
      found = { ...(await api('/api/players/lookup', { method: 'POST', body: { riot_id: riotId.trim() } })), for: riotId.trim().toLowerCase() };
      if (found.found && !found.on_roster) riotId = found.riot_id; // the account's own spelling
      if (found.found) found.for = riotId.trim().toLowerCase();
    } catch (e) {
      found = null;
      lookupError = `Couldn't look them up: ${e.message}`;
    } finally {
      looking = false;
    }
  }

  async function next() {
    if (step === 1) {
      await lookup();
      if (lookupError || !found || found.on_roster || found.found === false) return;
      if (found.no_key) { step = 3; return; }
      await startReading();
    } else if (step === 3) {
      step = 4;
    }
  }
  async function startReading() {
    step = 2;
    reading = { status: 'reading', games_read: 0, games_total: 0 };
    try {
      job = (await api('/api/players/onboard', { method: 'POST', body: { riot_id: riotId.trim(), role: chosenRole } })).job;
    } catch (e) {
      reading = { status: 'error', error: e.message };
      return;
    }
    clearInterval(poll);
    poll = setInterval(async () => {
      const status = await api(`/api/players/onboard/${job}`).catch(() => null);
      if (!status) return;
      reading = status;
      if (status.status !== 'reading') { clearInterval(poll); if (status.status === 'done') review(status); }
    }, 700);
  }
  async function skip() {
    clearInterval(poll);
    const status = await api(`/api/players/onboard/${job}/skip`, { method: 'POST' }).catch(() => reading);
    // the job stops at its next match and proposes from what it has; one more look gets that proposal
    setTimeout(async () => review(await api(`/api/players/onboard/${job}`).catch(() => status)), 400);
  }
  function review(status) {
    rows = (status.proposal ?? []).map((r) => ({ ...r, include: true }));
    alsoPlays = status.also_plays ?? [];
    step = 3;
  }
  function back() {
    clearInterval(poll);
    if (step === 2 || (step === 3 && (found?.no_key || !job))) step = 1;
    else if (step === 3) step = 1; // reading again is one click; the proposal isn't kept across a change of player
    else if (step === 4) step = 3;
  }

  function addChampion() {
    addError = '';
    const champ = champions.find((c) => c.name.toLowerCase() === addName.trim().toLowerCase());
    if (!champ) { addError = `No champion called "${addName.trim()}"`; return; }
    put(champ.id, champ, addTier, 'added');
    addName = '';
  }
  function put(champId, champ, tier, source) {
    const at = rows.findIndex((r) => r.champ_id === champId);
    if (at >= 0) rows[at] = { ...rows[at], tier, include: true };
    else rows = [...rows, { champ_id: champId, champ, tier, source, games: 0, wins: 0, mastery: 0, include: true }];
  }
  async function readPaste() {
    pasteErrors = [];
    try {
      const out = await api('/api/pools/parse', { method: 'POST', body: { text: pasted, role: chosenRole } });
      for (const e of out.entries) put(e.champ_id, e.champ, e.tier, 'pasted');
      pasteErrors = out.errors;
      if (!out.errors.length) pasted = '';
    } catch (e) {
      pasteErrors = [e.message];
    }
  }

  async function create() {
    saving = true;
    saveError = '';
    try {
      const roster = await api('/api/players', { method: 'POST', body: {
        riot_id: riotId.trim(), role: chosenRole, active: goActive, pool: included.map((r) => ({ champ_id: r.champ_id, role: chosenRole, tier: r.tier })) } });
      open = false;
      oncreated(roster, riotId.trim(), chosenRole, goActive);
    } catch (e) {
      saveError = `Not added: ${e.message}`;
    } finally {
      saving = false;
    }
  }
  const SOURCE = { games: 'games', mastery: 'mastery', added: 'added by hand', pasted: 'pasted' };
  const TITLES = ['Who', 'Reading their games', 'Review the pool', 'Where they go'];
</script>

<Sheet bind:open title="Add player" description="Step {step} of 4: {TITLES[step - 1]}" width={step === 3 ? 640 : 480} {step} onclose={() => clearInterval(poll)}>
  {#if step === 1}
    <form class="stack" onsubmit={(e) => { e.preventDefault(); next(); }}>
      <Field label="Riot ID" bind:value={riotId} placeholder="Name#TAG" autocomplete="off" spellcheck="false" onblur={lookup}
             oninput={() => { found = null; lookupError = ''; }}
             hint="As it shows in the client, with the tag after #" />
      <Field label="Role" bind:value={chosenRole} options={ROLES.map((r) => [r, ROLE_LABELS[r]])} />
      <div class="result" aria-live="polite">
        {#if looking}<p class="quiet">Looking them up…</p>
        {:else if lookupError}<p class="bad" role="alert">⚠ {lookupError}</p>
        {:else if found?.on_roster}
          <p class="bad" role="alert">⚠ {found.riot_id} is already on the roster ({ROLE_LABELS[found.role]}, {found.active ? 'active' : 'on the bench'}).
            <a href={profileHref(found.riot_id)} onclick={() => (open = false)}>Open their profile</a></p>
        {:else if found?.no_key}<p class="quiet">No Riot key: you can still add them and build the pool by hand.</p>
        {:else if found?.found === false}<p class="bad" role="alert">⚠ No such account. Check the name and the tag after #.</p>
        {:else if found?.found}
          <p class="ok"><strong>{found.riot_id}</strong> <span>{[found.level ? `level ${found.level}` : null,
            found.rank?.best ? `${rankText(found.rank.best)} (${found.rank.best.queue})` : 'unranked this season'].filter(Boolean).join(' · ')}</span></p>
        {/if}
      </div>
    </form>
  {:else if step === 2}
    <div class="stack" aria-live="polite">
      {#if reading?.status === 'error'}
        <p class="bad" role="alert">⚠ {reading.error}</p>
        <p class="quiet">You can go back and fix the Riot ID, or go on and build the pool by hand.</p>
      {:else}
        <p class="reading"><strong>{riotId}</strong>: ranked games of the last 60 days, mastery and rank.</p>
        <p class="progress mono">{reading?.games_total ? `${reading.games_read} of ${reading.games_total} games` : 'Finding their games…'}</p>
        <span class="bar" role="progressbar" aria-valuemin="0" aria-valuemax={reading?.games_total || 1} aria-valuenow={reading?.games_read ?? 0}>
          <span style:transform="scaleX({reading?.games_total ? reading.games_read / reading.games_total : 0})"></span></span>
        <p class="quiet">Usually under a minute. A running crawl pauses for it.</p>
      {/if}
    </div>
  {:else if step === 3}
    <div class="stack">
      {#if rows.length}
        <Table dense sticky={false}>
          <thead><tr><th scope="col"><span class="sr">Include</span></th><th scope="col">Champion</th><th scope="col">Tier</th>
            <th scope="col" class="num" title="Games in this role, last 60 days">Games</th><th scope="col" class="num">Won</th>
            <th scope="col" class="num">Mastery</th><th scope="col">From</th></tr></thead>
          <tbody>
            {#each rows as r (r.champ_id)}
              <tr class:off={!r.include}>
                <td class="tick"><Field type="checkbox" label="" aria-label="Include {r.champ?.name}" bind:checked={r.include} /></td>
                <td><span class="c"><Champ champ={r.champ} size={20} /><strong>{r.champ?.name}</strong></span></td>
                <td><Field label="Tier for {r.champ?.name}" hideLabel width="132px" bind:value={r.tier} options={TIER_OPTIONS} disabled={!r.include} /></td>
                <td class="num mono">{r.games || '—'}</td>
                <td class="num mono">{r.games ? pct(r.wins / r.games) : '—'}</td>
                <td class="num mono">{r.mastery ? r.mastery.toLocaleString() : '—'}</td>
                <td><Tag bare>{SOURCE[r.source] ?? r.source}</Tag></td>
              </tr>
            {/each}
          </tbody>
        </Table>
      {:else}
        <p class="quiet">{found?.no_key ? 'No Riot key, so nothing was read.' : `No ${ROLE_LABELS[chosenRole]} games or mastery to go on.`} Add their champions below.</p>
      {/if}
      {#if alsoPlays.length}
        <p class="quiet">Also plays {alsoPlays.map((a) => `${ROLE_LABELS[a.role]} (${pct(a.share)} of their games)`).join(', ')}.</p>
      {/if}
      <form class="addrow" onsubmit={(e) => { e.preventDefault(); addChampion(); }}>
        <Field label="Add a champion" bind:value={addName} list="add-player-champs" placeholder="Champion name" autocomplete="off" error={addError} />
        <Field label="Tier" bind:value={addTier} options={TIER_OPTIONS} width="140px" />
        <Button type="submit" disabled={!addName.trim()}>Add</Button>
      </form>
      <datalist id="add-player-champs">{#each champions as c}<option value={c.name}></option>{/each}</datalist>
      <details class="paste">
        <summary>Paste a pool</summary>
        <Field label="One champion a line, with its tier" type="textarea" mono bind:value={pasted} placeholder={"K'Sante: main\nRumble: comfortable\nGnar: learning"} />
        {#each pasteErrors as e}<p class="bad">⚠ {e}</p>{/each}
        <div><Button onclick={readPaste} disabled={!pasted.trim()}>Read into the pool</Button></div>
      </details>
    </div>
  {:else}
    <div class="stack">
      <p class="reading"><strong>{riotId}</strong> · {ROLE_LABELS[chosenRole]} · {included.length} champion{included.length === 1 ? '' : 's'} in the pool</p>
      <RadioList label="Where they go" bind:value={goActive} options={[
        { value: true, label: `Active ${ROLE_LABELS[chosenRole]}`,
          note: currentInRole ? `${nameOf(currentInRole)} goes to the bench with their pool.` : 'The role is empty: nobody is benched.' },
        { value: false, label: 'On the bench', note: 'The lineup stays as it is; swap them in when they play.' }]} />
      {#if saveError}<p class="bad" role="alert">⚠ {saveError}</p>{/if}
    </div>
  {/if}

  {#snippet footer()}
    {#if step > 1}<span class="backwrap"><Button variant="ghost" onclick={back}>Back</Button></span>{/if}
    {#if step === 1}
      <Button variant="primary" size="md" disabled={!validId || found?.on_roster || found?.found === false} busy={looking} onclick={next}>
        {found?.no_key ? 'Build the pool by hand' : 'Read their games'}</Button>
    {:else if step === 2}
      {#if reading?.status === 'error'}
        <Button variant="primary" size="md" onclick={() => review({})}>Build the pool by hand</Button>
      {:else}
        <Button size="md" disabled={!job} onclick={skip} title="Go on with the games read so far">Skip the rest</Button>
      {/if}
    {:else if step === 3}
      <Button variant="primary" size="md" onclick={next}>{included.length ? `Go on with ${included.length} champion${included.length === 1 ? '' : 's'}` : 'Go on with an empty pool'}</Button>
    {:else}
      <Button variant="primary" size="md" busy={saving} onclick={create}>Add {nameOf(riotId)}</Button>
    {/if}
  {/snippet}
</Sheet>

<style>
  .stack { display: flex; flex-direction: column; gap: 14px; margin: 0; }
  .result { min-height: 40px; }
  p { margin: 0; font-size: 13px; line-height: 1.45; }
  .quiet { color: var(--text-3); }
  .bad { color: var(--error); }
  .bad a { color: var(--text-1); margin-left: 4px; }
  .ok { color: var(--text-2); }
  .ok strong, .reading strong { color: var(--text-1); font-weight: 600; }
  .ok strong { margin-right: 6px; }
  .reading { color: var(--text-2); }
  .progress { font-size: 20px; color: var(--text-1); letter-spacing: -0.02em; }
  .bar { display: block; height: 4px; border-radius: 2px; background: var(--surface-3); overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--text-2); transform-origin: left; transition: transform 300ms linear; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .tick { width: 28px; }
  .c { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  .c strong { font-weight: 600; }
  .stack :global(tr.off td) { color: var(--text-3); }
  .stack :global(tr.off .c) { opacity: 0.5; }
  .addrow { display: flex; align-items: flex-start; gap: 8px; }
  .addrow :global(.field:first-child) { flex: 1; }
  .addrow :global(.btn) { margin-top: 20px; }
  .paste { border-top: 1px solid var(--hairline); padding-top: 10px; display: flex; flex-direction: column; gap: 8px; }
  .paste summary { cursor: pointer; font-size: 13px; color: var(--text-2); margin-bottom: 8px; }
  .paste > div { margin-top: 8px; }
  .backwrap { margin-right: auto; }
</style>
