<script>
  // The crawl and the other data jobs, from the app (src/draftassist/jobs.py): one at a time, each its own process,
  // so a long crawl keeps going if the app closes, and the app finds it again when reopened.
  import { patchLabel } from './patch.js';

  const CRAWLS = ['crawl', 'timelines', 'master', 'refetch'];
  const TARGET = 20000; // matches per tier the spec's coverage check wants (Phase 3)
  let data = $state(null);
  let crawlJob = $state('crawl');
  let hours = $state(8);
  let message = $state('');
  let now = $state(Date.now() / 1000);
  let logBox = $state(null);

  async function load() {
    try {
      data = await (await fetch('/api/jobs')).json();
    } catch { /* the app is restarting */ }
  }
  load();
  $effect(() => {
    const every = data?.running ? 3000 : 15000;
    const t = setInterval(load, every);
    return () => clearInterval(t);
  });
  $effect(() => {
    const t = setInterval(() => (now = Date.now() / 1000), 1000);
    return () => clearInterval(t);
  });
  $effect(() => { data?.log; if (logBox) logBox.scrollTop = logBox.scrollHeight; });

  const jobs = $derived(Object.fromEntries((data?.jobs ?? []).map((j) => [j.id, j])));
  const others = $derived((data?.jobs ?? []).filter((j) => !CRAWLS.includes(j.id)));
  const running = $derived(data?.running);

  async function post(url, body) {
    message = '';
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
    const out = await res.json();
    if (res.ok) data = out;
    else message = out.detail;
  }
  const start = (job) => post('/api/jobs/start', { job, hours: jobs[job]?.hours ? +hours : null });
  const stop = () => post('/api/jobs/stop');

  function since(t) {
    const s = Math.max(0, Math.round(now - t));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
  }
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  const result = (h) => (h.stopped ? 'stopped' : h.exit === 0 ? 'finished' : h.exit == null ? 'ended' : `failed (exit ${h.exit})`);
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
</script>

<main class="page">
  <h1>Crawl</h1>
  {#if !data}
    <p class="muted">Loading…</p>
  {:else}
    {#if !data.riot_key_set}
      <p class="warn">The crawl needs your Riot API key: add it in <a href="#/settings">Settings</a>.</p>
    {/if}

    <section>
      <h2>Crawl ranked matches</h2>
      {#if running}
        <div class="running">
          <span class="dot"></span>
          <strong>{running.label}</strong> running for {since(running.started)}
          <button class="stop" onclick={stop}>Stop</button>
        </div>
        <p class="muted">It keeps going if you close the app, and picks up where it left off if stopped. Stats refresh
          as it goes (every 15 minutes) and when it ends.</p>
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
          <button onclick={() => start(crawlJob)} disabled={!data.riot_key_set}>Start</button>
        </div>
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
      <h2>Matches crawled {#if data.crawl.patch}<small>patch {patchLabel(data.crawl.patch)}</small>{/if}</h2>
      {#if data.crawl.brackets.length}
        <table>
          <thead><tr><th>Tier</th><th class="n">This patch</th><th class="bar">of {TARGET.toLocaleString()} wanted</th>
            <th class="n">Every patch</th><th class="n">With timeline</th></tr></thead>
          <tbody>
            {#each data.crawl.brackets as b}
              <tr>
                <td class="tier">{b.bracket}</td>
                <td class="n">{b.patch.toLocaleString()}</td>
                <td class="bar"><span class="track"><span class="fill" style="width:{Math.min(100, pct(b.patch, TARGET))}%"></span></span>
                  <small>{pct(b.patch, TARGET)}%</small></td>
                <td class="n">{b.all.toLocaleString()}</td>
                <td class="n">{pct(b.timelines, b.all)}%</td>
              </tr>
            {/each}
          </tbody>
          <tfoot><tr><td>Total</td><td class="n">{data.crawl.total_patch.toLocaleString()}</td><td></td>
            <td class="n">{data.crawl.total.toLocaleString()}</td><td></td></tr></tfoot>
        </table>
      {:else}
        <p class="muted">Nothing crawled yet. Until the crawl has enough games, Live uses OP.GG's stats.</p>
      {/if}
    </section>

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
  h1 { font-size: 34px; margin: 4px 0 22px; }
  h2 { font-size: 21px; margin: 0 0 16px; display: flex; align-items: baseline; gap: 10px; }
  h2 small { font-size: 14px; color: var(--muted); font-weight: 400; }
  h3 { font-size: 14px; color: var(--muted); font-weight: 600; margin: 18px 0 8px; text-transform: uppercase; letter-spacing: 0.4px; }
  section { background: var(--panel); border: 1px solid #1d232c; border-radius: 10px; padding: 24px 28px; }
  section + section { margin-top: 22px; }
  .muted { color: var(--muted); font-size: 14px; line-height: 1.55; margin: 8px 0 0; max-width: 76ch; }
  .warn { color: var(--gold); margin: 0 0 16px; }
  .form { display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: center; }
  .form label { display: flex; align-items: center; gap: 10px; }
  select, input { height: 34px; background: var(--panel-2); color: var(--text); border: 1px solid var(--line); padding: 4px 10px; }
  input { width: 80px; }
  button { background: var(--blue); color: white; border: 0; padding: 8px 18px; height: 36px; }
  button:disabled { opacity: 0.45; cursor: default; }
  button.stop { background: #8a3a3a; margin-left: auto; }
  .running { display: flex; align-items: center; gap: 10px; font-size: 17px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #1baf7a; box-shadow: 0 0 0 4px rgba(27, 175, 122, 0.2);
         animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 7px rgba(27, 175, 122, 0.05); } }
  pre { background: #0b0f14; border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; margin: 0;
        max-height: 320px; overflow: auto; font: 12.5px/1.5 "Cascadia Mono", Consolas, monospace; color: #c9d1d9;
        white-space: pre-wrap; word-break: break-word; }
  table { width: 100%; border-collapse: collapse; font-size: 15px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 13px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 8px; border-bottom: 1px solid #1f2630; }
  tfoot td { font-weight: 600; border-bottom: 0; }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .tier { text-transform: capitalize; }
  .bar { width: 34%; }
  .bar small { color: var(--muted); margin-left: 8px; font-variant-numeric: tabular-nums; }
  .track { display: inline-block; vertical-align: middle; width: calc(100% - 52px); height: 8px; background: #1f2630; border-radius: 4px; overflow: hidden; }
  .fill { display: block; height: 100%; background: linear-gradient(90deg, #2f6fb7, #4c9aff); border-radius: 4px; }
  .jobs { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
  .job { display: flex; justify-content: space-between; align-items: center; gap: 14px; background: var(--panel-2);
         border-radius: 8px; padding: 12px 14px; }
  .job p { margin: 4px 0 0; font-size: 13px; }
  .history { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
  .history li { display: flex; justify-content: space-between; gap: 12px; font-size: 15px; }
</style>
