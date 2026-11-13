<script>
  import { pulse } from './motion.js';
  // The crawl and the other data jobs, from the app (src/draftassist/jobs.py): one at a time, each its own process,
  // so a long crawl keeps going if the app closes, and the app finds it again when reopened.
  import { patchLabel, riotPatch } from './patch.js';
  import PageHeader from './ui/PageHeader.svelte';
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';
  import Table from './ui/Table.svelte';
  import Panel from './ui/Panel.svelte';
  import Stat from './ui/Stat.svelte';
  import Tag from './ui/Tag.svelte';

  const CRAWLS = ['crawl', 'timelines', 'master', 'refetch'];
  const TARGET = 20000; // matches per tier the spec's coverage check wants (Phase 3)
  let data = $state(null);
  let crawlJob = $state('crawl');
  let hours = $state(8);
  let message = $state('');
  let now = $state(Date.now() / 1000);
  let logBox = $state(null);
  // crawl options (jobs.crawl_options); '' = the current patch / every tier
  // the tier picker's sets: the default crawl, above it, and Master+ (its own database, data/master.py)
  const TIER_SETS = [
    { id: 'emerald_minus', label: 'Emerald and below', tiers: ['iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald'] },
    { id: 'emerald_plus', label: 'Above Emerald', tiers: ['diamond'] },
    { id: 'master_plus', label: 'Master+', tiers: ['master', 'grandmaster', 'challenger'], apex: true },
  ];
  const APEX = TIER_SETS[2].tiers;
  const isApex = (t) => APEX.includes(t);
  function toggleCrawlTier(t) {
    if (tiers.includes(t)) { tiers = tiers.filter((x) => x !== t); return; }
    // Master+ crawls into its own database: one kind or the other per crawl
    tiers = [...tiers.filter((x) => isApex(x) === isApex(t)), t];
  }
  function toggleSet(set) {
    const all = set.tiers.every((t) => tiers.includes(t));
    tiers = all ? tiers.filter((t) => !set.tiers.includes(t))
                : [...new Set([...tiers.filter((x) => isApex(x) === !!set.apex), ...set.tiers])];
  }
  const apexPicked = $derived(tiers.some(isApex));
  let patch = $state('');
  let tiers = $state([]);
  let idsPerQueue = $state(20);
  let noTimelines = $state(false);
  // the tier table: follows the running crawl's patch and database unless one is picked here
  let viewPatch = $state('');
  let viewDb = $state('');
  const running = $derived(data?.running);
  const prog = $derived(data?.progress);
  const shownDb = $derived(viewDb || (running?.job === 'master' ? 'master' : prog?.database && running ? prog.database : 'main'));
  const shownPatch = $derived(viewPatch || (running && prog?.patch && shownDb === 'main' ? prog.patch : ''));

  async function load() {
    try {
      const q = new URLSearchParams({ database: shownDb, ...(shownPatch ? { patch: shownPatch } : {}) });
      data = await (await fetch(`/api/jobs?${q}`)).json();
    } catch { /* the app is restarting */ }
  }
  load();
  // a steady poll: every 2 s while a job runs (the tier counts and this run's numbers move live), else every 15 s
  let busy = $derived(!!data?.running);
  $effect(() => {
    const t = setInterval(load, busy ? 2000 : 15000);
    return () => clearInterval(t);
  });
  $effect(() => { shownDb; shownPatch; load(); });
  $effect(() => {
    const t = setInterval(() => (now = Date.now() / 1000), 1000);
    return () => clearInterval(t);
  });
  $effect(() => { data?.log; if (logBox) logBox.scrollTop = logBox.scrollHeight; });

  const jobs = $derived(Object.fromEntries((data?.jobs ?? []).map((j) => [j.id, j])));
  const others = $derived((data?.jobs ?? []).filter((j) => !CRAWLS.includes(j.id) && !(j.id in STORAGE_JOBS)));
  async function post(url, body) {
    message = '';
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
    const out = await res.json();
    if (res.ok) data = out;
    else message = out.detail;
  }
  const takesOptions = (job) => job === 'crawl' || job === 'timelines';
  const takesTiers = (job) => job === 'crawl' || job === 'timelines' || job === 'master';
  const start = (job) => post('/api/jobs/start', {
    job, hours: jobs[job]?.hours ? +hours : null,
    options: takesTiers(job) ? { patch: patch || null, brackets: tiers.length ? tiers : null,
                                   ids_per_queue: +idsPerQueue === 20 ? null : +idsPerQueue, no_timelines: noTimelines } : null,
  });
  const PHASES = { starting: 'Starting', seeding: 'Finding players (ladder)', timelines: 'Fetching gold leads (timelines)',
                   crawling: 'Crawling matches', aggregating: 'Refreshing stats', done: 'Done' };
  const runStored = $derived(prog?.stored ? Object.values(prog.stored).reduce((a, b) => a + b, 0) : 0);
  const perHour = $derived(running && prog?.started && runStored ? Math.round(runStored / Math.max(1 / 60, (now - prog.started) / 3600)) : null);
  const stop = () => post('/api/jobs/stop');

  function since(t) {
    const s = Math.max(0, Math.round(now - t));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
  }
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  const result = (h) => (h.stopped ? 'stopped' : h.exit === 0 ? 'finished' : h.exit == null ? 'ended' : `failed (exit ${h.exit})`);
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const cap = (w) => (w ? w[0].toUpperCase() + w.slice(1) : '');
  const failedRun = (h) => !h.stopped && h.exit != null && h.exit !== 0;
  // each job's latest run, for the Other jobs table
  const lastRun = $derived.by(() => {
    const out = {};
    for (const h of data?.history ?? []) if (!out[h.label] || h.started > out[h.label].started) out[h.label] = h;
    return out;
  });
  const JOB_COLUMNS = [{ key: 'label', label: 'Job', width: '220px' }, { key: 'about', label: 'What it does' },
                       { key: 'last', label: 'Last run', width: '200px' }, { key: 'run', label: '', width: '72px', align: 'right' }];

  // shared data (docs/shared-data.md, Stage 2): the last pull, quietly, and "Sync now"
  let shared = $state(null);
  let syncing = $state(false);
  async function loadShared() {
    try { shared = await (await fetch('/api/shared/status')).json(); } catch { /* restarting */ }
  }
  loadShared();
  async function syncNow() {
    syncing = true;
    try { shared = await (await fetch('/api/shared/sync', { method: 'POST' })).json(); } finally { syncing = false; }
  }
  const role = $derived(data?.role ?? 'solo');

  // members (admin, docs/shared-data.md Stage 3): an invite is shown once, right after it's made
  let members = $state(null);
  let newMember = $state('');
  let shownInvites = $state(null);  // {name: code}
  let confirmRemove = $state('');
  let memberMessage = $state('');
  async function loadMembers() {
    if (role !== 'admin') return;
    const res = await fetch('/api/members');
    const out = await res.json();
    if (res.ok) members = out.members; else memberMessage = out.detail;
  }
  $effect(() => { if (role === 'admin' && members === null) loadMembers(); });
  async function memberCall(url, method, body) {
    memberMessage = '';
    const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const out = await res.json();
    if (!res.ok) { memberMessage = out.detail; return null; }
    members = out.members;
    return out;
  }
  async function addMember() {
    const name = newMember.trim();
    const out = name && await memberCall('/api/members', 'POST', { name });
    if (out) { shownInvites = { [name]: out.invite }; newMember = ''; }
  }
  async function renewMember(name) {
    const out = await memberCall(`/api/members/${encodeURIComponent(name)}/renew`, 'POST');
    if (out) shownInvites = { [name]: out.invite };
  }
  async function removeMember(name) {
    if (confirmRemove !== name) { confirmRemove = name; return; }
    confirmRemove = '';
    const out = await memberCall(`/api/members/${encodeURIComponent(name)}`, 'DELETE');
    if (out) shownInvites = out.reissued;
  }
  const copy = (text) => navigator.clipboard?.writeText(text);

  // local storage: what takes space, the safe clears, the optimising jobs (data/storage.py)
  let storage = $state(null);
  let storageMessage = $state('');
  const STORAGE_JOBS = { pack: 'pack', archive: 'archive', optimize: 'optimize' };
  async function loadStorage() {
    try { storage = await (await fetch('/api/storage')).json(); } catch { /* restarting */ }
  }
  loadStorage();
  async function clearStorage(what) {
    storageMessage = '';
    const res = await fetch('/api/storage/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({ what }) });
    const out = await res.json();
    if (res.ok) { storage = out; storageMessage = `Freed ${mbs(out.freed)}.`; } else storageMessage = out.detail;
  }
  const mbs = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
  let wasRunning = false;
  $effect(() => { const r = !!data?.running; if (wasRunning && !r) loadStorage(); wasRunning = r; });

  // datasets (docs/shared-data.md Stage 4): the admin's list, the user's selection
  let ds = $state(null);
  let sel = $state(null);
  let selMessage = $state('');
  let newDs = $state({ dataset_id: '', name: '', scope: 'last_n', n: 3, half_life: 6 });
  async function loadDatasets() {
    if (role === 'solo') return;
    try {
      ds = await (await fetch('/api/datasets')).json();
      sel = structuredClone(ds.selection);
    } catch { /* restarting */ }
  }
  $effect(() => { if (role !== 'solo' && ds === null) loadDatasets(); });
  const tiersFor = (b) => sel.tiers[b] ?? [b];
  function toggleTier(b, t) {
    const now = tiersFor(b);
    const next = now.includes(t) ? now.filter((x) => x !== t) : [...now, t];
    sel.tiers = { ...sel.tiers, [b]: next.length ? next : [b] };
  }
  function toggleQueue(q) {
    const next = sel.queues.includes(q) ? sel.queues.filter((x) => x !== q) : [...sel.queues, q];
    if (next.length) sel.queues = next;
  }
  async function saveSelection() {
    selMessage = '';
    const res = await fetch('/api/selection', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sel) });
    const out = await res.json();
    if (!res.ok) { selMessage = out.detail; return; }
    selMessage = out.reload === 'deferred' ? 'Saved: applied when this draft is over.' : 'Applied.';
    ds = { ...ds, fits_from: out.fits_from };
  }
  async function addDataset() {
    selMessage = '';
    const res = await fetch('/api/datasets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(newDs) });
    const out = await res.json();
    if (res.ok) { ds = out; newDs = { dataset_id: '', name: '', scope: 'last_n', n: 3, half_life: 6 }; }
    else selMessage = out.detail;
  }
  const SCOPE_LABELS = { default: "The admin's choice (as before)", current: 'Current patch only', last_n: 'Last N patches',
                         patches: 'Specific patches', all: 'Every patch' };
  const SYNC_STATES = { updated: 'updated', unchanged: 'up to date', offline: "couldn't reach the shared database",
                        draft: 'waits for the draft to end', too_new: 'needs a newer app', too_old: 'waiting for the admin',
                        error: 'failed', local: 'this machine crawls its own data', off: 'not set up' };
</script>

<main class="console">
  <PageHeader title="Crawl" description={data?.account ? data.account.label : 'The crawl and the other data jobs: one at a time, each its own process, so a crawl keeps going if the app closes.'}>
    {#snippet actions()}
      {#if data?.account}<Button variant="ghost" href="#/settings">Account</Button>{/if}
    {/snippet}
  </PageHeader>
  {#if !data}
    <p class="quiet">Loading…</p>
  {:else}
    {#if data.account?.warning}<p class="warn" role="status">⚠ {data.account.warning}</p>{/if}
    {#if !data.riot_key_set && role !== 'user'}
      <p class="warn" role="status">⚠ The crawl needs your Riot API key: add it in <a href="#/settings">Settings</a>.</p>
    {/if}

    {#if shared?.configured}
      <section>
        <div class="sechead"><h2>Shared data</h2><Button onclick={syncNow} busy={syncing}>{syncing ? 'Syncing…' : 'Sync now'}</Button></div>
        <p class="line">
          {#if shared.pull?.published_at}Stats from <span class="mono">{when(shared.pull.published_at)}</span> (publish {shared.pull.publish_id}){:else}Nothing pulled yet{/if}{#if shared.pull?.pulled_at}<span class="quiet"> · pulled {when(shared.pull.pulled_at)}</span>{/if}{#if shared.last}<span class="quiet"> · last sync: {SYNC_STATES[shared.last.state] ?? shared.last.state}</span>{/if}
        </p>
        {#if shared.last?.message && !['updated', 'unchanged'].includes(shared.last.state)}<p class="cap">{shared.last.message}</p>{/if}
        {#if role === 'admin' && data.quota}
          <p class="cap">Rows written to the shared database this month: <span class="mono">{data.quota.rows.toLocaleString()}</span> of
            <span class="mono">{data.quota.limit.toLocaleString()}</span> (<span class="mono">{pct(data.quota.rows, data.quota.limit)}%</span>). Publish warns from 70% and stops at 95%.</p>
        {/if}
      </section>
    {/if}

    {#if role === 'user'}
      <section>
        <h2>What's published</h2>
        {#if data.published?.patches?.length}
          <Table sticky={false}>
            <thead><tr><th scope="col">Patch</th><th scope="col">Matches per tier</th><th scope="col" class="num">Total</th></tr></thead>
            <tbody>
              {#each data.published.patches as p}
                <tr><td class="mono">{patchLabel(p.patch)}</td>
                  <td>{#each Object.entries(p.brackets) as [b, n]}<span class="per1">{cap(b)} <span class="mono">{n.toLocaleString()}</span></span>{/each}</td>
                  <td class="num mono">{p.total.toLocaleString()}</td></tr>
              {/each}
            </tbody>
          </Table>
        {:else}
          <p class="quiet">Nothing pulled yet: Sync now, above. Until then Live uses OP.GG's stats.</p>
        {/if}
        <p class="cap">The team's admin crawls and publishes; this machine pulls. Your own games, History and scouting stay on this machine.</p>
      </section>
    {/if}

    {#if role === 'user' && ds && sel}
      <section>
        <div class="sechead"><h2>Data selection <span class="sub">{ds.fits_from}</span></h2><Button variant="primary" size="md" onclick={saveSelection}>Apply</Button></div>
        <Toolbar label="Data selection">
          <ToolGroup label="Patches"><Field label="Patches" hideLabel width="240px" bind:value={sel.scope} options={Object.entries(SCOPE_LABELS)} /></ToolGroup>
          {#if sel.scope === 'last_n'}<ToolGroup label="N"><Field label="Number of patches" hideLabel type="number" min="1" max="20" width="72px" bind:value={sel.n} /></ToolGroup>{/if}
          {#if ['last_n', 'patches', 'all'].includes(sel.scope)}
            <ToolGroup label="Half-life"><Field label="Half-life in patches" hideLabel type="number" min="0" max="20" step="0.5" width="72px" bind:value={sel.half_life} /><span class="quiet">patches (0 = every patch counts the same)</span></ToolGroup>
          {/if}
        </Toolbar>
        {#if sel.scope === 'patches'}
          <div class="picks"><span class="label-caps">Patches</span>{#each ds.patches as p}
            <Tag onclick={() => (sel.patches = sel.patches.includes(p) ? sel.patches.filter((x) => x !== p) : [...sel.patches, p])} pressed={sel.patches.includes(p)}>{patchLabel(p)}</Tag>{/each}</div>
        {/if}
        {#if sel.scope !== 'default'}
          <div class="picks"><span class="label-caps">Queues</span>
            <Tag onclick={() => toggleQueue(420)} pressed={sel.queues.includes(420)}>Solo/Duo</Tag>
            <Tag onclick={() => toggleQueue(440)} pressed={sel.queues.includes(440)}>Flex</Tag></div>
          {#each ds.player_brackets as b}
            <div class="picks"><span class="label-caps">Tiers for {b} players</span>
              {#each ds.tiers as t}<Tag onclick={() => toggleTier(b, t)} pressed={tiersFor(b).includes(t)}>{cap(t)}</Tag>{/each}</div>
          {/each}
        {/if}
        <div class="picks"><Field type="checkbox" label="Master+ games for the Matchup tree" bind:checked={sel.master_tree} /></div>
        {#if selMessage}<p class="line" role="status" use:pulse={{ value: selMessage, ok: !/^(Not|No) |fail|error|couldn/i.test(selMessage) }}>{selMessage}</p>{/if}
        <p class="cap">Win rates, matchups and synergy are summed from the pulled counts for exactly this choice. Fits (matchup
          model, synergy rules, traits) come from the admin's dataset closest to it, named above. Changes apply between drafts.</p>
      </section>
    {/if}

    {#if role === 'admin' && ds}
      <section>
        <h2>Datasets</h2>
        <Table sticky={false} columns={[{ key: 'dataset_id', label: 'Id', mono: true, width: '160px' }, { key: 'name', label: 'Name', width: '240px' },
                                        { key: 'description', label: 'Patches', value: (d) => `${d.description}${d.source === 'master_plus' ? ' · Master+ database' : ''}` }]}
               rows={ds.datasets} empty="No datasets yet" />
        <form class="formrow" onsubmit={(e) => { e.preventDefault(); addDataset(); }}>
          <Field label="Id" bind:value={newDs.dataset_id} placeholder="last3" width="140px" mono />
          <Field label="Name" bind:value={newDs.name} placeholder="Last 3 patches" width="220px" />
          <Field label="Patches" bind:value={newDs.scope} options={[['current', 'Current'], ['last_n', 'Last N'], ['all', 'Every patch']]} width="140px" />
          {#if newDs.scope === 'last_n'}<Field label="N" type="number" min="1" max="20" bind:value={newDs.n} width="72px" />{/if}
          <Button type="submit" disabled={!newDs.dataset_id.trim() || !newDs.name.trim()}>Add dataset</Button>
        </form>
        <p class="cap">Each dataset gets its own fits, computed and published with the next Publish. Teammates get the one
          closest to their selection.</p>
        {#if selMessage}<p class="warn" role="alert">⚠ {selMessage}</p>{/if}
      </section>
    {/if}

    {#if role === 'admin'}
      <section>
        <h2>Members</h2>
        {#if members === null}<p class="quiet">Loading…</p>{:else}
          <Table sticky={false}>
            <thead><tr><th scope="col">Name</th><th scope="col">Invite expires</th><th scope="col"><span class="sr">Actions</span></th></tr></thead>
            <tbody>
              {#each members as m}
                <tr><td><strong>{m.name}</strong></td>
                  <td><span class="mono">{m.expires_at ? when(m.expires_at) : 'never'}</span>{#if m.expiring}<span class="warn inline">⚠ expiring soon</span>{/if}</td>
                  <td class="acts"><Button onclick={() => renewMember(m.name)}>Renew</Button>
                    <Button danger onclick={() => removeMember(m.name)}>{confirmRemove === m.name ? 'Confirm: new codes for everyone' : 'Remove…'}</Button></td></tr>
              {:else}
                <tr><td colspan="3" class="quiet">No members yet. Create an invite below and send it privately.</td></tr>
              {/each}
            </tbody>
          </Table>
          <form class="formrow" onsubmit={(e) => { e.preventDefault(); addMember(); }}>
            <Field label="Display name" bind:value={newMember} placeholder="No Riot ID needed" width="240px" />
            <Button type="submit" disabled={!newMember.trim()}>Create invite</Button>
          </form>
          {#if shownInvites}
            <div class="invites">
              <p class="line"><strong>Send these privately. They're shown only now.</strong></p>
              {#each Object.entries(shownInvites) as [name, code]}
                <div class="invite"><span>{name}</span><code class="mono">{code}</code><Button onclick={() => copy(code)}>Copy</Button></div>
              {/each}
              <div><Button onclick={() => (shownInvites = null)}>Done</Button></div>
            </div>
          {/if}
          <p class="cap">Turso can only invalidate every token at once, so removing a member rotates the database's keys and
            gives everyone else a new code to send. Renew re-issues one member's code (the old one works until it expires).
            Invites last 90 days; you and the member are warned 14 days ahead.</p>
        {/if}
        {#if memberMessage}<p class="warn" role="alert">⚠ {memberMessage}</p>{/if}
      </section>
    {/if}

    {#if role !== 'user'}
    <section>
      <Panel title="Crawl ranked matches">
        {#snippet actions()}
          {#if running}<Button onclick={stop} title="It picks up where it left off next time">Stop</Button>
          {:else}<Button variant="primary" size="md" onclick={() => start(crawlJob)} disabled={!data.riot_key_set}
                         title={data.riot_key_set ? null : 'Needs a Riot API key (Settings)'}>Start</Button>{/if}
        {/snippet}
        {#if running}
          <p class="runline"><strong>{running.label}</strong> running for <span class="mono">{since(running.started)}</span></p>
          {#if prog}
            <div class="runstats">
              <Stat size="sm" value={runStored.toLocaleString()} label="Matches this run" note={perHour ? `about ${perHour.toLocaleString()} an hour` : ''} />
              <Stat size="sm" value={PHASES[prog.phase] ?? prog.phase} label="Now" note={[prog.database === 'master' ? 'Master+ database' : '', prog.patch ? `patch ${patchLabel(prog.patch)}` : ''].filter(Boolean).join(' · ')} />
              {#if prog.players}<Stat size="sm" value={prog.players.toLocaleString()} label="Players read" />{/if}
              {#if prog.timelines_total}
                <Stat size="sm" value={`${pct(prog.timelines_done, prog.timelines_total)}%`} label="Timelines" note={`${prog.timelines_done.toLocaleString()} of ${prog.timelines_total.toLocaleString()}`} />
              {/if}
            </div>
            {#if prog.stored && Object.keys(prog.stored).length}
              <p class="per">{#each Object.entries(prog.stored) as [b, n]}<span>{cap(b)} <span class="mono">+{n.toLocaleString()}</span></span>{/each}</p>
            {/if}
            <p class="cap">{#if prog.skipped_other_patch}{prog.skipped_other_patch.toLocaleString()} matches from other patches skipped · {/if}Updated {prog.updated ? since(prog.updated) : '—'} ago</p>
          {/if}
          <p class="cap">It keeps going if you close the app, and picks up where it left off if stopped. The numbers here
            update live; Live's stats refresh every 15 minutes and when it ends.</p>
        {:else}
          <Toolbar label="Crawl options">
            <ToolGroup label="What">
              <Field label="What to crawl" hideLabel width="260px" bind:value={crawlJob} options={CRAWLS.filter((id) => jobs[id]).map((id) => [id, jobs[id].label])} />
            </ToolGroup>
            {#if jobs[crawlJob]?.hours}
              <ToolGroup label="For"><Field label="Hours" hideLabel type="number" min="0.5" max="24" step="0.5" width="72px" bind:value={hours} /><span class="quiet">hours</span></ToolGroup>
            {/if}
            {#if takesOptions(crawlJob)}
              <ToolGroup label="Patch">
                <Field label="Patch" hideLabel width="160px" bind:value={patch}
                       options={[['', `Current (${patchLabel(data.crawl.current) ?? '—'})`], ...data.crawl.patches.filter((p) => p !== data.crawl.current).map((p) => [p, patchLabel(p)]),
                                 ...(patch && patch !== data.crawl.current && !data.crawl.patches.includes(patch) ? [[patch, patchLabel(patch)]] : [])]} />
                <Field label="Or type a patch" hideLabel width="120px" placeholder="or type {patchLabel(data.crawl.current) ?? '26.21'}" mono
                       onchange={(e) => (patch = riotPatch(e.currentTarget.value))} />
              </ToolGroup>
              <ToolGroup label="Match ids per player"><Field label="Match ids per player" hideLabel type="number" min="1" max="100" width="72px" bind:value={idsPerQueue} /></ToolGroup>
              <ToolGroup><Field type="checkbox" label="Skip timelines (faster, no gold leads)" bind:checked={noTimelines} /></ToolGroup>
            {/if}
          </Toolbar>
          {#if takesTiers(crawlJob)}
            <div class="tierpick">
              {#each TIER_SETS.filter((s) => crawlJob === 'master' ? s.apex : crawlJob !== 'timelines' || !s.apex) as set}
                <div class="picks">
                  <button type="button" class="setname label-caps" onclick={() => toggleSet(set)} title="Pick or clear the whole set">{set.label}</button>
                  {#each set.tiers as t}<Tag onclick={() => toggleCrawlTier(t)} pressed={tiers.includes(t)}>{cap(t)}</Tag>{/each}
                </div>
              {/each}
            </div>
            <p class="cap">
              {#if apexPicked}Master, Grandmaster and Challenger crawl into the Master+ database (the Matchup tree and
                Strategy's Master+ builds), Solo/Duo only, never into Live's stats: this runs as the Master+ crawl.
              {:else if tiers.length}Crawling {tiers.map(cap).join(', ')}.
              {:else if crawlJob === 'master'}None picked: all three Master+ ladders.
              {:else}None picked: every tier up to Emerald, plus any set on the Roster.{/if}
            </p>
          {/if}
          <p class="cap">{jobs[crawlJob]?.about}. Live and scouting keep working meanwhile; they share the key's rate
            limits, so both run a little slower.</p>
        {/if}
        {#if message}<p class="warn" role="alert">⚠ {message}</p>{/if}
        {#if data.log.length}
          <h3 class="label-caps">{running ? 'Output' : `Last run: ${data.last?.label ?? ''} · ${data.last ? result(data.last) : ''}`}</h3>
          <pre bind:this={logBox} tabindex="0" aria-label="Job output">{data.log.join('\n')}</pre>
        {/if}
      </Panel>
    </section>

    <section>
      <div class="sechead">
        <h2>Matches crawled {#if data.crawl.patch}<span class="sub">patch {patchLabel(data.crawl.patch)}{data.database === 'master' ? ' · Master+' : ''}</span>{/if}</h2>
        <Field label="Patch shown" hideLabel width="180px" bind:value={viewPatch}
               options={[['', running && prog?.patch ? "The crawl's patch" : 'Current patch'], ...data.crawl.patches.map((p) => [p, patchLabel(p)])]} />
      </div>
      {#if data.groups}
        <Table sticky={false}>
          <thead><tr><th scope="col">Tier</th><th scope="col" class="num">This patch</th><th scope="col" class="barhead">Of {TARGET.toLocaleString()} wanted</th>
            <th scope="col" class="num">Every patch</th><th scope="col" class="num">With timeline</th></tr></thead>
          {#each data.groups as g}
            <tbody>
              <tr class="ghead"><th colspan="5" scope="colgroup">{g.label}</th></tr>
              {#each g.rows as b}
                <tr class:none={!b.all}>
                  <td>{b.bracket === 'master' ? 'Master+' : cap(b.bracket)}</td>
                  <td class="num mono">{b.patch.toLocaleString()}</td>
                  <!-- progress is not a call to act: the bar is grey, a little brighter for the patch being crawled -->
                  <td class="barcell"><span class="track" role="img" aria-label="{pct(b.patch, TARGET)}% of the matches wanted"><span class="fill" class:current={!shownPatch || shownPatch === data.crawl.current} style:width="{Math.min(100, pct(b.patch, TARGET))}%"></span></span>
                    <span class="mono share">{pct(b.patch, TARGET)}%</span></td>
                  <td class="num mono">{b.all.toLocaleString()}</td>
                  <td class="num mono">{b.all ? `${pct(b.timelines, b.all)}%` : '—'}</td>
                </tr>
              {/each}
              {#if g.rows.length > 1}
                <tr class="subtotal"><td>{g.label}</td><td class="num mono">{g.total_patch.toLocaleString()}</td><td></td>
                  <td class="num mono">{g.total.toLocaleString()}</td><td class="num mono">{g.total ? `${pct(g.timelines, g.total)}%` : '—'}</td></tr>
              {/if}
            </tbody>
          {/each}
          <tfoot><tr><td>Total</td><td class="num mono">{data.groups.reduce((a, g) => a + g.total_patch, 0).toLocaleString()}</td><td></td>
            <td class="num mono">{data.groups.reduce((a, g) => a + g.total, 0).toLocaleString()}</td><td></td></tr></tfoot>
        </Table>
        <p class="cap">Bars: this patch's matches against the {TARGET.toLocaleString()} per tier the coverage check wants;
          they move while a crawl of this patch stores matches. Master+ counts Master, Grandmaster and Challenger together.</p>
      {:else}
        <p class="quiet">Nothing crawled yet. Until the crawl has enough games, Live uses OP.GG's stats.</p>
      {/if}
    </section>
    {/if}

    <section>
      <h2>Other jobs</h2>
      <Table sticky={false} columns={JOB_COLUMNS} rows={others} key={(j) => j.id} empty="No other jobs for this account">
        {#snippet cell(j, col)}
          {#if col.key === 'label'}<strong>{j.label}</strong>
          {:else if col.key === 'about'}<span class="about">{j.about}{#if j.needs_key && !data.riot_key_set}<span class="quiet needs">· needs a Riot key</span>{/if}</span>
          {:else if col.key === 'last'}
            {#if lastRun[j.label]}<span class="mono">{when(lastRun[j.label].started)}</span> <span class:warn={failedRun(lastRun[j.label])} class:quiet={!failedRun(lastRun[j.label])}>· {failedRun(lastRun[j.label]) ? '⚠ ' : ''}{result(lastRun[j.label])}</span>
            {:else}<span class="quiet">not run yet</span>{/if}
          {:else}<Button onclick={() => start(j.id)} disabled={!!running || (j.needs_key && !data.riot_key_set)}
                         title={running ? 'One job at a time: wait for the running one' : null} aria-label="Run {j.label}">Run</Button>{/if}
        {/snippet}
      </Table>
    </section>

    {#if storage}
      <section>
        <div class="sechead"><h2>Storage <span class="sub"><span class="mono">{mbs(storage.total)}</span> in {storage.data_folder}</span></h2>
          <Button variant="ghost" onclick={loadStorage}>Refresh</Button></div>
        <Table sticky={false}>
          <thead><tr><th scope="col">What</th><th scope="col" class="num">Size</th><th scope="col"><span class="sr">Actions</span></th></tr></thead>
          <tbody>
            {#each storage.items.filter((i) => i.bytes > 0) as i}
              <tr>
                <td class="what"><strong>{i.label}</strong>{#if i.note}<div class="about">{i.note}</div>{/if}</td>
                <td class="num mono">{mbs(i.bytes)}</td>
                <td class="acts">
                  {#if i.action === 'clear'}<Button onclick={() => clearStorage(i.id)} disabled={!i.clearable}>Clear</Button>
                  {:else if i.action === 'pack' && jobs.pack}<Button onclick={() => start('pack')} disabled={!!running}>Pack cache</Button>
                  {:else if i.action === 'archive' && jobs.archive}<Button onclick={() => start('archive')} disabled={!!running}>Archive old patches</Button>
                  {:else if i.action === 'optimize' && i.id === 'database' && jobs.optimize}<Button onclick={() => start('optimize')} disabled={!!running}>Optimise databases</Button>{/if}
                </td>
              </tr>
            {:else}
              <tr><td colspan="3" class="quiet">Nothing stored yet</td></tr>
            {/each}
          </tbody>
        </Table>
        {#if storageMessage}<p class="line" role="status" use:pulse={{ value: storageMessage, ok: !/^(Not|No) |fail|error|couldn/i.test(storageMessage) }}>{storageMessage}</p>{/if}
        <p class="cap">The optimising runs as a job (one at a time, output above). The crawl's raw matches are never
          deleted: counts, fits and backfills are computed from them, and publishing copies only counts and fits.</p>
      </section>
    {/if}

    {#if data.history.length}
      <section>
        <h2>Recent runs</h2>
        <div class="narrow">
          <Table dense sticky={false} columns={[{ key: 'label', label: 'Job' }, { key: 'started', label: 'Started', mono: true, format: when, width: '180px' },
                                                { key: 'result', label: 'Result', width: '180px', value: result }]} rows={data.history}>
            {#snippet cell(h, col, i, text)}
              {#if col.key === 'result'}<span class:warn={failedRun(h)}>{failedRun(h) ? '⚠ ' : ''}{result(h)}</span>{:else}{text(h, col)}{/if}
            {/snippet}
          </Table>
        </div>
      </section>
    {/if}
  {/if}
</main>

<style>
  section { margin-bottom: 32px; min-width: 0; }
  h2 { margin: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  .sechead { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
  .sechead h2 { margin: 0; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; }
  h3 { margin: 20px 0 6px; }
  .quiet { color: var(--text-3); font-size: 13px; }
  p.quiet { margin: 0; }
  .line { margin: 0 0 4px; font-size: 13px; color: var(--text-2); }
  .cap { margin: 8px 0 0; font-size: 12px; line-height: 1.5; color: var(--text-3); max-width: 110ch; }
  .warn { color: var(--warn); font-size: 13px; }
  p.warn { margin: 0 0 12px; }
  .warn.inline { margin-left: 10px; font-size: 12px; }
  .warn a { color: var(--text-1); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .per1 { margin-right: 16px; white-space: nowrap; color: var(--text-2); }
  .picks { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; margin-bottom: 8px; }
  .picks .label-caps { margin-right: 4px; min-width: 132px; }
  .formrow { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 8px 12px; margin-top: 12px; }
  .acts { width: 1%; white-space: nowrap; text-align: right; }
  .invites { margin-top: 16px; padding-left: 12px; border-left: 1px solid var(--hairline-strong); display: grid; gap: 8px; }
  .invite { display: flex; gap: 12px; align-items: center; font-size: 13px; }
  .invite code { flex: 1; overflow-wrap: anywhere; font-size: 12px; color: var(--text-2); }

  .runline { margin: 0 0 12px; font-size: 14px; color: var(--text-2); }
  .runline strong { color: var(--text-1); }
  .runstats { display: flex; flex-wrap: wrap; gap: 12px 40px; }
  .per { display: flex; flex-wrap: wrap; gap: 4px 20px; margin: 12px 0 0; font-size: 12px; color: var(--text-3); }
  .per .mono { color: var(--text-2); }
  .tierpick { display: grid; gap: 0; margin-top: 4px; }
  .setname { background: none; border: 0; padding: 0; min-width: 132px; text-align: left; cursor: pointer; }
  .setname:hover { color: var(--text-1); filter: none; }
  pre { background: var(--bg); border: 1px solid var(--hairline); border-radius: var(--radius-sm); padding: 10px 12px; margin: 0;
        max-height: 280px; overflow: auto; font: 12px/1.5 var(--font-mono); color: var(--text-2); white-space: pre-wrap; word-break: break-word; }

  section :global(tr.ghead th) { height: 32px; vertical-align: bottom; padding-bottom: 6px; background: none; box-shadow: none; }
  section :global(tr.none td) { color: var(--text-3); }
  section :global(tr.subtotal td) { color: var(--text-3); }
  section :global(tfoot td) { font-weight: 600; border-bottom: 0; }
  section :global(th.barhead), .barcell { width: 34%; }
  .barcell { white-space: nowrap; }
  .track { display: inline-block; vertical-align: middle; width: calc(100% - 52px); height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
  .fill { display: block; height: 100%; background: var(--text-3); transition: width 300ms linear; }
  .fill.current { background: var(--text-2); }
  .share { display: inline-block; width: 44px; text-align: right; font-size: 12px; color: var(--text-3); }
  .needs { margin-left: 6px; }
  .about { font-size: 12.5px; color: var(--text-2); white-space: normal; }
  div.about { margin-top: 2px; color: var(--text-3); }
  .what { padding-top: 6px !important; padding-bottom: 6px !important; }
  .narrow { max-width: 760px; }
</style>
