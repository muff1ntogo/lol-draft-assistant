<script>
  import { pulse } from './motion.js';
  // The crawl and the other data jobs, from the app (src/draftassist/jobs.py): one at a time, each its own process,
  // so a long crawl keeps going if the app closes, and the app finds it again when reopened.
  import { patchLabel, riotPatch } from './patch.js';

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

<main class="page">
  <h1>Crawl</h1>
  {#if !data}
    <p class="muted">Loading…</p>
  {:else}
    {#if data.account}<p class="muted acct">{data.account.label}{#if data.account.warning} · <span class="warnline">{data.account.warning}</span>{/if}
      · <a href="#/settings">Account</a></p>{/if}
    {#if !data.riot_key_set && role !== 'user'}
      <p class="warn">The crawl needs your Riot API key: add it in <a href="#/settings">Settings</a>.</p>
    {/if}

    {#if shared?.configured}
      <section class="shared">
        <h2>Shared data</h2>
        <p>
          {#if shared.pull?.published_at}Stats from {when(shared.pull.published_at)} (publish {shared.pull.publish_id}){:else}Nothing pulled yet{/if}
          {#if shared.pull?.pulled_at}<span class="muted"> · pulled {when(shared.pull.pulled_at)}</span>{/if}
          {#if shared.last}<span class="muted"> · last sync: {SYNC_STATES[shared.last.state] ?? shared.last.state}</span>{/if}
          <button onclick={syncNow} disabled={syncing}>{syncing ? 'Syncing…' : 'Sync now'}</button>
        </p>
        {#if shared.last?.message && !['updated', 'unchanged'].includes(shared.last.state)}<p class="muted small">{shared.last.message}</p>{/if}
        {#if role === 'admin' && data.quota}
          <p class="muted">Rows written to the shared database this month: {data.quota.rows.toLocaleString()} of
            {data.quota.limit.toLocaleString()} ({pct(data.quota.rows, data.quota.limit)}%). Publish warns from 70% and stops at 95%.</p>
        {/if}
      </section>
    {/if}

    {#if role === 'user'}
      <section>
        <h2>What's published</h2>
        {#if data.published?.patches?.length}
          <table>
            <thead><tr><th>Patch</th><th>Matches per tier</th><th class="n">Total</th></tr></thead>
            <tbody>
              {#each data.published.patches as p}
                <tr><td>{patchLabel(p.patch)}</td>
                  <td>{#each Object.entries(p.brackets) as [b, n]}<span class="per1"><span class="tier">{b}</span> {n.toLocaleString()}</span>{/each}</td>
                  <td class="n">{p.total.toLocaleString()}</td></tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="muted">Nothing pulled yet: Sync now, above. Until then Live uses OP.GG's stats.</p>
        {/if}
        <p class="muted">The team's admin crawls and publishes; this machine pulls. Your own games, History and scouting stay on this machine.</p>
      </section>
    {/if}

    {#if role === 'user' && ds && sel}
      <section>
        <h2>Data selection <small>{ds.fits_from}</small></h2>
        <div class="form">
          <label>Patches
            <select bind:value={sel.scope}>
              {#each Object.entries(SCOPE_LABELS) as [k, label]}<option value={k}>{label}</option>{/each}
            </select>
          </label>
          {#if sel.scope === 'last_n'}<label>N <input type="number" min="1" max="20" bind:value={sel.n} /></label>{/if}
          {#if ['last_n', 'patches', 'all'].includes(sel.scope)}
            <label>Half-life <input type="number" min="0" max="20" step="0.5" bind:value={sel.half_life} /> patches (0 = equal)</label>
          {/if}
        </div>
        {#if sel.scope === 'patches'}
          <div class="form opts">{#each ds.patches as p}
            <label class="cb"><input type="checkbox" value={p} bind:group={sel.patches} /> {patchLabel(p)}</label>{/each}</div>
        {/if}
        {#if sel.scope !== 'default'}
          <div class="form opts">
            <span class="muted lbl">Queues</span>
            <label class="cb"><input type="checkbox" checked={sel.queues.includes(420)} onchange={() => toggleQueue(420)} /> Solo/Duo</label>
            <label class="cb"><input type="checkbox" checked={sel.queues.includes(440)} onchange={() => toggleQueue(440)} /> Flex</label>
          </div>
          {#each ds.player_brackets as b}
            <div class="form opts tiers">
              <span class="muted lbl">Tiers for <span class="tier">{b}</span> players</span>
              {#each ds.tiers as t}<label class="cb cap"><input type="checkbox" checked={tiersFor(b).includes(t)} onchange={() => toggleTier(b, t)} /> {t}</label>{/each}
            </div>
          {/each}
        {/if}
        <div class="form opts">
          <label class="cb"><input type="checkbox" bind:checked={sel.master_tree} /> Master+ games for the Matchup tree</label>
          <button class="primary" onclick={saveSelection}>Apply</button>
        </div>
        {#if selMessage}<p class="muted" use:pulse={{ value: selMessage, ok: !/^(Not|No) |fail|error|couldn/i.test(selMessage) }}>{selMessage}</p>{/if}
        <p class="muted">Win rates, matchups and synergy are summed from the pulled counts for exactly this choice. Fits (matchup
          model, synergy rules, traits) come from the admin's dataset closest to it, named above. Changes apply between drafts.</p>
      </section>
    {/if}

    {#if role === 'admin' && ds}
      <section>
        <h2>Datasets</h2>
        <table>
          <thead><tr><th>Id</th><th>Name</th><th>Patches</th></tr></thead>
          <tbody>{#each ds.datasets as d}<tr><td>{d.dataset_id}</td><td>{d.name}</td><td>{d.description}{d.source === 'master_plus' ? ' · Master+ database' : ''}</td></tr>{/each}</tbody>
        </table>
        <form class="form" onsubmit={(e) => { e.preventDefault(); addDataset(); }}>
          <label>Id <input bind:value={newDs.dataset_id} placeholder="last3" /></label>
          <label>Name <input bind:value={newDs.name} placeholder="Last 3 patches" /></label>
          <label>Patches <select bind:value={newDs.scope}><option value="current">current</option><option value="last_n">last N</option><option value="all">every</option></select></label>
          {#if newDs.scope === 'last_n'}<label>N <input type="number" min="1" max="20" bind:value={newDs.n} /></label>{/if}
          <button type="submit" disabled={!newDs.dataset_id.trim() || !newDs.name.trim()}>Add</button>
        </form>
        <p class="muted">Each dataset gets its own fits, computed and published with the next Publish. Teammates get the one
          closest to their selection.</p>
        {#if selMessage}<p class="warn">{selMessage}</p>{/if}
      </section>
    {/if}

    {#if role === 'admin'}
      <section>
        <h2>Members</h2>
        {#if members === null}<p class="muted">Loading…</p>{:else}
          {#if members.length}
            <table>
              <thead><tr><th>Name</th><th>Expires</th><th></th></tr></thead>
              <tbody>
                {#each members as m}
                  <tr><td>{m.name}</td>
                    <td>{m.expires_at ? when(m.expires_at) : 'never'}{#if m.expiring} <span class="warnline">expiring soon</span>{/if}</td>
                    <td class="n"><button onclick={() => renewMember(m.name)}>Renew</button>
                      <button onclick={() => removeMember(m.name)}>{confirmRemove === m.name ? 'Confirm: new codes for everyone' : 'Remove'}</button></td></tr>
                {/each}
              </tbody>
            </table>
          {:else}<p class="muted">No members yet.</p>{/if}
          <form class="form" onsubmit={(e) => { e.preventDefault(); addMember(); }}>
            <label>Display name <input bind:value={newMember} placeholder="no Riot ID needed" /></label>
            <button type="submit" disabled={!newMember.trim()}>Create invite</button>
          </form>
          {#if shownInvites}
            <div class="invites">
              <p><strong>Send these privately. They're shown only now.</strong></p>
              {#each Object.entries(shownInvites) as [name, code]}
                <div class="invite"><span>{name}</span><code>{code}</code><button onclick={() => copy(code)}>Copy</button></div>
              {/each}
              <button onclick={() => (shownInvites = null)}>Done</button>
            </div>
          {/if}
          <p class="muted">Turso can only invalidate every token at once, so removing a member rotates the database's keys and
            gives everyone else a new code to send. Renew re-issues one member's code (the old one works until it expires).
            Invites last 90 days; you and the member are warned 14 days ahead.</p>
        {/if}
        {#if memberMessage}<p class="warn">{memberMessage}</p>{/if}
      </section>
    {/if}

    {#if role !== 'user'}
    <section>
      <h2>Crawl ranked matches</h2>
      {#if running}
        <div class="running">
          <span class="dot"></span>
          <strong>{running.label}</strong> running for {since(running.started)}
          <button class="stop" onclick={stop}>Stop</button>
        </div>
        {#if prog}
          <div class="run">
            <div><span class="k">Now</span> {PHASES[prog.phase] ?? prog.phase}{prog.database === 'master' ? ' · Master+ database' : ''}{prog.patch ? ` · patch ${patchLabel(prog.patch)}` : ''}</div>
            <div><span class="k">This run</span> <strong>{runStored.toLocaleString()}</strong> matches{perHour ? ` · ~${perHour.toLocaleString()}/h` : ''}{prog.players ? ` · ${prog.players} players read` : ''}
              {#if prog.skipped_other_patch} · {prog.skipped_other_patch} from other patches skipped{/if}</div>
            {#if prog.timelines_total}
              <div><span class="k">Timelines</span> {prog.timelines_done.toLocaleString()} of {prog.timelines_total.toLocaleString()}
                <span class="track small"><span class="fill" style="width:{pct(prog.timelines_done, prog.timelines_total)}%"></span></span></div>
            {/if}
            {#if prog.stored && Object.keys(prog.stored).length}
              <div class="per">{#each Object.entries(prog.stored) as [b, n]}<span><span class="tier">{b}</span> +{n}</span>{/each}</div>
            {/if}
            <div class="muted small">Updated {prog.updated ? since(prog.updated) : '—'} ago</div>
          </div>
        {/if}
        <p class="muted">It keeps going if you close the app, and picks up where it left off if stopped. The numbers here
          update live; Live's stats refresh every 15 minutes and when it ends.</p>
      {:else}
        <div class="form">
          <label>What
            <select bind:value={crawlJob}>
              {#each CRAWLS as id}{#if jobs[id]}<option value={id}>{jobs[id].label}</option>{/if}{/each}
            </select>
          </label>
          {#if jobs[crawlJob]?.hours}
            <label>For <input type="number" min="0.5" max="24" step="0.5" bind:value={hours} /> hours</label>
          {/if}
          <button class="primary" onclick={() => start(crawlJob)} disabled={!data.riot_key_set}>Start</button>
        </div>
        {#if takesOptions(crawlJob)}
          <div class="form opts">
            <label>Patch
              <select bind:value={patch}>
                <option value="">current ({patchLabel(data.crawl.current) ?? '—'})</option>
                {#each data.crawl.patches.filter((p) => p !== data.crawl.current) as p}<option value={p}>{patchLabel(p)}</option>{/each}
              </select>
              <input class="patchin" placeholder="or type {patchLabel(data.crawl.current) ?? '26.21'}" onchange={(e) => (patch = riotPatch(e.currentTarget.value))} />
            </label>
            <label>Match ids per player <input type="number" min="1" max="100" bind:value={idsPerQueue} /></label>
            <label class="cb"><input type="checkbox" bind:checked={noTimelines} /> Skip timelines (faster, no gold leads)</label>
          </div>
        {/if}
        {#if takesTiers(crawlJob)}
          <div class="tierpick">
            {#each TIER_SETS.filter((s) => crawlJob === 'master' ? s.apex : crawlJob !== 'timelines' || !s.apex) as set}
              <div class="tset">
                <button type="button" class="tsetname" onclick={() => toggleSet(set)}
                        title="Pick or clear the whole set">{set.label}</button>
                <div class="chips">
                  {#each set.tiers as t}
                    <button type="button" class="chip" class:on={tiers.includes(t)} aria-pressed={tiers.includes(t)}
                            onclick={() => toggleCrawlTier(t)}>{t}</button>
                  {/each}
                </div>
              </div>
            {/each}
          </div>
          <p class="muted small">
            {#if apexPicked}Master, Grandmaster and Challenger crawl into the Master+ database (the Matchup tree and
              Strategy's Master+ builds), Solo/Duo only, never into Live's stats: this runs as the Master+ crawl.
            {:else if tiers.length}Crawling {tiers.join(', ')}.
            {:else if crawlJob === 'master'}None picked: all three Master+ ladders.
            {:else}None picked: every tier up to Emerald, plus any set on the Roster.{/if}
          </p>
        {/if}
        <p class="muted">{jobs[crawlJob]?.about}. Live and scouting keep working meanwhile; they share the key's rate
          limits, so both run a little slower.</p>
      {/if}
      {#if message}<p class="warn">{message}</p>{/if}
      {#if data.log.length}
        <h3>{running ? 'Output' : `Last run: ${data.last?.label} · ${data.last ? result(data.last) : ''}`}</h3>
        <pre bind:this={logBox}>{data.log.join('\n')}</pre>
      {/if}
    </section>

    <section>
      <h2>Matches crawled {#if data.crawl.patch}<small>patch {patchLabel(data.crawl.patch)}{data.database === 'master' ? ' · Master+' : ''}</small>{/if}
        <span class="view">
          <select bind:value={viewPatch} aria-label="Patch shown">
            <option value="">{running && prog?.patch ? 'the crawl\'s patch' : 'current patch'}</option>
            {#each data.crawl.patches as p}<option value={p}>{patchLabel(p)}</option>{/each}
          </select>
        </span></h2>
      {#if data.groups}
        <table>
          <thead><tr><th>Tier</th><th class="n">This patch</th><th class="bar">of {TARGET.toLocaleString()} wanted</th>
            <th class="n">Every patch</th><th class="n">With timeline</th></tr></thead>
          {#each data.groups as g}
            <tbody class="grp">
              <tr class="ghead"><td colspan="5">{g.label}</td></tr>
              {#each g.rows as b}
                <tr class:none={!b.all}>
                  <td class="tier">{b.bracket === 'master' ? 'Master+' : b.bracket}</td>
                  <td class="n">{b.patch.toLocaleString()}</td>
                  <td class="bar"><span class="track"><span class="fill" style="width:{Math.min(100, pct(b.patch, TARGET))}%"></span></span>
                    <small>{pct(b.patch, TARGET)}%</small></td>
                  <td class="n">{b.all.toLocaleString()}</td>
                  <td class="n">{b.all ? `${pct(b.timelines, b.all)}%` : '—'}</td>
                </tr>
              {/each}
              {#if g.rows.length > 1}
                <tr class="sub"><td>{g.label}</td><td class="n">{g.total_patch.toLocaleString()}</td><td></td>
                  <td class="n">{g.total.toLocaleString()}</td><td class="n">{g.total ? `${pct(g.timelines, g.total)}%` : '—'}</td></tr>
              {/if}
            </tbody>
          {/each}
          <tfoot><tr><td>Total</td><td class="n">{data.groups.reduce((a, g) => a + g.total_patch, 0).toLocaleString()}</td><td></td>
            <td class="n">{data.groups.reduce((a, g) => a + g.total, 0).toLocaleString()}</td><td></td></tr></tfoot>
        </table>
        <p class="muted small">Bars: this patch's matches against the {TARGET.toLocaleString()} per tier the coverage check wants;
          they move while a crawl of this patch stores matches. Master+ counts Master, Grandmaster and Challenger together.</p>
      {:else}
        <p class="muted">Nothing crawled yet. Until the crawl has enough games, Live uses OP.GG's stats.</p>
      {/if}
    </section>
    {/if}

    <section>
      <h2>Other jobs</h2>
      <div class="jobs">
        {#each others as j}
          <div class="job">
            <div><strong>{j.label}</strong><p class="muted">{j.about}</p></div>
            <button onclick={() => start(j.id)} disabled={!!running || (j.needs_key && !data.riot_key_set)}>Run</button>
          </div>
        {/each}
      </div>
    </section>

    {#if storage}
      <section>
        <h2>Storage <small>{mbs(storage.total)} in {storage.data_folder}</small>
          <span class="view"><button onclick={loadStorage}>Refresh</button></span></h2>
        <table>
          <tbody>
            {#each storage.items.filter((i) => i.bytes > 0) as i}
              <tr>
                <td>{i.label}{#if i.note}<div class="muted small">{i.note}</div>{/if}</td>
                <td class="n">{mbs(i.bytes)}</td>
                <td class="n">
                  {#if i.action === 'clear'}<button onclick={() => clearStorage(i.id)} disabled={!i.clearable}>Clear</button>
                  {:else if i.action === 'pack' && jobs.pack}<button onclick={() => start('pack')} disabled={!!running}>Pack cache</button>
                  {:else if i.action === 'archive' && jobs.archive}<button onclick={() => start('archive')} disabled={!!running}>Archive old patches</button>
                  {:else if i.action === 'optimize' && i.id === 'database' && jobs.optimize}<button onclick={() => start('optimize')} disabled={!!running}>Optimise databases</button>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if storageMessage}<p class="muted" use:pulse={{ value: storageMessage, ok: !/^(Not|No) |fail|error|couldn/i.test(storageMessage) }}>{storageMessage}</p>{/if}
        <p class="muted">The optimising runs as a job (one at a time, output above). The crawl's raw matches are never
          deleted: counts, fits and backfills are computed from them, and publishing copies only counts and fits.</p>
      </section>
    {/if}

    {#if data.history.length}
      <section>
        <h2>Recent runs</h2>
        <ul class="history">
          {#each data.history as h}
            <li><span>{h.label}</span><span class="muted">{when(h.started)} · {result(h)}</span></li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</main>

<style>
  .page { max-width: 1040px; margin: 0 auto; padding: 28px 32px 80px; }
  .acct { margin: -12px 0 18px; }
  .warnline { color: var(--gold); }
  .per1 { margin-right: 14px; white-space: nowrap; }
  .invites { margin-top: 16px; padding: 14px; border: 1px solid var(--gold); border-radius: 8px; }
  .invite { display: flex; gap: 10px; align-items: center; margin: 8px 0; }
  .invite code { flex: 1; overflow-wrap: anywhere; font-size: 12px; }
  h1 { font-size: 34px; margin: 4px 0 22px; }
  h2 { font-size: 21px; margin: 0 0 16px; display: flex; align-items: baseline; gap: 10px; }
  h2 small { font-size: 14px; color: var(--muted); font-weight: 400; }
  h3 { font-size: 14px; color: var(--muted); font-weight: 600; margin: 18px 0 8px; text-transform: uppercase; letter-spacing: 0.4px; }
  section { background: var(--panel); border: 1px solid var(--panel-2); border-radius: 10px; padding: 24px 28px; }
  section + section { margin-top: 22px; }
  .muted { color: var(--muted); font-size: 14px; line-height: 1.55; margin: 8px 0 0; max-width: 76ch; }
  .warn { color: var(--gold); margin: 0 0 16px; }
  .form { display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: center; }
  .form label { display: flex; align-items: center; gap: 10px; }
  select, input { height: 34px; background: var(--panel-2); color: var(--text); border: 1px solid var(--line); padding: 4px 10px; }
  input { width: 80px; }
  button { background: var(--blue-deep); color: var(--text); border: 0; padding: 8px 18px; height: 36px; }
  button.primary, button[type="submit"] { background: var(--gold); color: var(--on-gold); font-weight: 600; }
  button.primary:disabled, button[type="submit"]:disabled { background: var(--gold-bg); color: var(--muted); }
  button:disabled { opacity: 0.45; cursor: default; }
  button.stop { background: #8a3a3a; margin-left: auto; }
  .running { display: flex; align-items: center; gap: 10px; font-size: 17px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--up); box-shadow: 0 0 0 4px rgba(27, 175, 122, 0.2);
         animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 7px rgba(27, 175, 122, 0.05); } }
  pre { background: var(--nav); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; margin: 0;
        max-height: 320px; overflow: auto; font: 12.5px/1.5 "Cascadia Mono", Consolas, monospace; color: var(--text-2);
        white-space: pre-wrap; word-break: break-word; }
  table { width: 100%; border-collapse: collapse; font-size: 15px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 13px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 8px; border-bottom: 1px solid var(--panel-2); }
  tfoot td { font-weight: 600; border-bottom: 0; }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .tier { text-transform: capitalize; }
  .bar { width: 34%; }
  .bar small { color: var(--muted); margin-left: 8px; font-variant-numeric: tabular-nums; }
  .track { display: inline-block; vertical-align: middle; width: calc(100% - 52px); height: 8px; background: var(--panel-2); border-radius: 4px; overflow: hidden; }
  .fill { display: block; height: 100%; background: linear-gradient(90deg, var(--side-blue), var(--blue)); border-radius: 4px; }
  .opts { margin-top: 14px; }
  .opts input[type='number'] { width: 70px; }
  .patchin { width: 140px !important; }
  .cb { gap: 6px !important; }
  .cap { text-transform: capitalize; }
  .cb input { width: 16px; height: 16px; }
  .lbl { margin: 0; }
  .tiers { gap: 8px 16px; }
  .tierpick { display: flex; flex-wrap: wrap; gap: 14px 28px; margin-top: 14px; }
  .tset { display: flex; flex-direction: column; gap: 6px; }
  .tsetname { background: none; border: 0; padding: 0; color: var(--muted); font-size: 13px; text-align: left; cursor: pointer; }
  .tsetname:hover { color: var(--text); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { text-transform: capitalize; padding: 5px 12px; border-radius: 999px; border: 1px solid var(--line, var(--line));
          background: var(--panel-2); color: var(--muted); font-size: 14px; cursor: pointer; }
  .chip.on { border-color: var(--accent, var(--blue)); color: var(--text); background: color-mix(in srgb, var(--accent, var(--blue)) 22%, transparent); }
  .grp .ghead td { padding-top: 14px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.4px; border-bottom: 0; }
  .grp .sub td { color: var(--muted); font-size: 13px; }
  tr.none td { color: var(--muted); }
  .run { margin: 14px 0 4px; padding: 12px 14px; background: var(--panel-2); border-radius: 8px; display: grid; gap: 6px; font-size: 15px; }
  .run .k { display: inline-block; width: 86px; color: var(--muted); }
  .run .per { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 14px; color: var(--muted); }
  .track.small { width: 180px; margin-left: 10px; }
  .small { font-size: 12px; }
  .view { margin-left: auto; display: flex; gap: 8px; }
  .view select { height: 30px; font-size: 13px; }
  .jobs { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
  .job { display: flex; justify-content: space-between; align-items: center; gap: 14px; background: var(--panel-2);
         border-radius: 8px; padding: 12px 14px; }
  .job p { margin: 4px 0 0; font-size: 13px; }
  .history { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
  .history li { display: flex; justify-content: space-between; gap: 12px; font-size: 15px; }
</style>
