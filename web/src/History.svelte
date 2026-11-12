<script>
  import { reveal, rise } from './motion.js';
  import { track } from './loading.js';
  import Champ from './Champ.svelte';
  import ExecutionStrip from './ExecutionStrip.svelte';
  import { patchLabel } from './patch.js';
  import Timeline from './Timeline.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';
  import Table from './ui/Table.svelte';
  import Tag from './ui/Tag.svelte';
  import Stat from './ui/Stat.svelte';

  // Favourability colours: teal = we're favoured, amber = they are, grey = within ±1pp. Validated
  // (colour-blind safe, distinct from the side blue/red) on the panel surface; the signed number is
  // always shown as well.
  // the charts (ECharts) load on their own, after the list: the rest of the app doesn't wait for them
  const chartsModule = track(import('./HistoryCharts.svelte')); // the page waits for the charts' code (loading.js)
  let games = $state(null);
  let filters = $state({ queue: [], patch: [], side: [], result: [] });
  let chosen = $state({ queue: '', side: '', result: '', patch: '' });
  let selected = $state(null);
  let recap = $state(null);
  let showTable = $state(false);
  let check = $state(null);
  fetch('/api/model-check').then((r) => r.json()).then((d) => (check = d));
  const signedPp = (x) => `${x > 0 ? '+' : x < 0 ? '\u2212' : '±'}${Math.abs(x).toFixed(1)}pp`;

  async function loadList() {
    const q = new URLSearchParams(Object.entries(chosen).filter(([, v]) => v));
    const body = await (await fetch(`/api/history?${q}`)).json();
    games = body.games;
    filters = body.filters;
    if (games.length && !games.some((g) => g.draft_id === selected)) open(games[0].draft_id);
  }

  async function open(id) {
    selected = id;
    showTable = false;
    recap = await (await fetch(`/api/history/${id}`)).json();
  }

  loadList();

  // Import missed games (docs/import-games.md): the Crawl page's job, started from here
  let importing = $state(false);
  let importNote = $state('');

  async function importMissed() {
    importNote = '';
    const res = await fetch('/api/jobs/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job: 'import' }),
    });
    const body = await res.json();
    if (!res.ok) { importNote = String(body.detail); return; }
    importing = true;
    const poll = setInterval(async () => {
      const jobs = await (await fetch('/api/jobs')).json();
      if (jobs.running) return;
      clearInterval(poll);
      importing = false;
      const lines = (jobs.log ?? []).filter((l) => l.trim());
      importNote = lines.slice(-2).join(' ') || 'Done.';
      await loadList();
    }, 2000);
  }

  const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const day = (iso) => new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short' });
  const pct = (p) => (p == null ? '—' : `${Math.round(p * 100)}%`);
  const length = (s) => (s == null ? '' : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);
  const signed = (x) => `${x > 0 ? '+' : x < 0 ? '−' : '±'}${Math.abs(x).toFixed(1)}pp`;
  const RESULT = { win: 'Win', loss: 'Loss', remake: 'Remake' };
  const tone = (x) => (x > 0 ? 'up' : x < 0 ? 'down' : null);
  const cap = (w) => (w ? w[0].toUpperCase() + w.slice(1) : '');
  const FILTERS = [['queue', 'Queue'], ['side', 'Side'], ['result', 'Result'], ['patch', 'Patch']];
  const filtered = $derived(Object.values(chosen).some(Boolean));
  const GAME_COLUMNS = [
    { key: 'played_at', label: 'Date', width: '72px', format: (v) => day(v) },
    { key: 'result', label: 'Result', width: '72px' },
    { key: 'ours', label: 'Us' }, { key: 'theirs', label: 'Them' },
    { key: 'queue', label: 'Queue' }, { key: 'side', label: 'Side', width: '56px', format: cap },
    { key: 'patch', label: 'Patch', width: '64px', mono: true, align: 'wideonly', format: (v) => patchLabel(v) ?? '—' },
    { key: 'pred_win_prob', label: 'P(win)', num: true, width: '72px', title: 'Our win probability at lock-in', value: (g) => (g.imported ? null : g.pred_win_prob), format: (v) => pct(v) },
    { key: 'verdict', label: 'Verdict' }, { key: 'notes', label: 'Notes', align: 'wideonly' },
  ];
  const STEP_COLUMNS = [
    { key: 'step', label: 'Step', num: true, width: '56px' }, { key: 'team', label: 'Team', value: (a) => (a.ours ? 'Us' : 'Them') },
    { key: 'side', label: 'Side', format: cap }, { key: 'type', label: 'Action', format: cap },
    { key: 'champ', label: 'Champion', value: (a) => a.champ?.name ?? 'skipped' },
    { key: 'rank', label: 'Our rank', num: true, format: (v) => `#${v}` },
    { key: 'win_prob', label: 'P(win) after', num: true, format: (v) => pct(v) },
  ];
  const MAX_PP = 10; // a full half-bar

  // Bar grows from the centre towards the favoured team's side.
  function bar(m, ourSide) {
    if (!m || m.delta_pp == null) return null;
    const favoured = m.delta_pp > 0 ? ourSide : ourSide === 'blue' ? 'red' : 'blue';
    const width = (Math.min(Math.abs(m.delta_pp), MAX_PP) / MAX_PP) * 50;
    return { width, toward: favoured === 'blue' ? 'left' : 'right', band: m.band };
  }
</script>

<main class="console">
  <PageHeader title="History" description="Every logged game: what the draft predicted, what decided it, and how closely we followed the suggestions.">
    {#snippet actions()}
      <Segmented label="Review pages" value="history" onchange={(v) => (location.hash = `#/${v}`)} options={[['history', 'History'], ['team', 'Team trends']]} />
      <Button onclick={importMissed} disabled={importing}
              title="Games of the last 60 days with 3 or more of our players together that History doesn't have, from Riot's match history">
        {importing ? 'Importing missed games…' : 'Import missed games'}</Button>
    {/snippet}
  </PageHeader>
  {#if importNote}<p class="importnote" role="status">{importNote}</p>{/if}

  {#await chartsModule then m}<m.default filters={{ ...chosen }} {check} />{/await}

  <Toolbar label="Game filters">
    {#each FILTERS as [key, label]}
      <ToolGroup {label}>
        <Field {label} hideLabel width="148px" bind:value={chosen[key]} onchange={loadList}
               options={[['', 'All'], ...filters[key].map((v) => [v, key === 'patch' ? patchLabel(v) : cap(v)])]} />
      </ToolGroup>
    {/each}
    {#if filtered}<ToolGroup end><Button variant="ghost" onclick={() => { chosen = { queue: '', side: '', result: '', patch: '' }; loadList(); }}>Clear filters</Button></ToolGroup>{/if}
  </Toolbar>

  <div class="games">
  <Table columns={GAME_COLUMNS} rows={games ?? []} key={(g) => g.draft_id} loading={games === null} maxHeight="372px"
         empty={filtered ? 'No games match these filters' : 'No games logged yet. Drafts are logged automatically once a game starts.'}
         selected={(g) => g.draft_id === selected} onrowclick={(g) => open(g.draft_id)}>
    {#snippet cell(g, col, i, text)}
      {#if col.key === 'result'}<span class="res {g.result ?? 'unknown'}">{RESULT[g.result] ?? 'Unknown'}</span>
      {:else if col.key === 'ours'}<span class="five">{#each g.our_champs as c}<Champ champ={c} size={22} />{/each}</span>
      {:else if col.key === 'theirs'}<span class="five">{#each g.their_champs as c}<Champ champ={c} size={22} />{/each}</span>
      {:else if col.key === 'pred_win_prob' && g.imported}<span class="quiet" title="Imported from Riot's match history: nothing was predicted">—</span>
      {:else if col.key === 'verdict'}<span class="verdictcell">{g.verdict ?? ''}</span>
      {:else if col.key === 'notes'}
        <span class="notes">
          {#if g.imported}<Tag bare>imported</Tag>{/if}
          {#if g.plan}<Tag bare title="Plan picks made: {g.plan.picked} of {g.plan.total}">plan {g.plan.followed === 'yes' ? 'followed' : g.plan.followed === 'partly' ? 'partly followed' : 'not followed'}</Tag>{/if}
          {#if g.scouting}<Tag bare title="Scouting: {g.scouting.found} of {g.scouting.players} players found{g.scouting.all_done_s != null ? `, all read in ${Math.round(g.scouting.all_done_s)} s` : ''}">scouted {g.scouting.done}/{g.scouting.players}</Tag>{/if}
        </span>
      {:else}{text(g, col)}{/if}
    {/snippet}
  </Table>
  </div>

  <section class="recap" aria-live="polite">
    {#if recap}
      {@const h = recap.header}
      {@const ours = h.side}
      <header class="head">
        <div>
          <h2>{RESULT[h.result] ?? 'Result unknown'}{#if h.game_length_s} <span class="mono len">{length(h.game_length_s)}</span>{/if}</h2>
          <p class="facts">{h.queue} · Patch {patchLabel(h.patch) ?? '—'} · {when(h.played_at)} · {ours} side{#if h.imported}
            · <span title="Imported from Riot's match history: the app didn't see this champ select, so nothing was suggested, and the pick order is the order players are listed in, which may differ from the order they locked in">imported, not watched live</span>{/if}</p>
        </div>
        {#if !h.imported}<Stat size="sm" value={h.pred_win_prob == null ? null : Math.round(h.pred_win_prob * 100)} unit="%" label="Predicted at lock-in" />{/if}
      </header>
      {#if recap.plan}
        <div class="planline"><span class="label-caps">Plan</span> <strong>{recap.plan.followed === 'yes' ? 'followed' : recap.plan.followed === 'partly' ? 'partly followed' : 'not followed'}</strong>
          <span class="mono">{recap.plan.picked} of {recap.plan.total}</span>
          {#each recap.plan.champs as c}<span class="pc" class:miss={!c.picked} title={c.picked ? 'Picked' : 'Not picked'}><Champ champ={c} size={20} faded={!c.picked} />{c.name}</span>{/each}</div>
      {/if}
      {#if recap.note}<p class="note">{recap.note}</p>{/if}
      {#if recap.execution}<ExecutionStrip e={recap.execution} />{/if}
      {#if recap.report}
        {@const r = recap.report}
        <section class="block">
          <h3>Draft report</h3>
          <p class="verdict"><strong>{r.verdict}</strong>{#if r.brier != null}<span class="quiet brier">· Brier <span class="mono">{r.brier.toFixed(3)}</span></span>{/if}</p>
          <div class="factors">
            {#each r.factors as f}
              <span class="factor"><b class="mono {tone(f.pp) ?? ''}">{signedPp(f.pp)}</b> {f.factor}</span>
            {/each}
          </div>
          <p class="cap">{r.note}</p>
          {#each r.deviations as d}
            <p class="dev">
              Step {d.step}: we picked {d.picked.name} ({d.rank ? `our #${d.rank}` : 'not on our list'}); our #1 was {d.suggested.name}:
              <b class="mono {tone(d.delta_pp) ?? ''}">{signedPp(d.delta_pp)}</b> by the model
            </p>
          {/each}
        </section>
      {/if}

      <section class="block">
        <h3>The draft</h3>
        <div class="bans">
          {#each ['blue', 'red'] as side}
            <div class="banside {side}">
              <span class="label-caps">{side === ours ? 'Our' : 'Their'} bans</span>
              {#each recap.bans as phase, pi}
                {#if pi > 0}<span class="divider" title="Phase 2"></span>{/if}
                {#each phase[side] as c}
                  {#if c}<Champ champ={c} size={32} banned />{:else}<span class="skip" title="Skipped">—</span>{/if}
                {/each}
              {/each}
            </div>
          {/each}
        </div>

        <!-- the side is each team column's outer edge (colour rule 2); the favourability bars sit in the middle, away from it -->
        <div class="board">
          <div class="colhead">{ours === 'blue' ? 'Us' : 'Them'} · blue</div>
          <div class="colhead mid">Lane matchups <span>pp for us</span></div>
          <div class="colhead right">{ours === 'red' ? 'Us' : 'Them'} · red</div>
          {#each recap.picks as p}
            {@const b = bar(p.matchup, ours)}
            {#if p.role === 'adc' && recap.bot_lane}
              {@const bl = bar(recap.bot_lane, ours)}
              <div class="pick blue thin"></div>
              <div class="matchup">
                <div class="role mono">Bot lane 2v2</div>
                {#if bl}
                  <div class="track" title="{signed(recap.bot_lane.delta_pp)} for {ours === 'blue' ? 'blue (us)' : 'red (us)'}{recap.bot_lane.lane_gold != null ? ` · ${signed(recap.bot_lane.lane_gold)} gold at 15` : ''}">
                    <span class="axis"></span>
                    <span class="fill {bl.band} {bl.toward}" style="width:{bl.width}%"></span>
                  </div>
                  <div class="value mono {bl.band}">{signed(recap.bot_lane.delta_pp)}</div>
                {/if}
              </div>
              <div class="pick red thin"></div>
            {/if}
            <div class="pick blue">
              {#if p.blue}
                <Champ champ={p.blue.champ} size={40} />
                <div class="who"><div class="name">{p.blue.champ.name}</div>{#if p.blue.player}<div class="player">{p.blue.player}</div>{/if}</div>
                <span class="order mono" title="Pick order">{p.blue.tag}</span>
              {/if}
            </div>
            <div class="matchup">
              <div class="role mono">{p.role_label}</div>
              {#if b}
                <div class="track" title="{signed(p.matchup.delta_pp)} for {ours === 'blue' ? 'blue (us)' : 'red (us)'} · {p.matchup.games} games{p.matchup.lane_gold != null ? ` · ${signed(p.matchup.lane_gold)} gold at 15, game ${signed(p.matchup.game_pp)}` : ''}">
                  <span class="axis"></span>
                  <span class="fill {b.band} {b.toward}" style="width:{b.width}%"></span>
                </div>
                <div class="value mono {b.band}">{signed(p.matchup.delta_pp)} <small>{p.matchup.band === 'ours' ? 'us' : p.matchup.band === 'theirs' ? 'them' : 'even'}</small></div>
              {:else}
                <div class="track"><span class="axis"></span></div>
                <div class="value nodata">no data</div>
              {/if}
            </div>
            <div class="pick red">
              {#if p.red}
                <span class="order mono" title="Pick order">{p.red.tag}</span>
                <div class="who"><div class="name">{p.red.champ.name}</div>{#if p.red.player}<div class="player">{p.red.player}</div>{/if}</div>
                <Champ champ={p.red.champ} size={40} />
              {/if}
            </div>
          {/each}
        </div>
      </section>

      <section class="block review">
        <div class="chart"><Timeline points={recap.timeline} swingStep={recap.review.biggest_swing?.step} /></div>
        <div class="facts2">
          <div>
            <h4 class="label-caps">Followed our suggestions?</h4>
            <div class="ranks">
              {#each recap.review.followed as f}
                <span class="rk" title="{f.type} · {f.champ?.name ?? 'skipped'}">
                  <Champ champ={f.champ} size={24} banned={f.type === 'ban'} />
                  <b class="mono" class:off={f.rank === 'off-list'}>{f.rank === 'off-list' ? 'off' : f.rank === '—' ? '—' : `#${f.rank}`}</b>
                </span>
              {/each}
            </div>
          </div>
          {#if recap.review.biggest_swing}
            {@const s = recap.review.biggest_swing}
            <div>
              <h4 class="label-caps">Biggest swing</h4>
              <p>Step {s.step} ({s.side} {s.type}{s.label !== 'ban' ? ' ' + s.label : ''}): {s.champ?.name ?? 'skipped'},
                <span class="mono {tone(s.delta) ?? ''}">{signed(s.delta * 100)}</span> to <span class="mono">{pct(s.win_prob)}</span></p>
            </div>
          {/if}
          <div>
            <h4 class="label-caps">Synergy</h4>
            <p>Us <span class="mono">{signed(recap.review.synergy.ours_pp)}</span> · Them <span class="mono">{signed(recap.review.synergy.theirs_pp)}</span>
              · net <span class="mono {tone(recap.review.synergy.net_pp) ?? ''}">{signed(recap.review.synergy.net_pp)}</span></p>
          </div>
        </div>
      </section>

      <div class="more"><Button variant="ghost" aria-expanded={showTable} onclick={() => (showTable = !showTable)}>{showTable ? 'Hide' : 'Show'} every draft step</Button></div>
      {#if showTable}
        <div transition:reveal><Table dense sticky={false} columns={STEP_COLUMNS} rows={recap.table} selected={(a) => a.ours} /></div>
      {/if}
    {:else if games?.length}
      <p class="quiet">Loading the game…</p>
    {/if}
  </section>
</main>

<style>
  .importnote { margin: -8px 0 12px; font-size: 13px; color: var(--text-2); }
  .quiet { color: var(--text-3); font-size: 13px; }
  .cap { margin: 0; font-size: 12px; color: var(--text-3); }
  /* one line per game; on narrow windows Patch and Notes give way (the recap has both) */
  .games :global(td) { white-space: nowrap; }
  @media (max-width: 1180px) { .games :global(.wideonly) { display: none; } }
  .five { display: inline-flex; gap: 2px; }
  .res { font-weight: 600; }
  .res.loss { color: var(--text-2); font-weight: 500; }
  .res.remake, .res.unknown { color: var(--text-3); font-weight: 400; }
  .verdictcell { color: var(--text-2); }
  .notes { display: inline-flex; gap: 12px; }

  .recap { margin-top: 28px; min-width: 0; }
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
  h2 { margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
  .len { font-size: 14px; font-weight: 500; color: var(--text-3); margin-left: 8px; }
  .facts { margin: 4px 0 0; font-size: 13px; color: var(--text-3); }
  .planline { display: flex; align-items: center; gap: 8px 12px; flex-wrap: wrap; font-size: 13px; margin-top: 12px; }
  .planline .mono { color: var(--text-3); }
  .pc { display: inline-flex; align-items: center; gap: 6px; }
  .pc.miss { color: var(--text-3); text-decoration: line-through; }
  .note { color: var(--text-2); font-size: 13px; margin: 8px 0 0; }

  .block { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--hairline); }
  h3 { margin: 0 0 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3); }
  h4 { margin: 0 0 6px; }
  .verdict { margin: 0 0 8px; font-size: 15px; }
  .brier { margin-left: 8px; }
  .factors { display: flex; flex-wrap: wrap; gap: 4px 20px; font-size: 13px; color: var(--text-2); margin-bottom: 6px; }
  .factor b, .dev b { font-weight: 600; color: var(--text-2); }
  .up { color: var(--up-text) !important; }
  .down { color: var(--down-text) !important; }
  .dev { margin: 6px 0 0; font-size: 13px; color: var(--text-2); }

  .bans { display: flex; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
  .banside { display: flex; align-items: center; gap: 6px; }
  .banside.red { flex-direction: row-reverse; }
  .banside .label-caps { margin: 0 6px 0 0; }
  .banside.red .label-caps { margin: 0 0 0 6px; }
  .divider { width: 1px; align-self: stretch; background: var(--hairline-strong); margin: 0 4px; }
  .skip { width: 32px; height: 32px; display: inline-grid; place-items: center; border: 1px dashed var(--hairline-strong); border-radius: var(--radius-sm); color: var(--text-3); }
  .board { display: grid; grid-template-columns: minmax(0, 1fr) minmax(200px, 320px) minmax(0, 1fr); column-gap: 24px; }
  .colhead { padding: 0 12px 6px; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3); border-bottom: 1px solid var(--hairline-strong); }
  .colhead.right { text-align: right; }
  .colhead.mid { text-align: center; }
  .colhead span { font-weight: 400; letter-spacing: 0; text-transform: none; }
  .pick { display: flex; align-items: center; gap: 12px; padding: 8px 12px; min-height: 56px; border-bottom: 1px solid var(--hairline); }
  .pick.blue { box-shadow: inset 3px 0 0 var(--side-blue); }
  .pick.red { box-shadow: inset -3px 0 0 var(--side-red); justify-content: flex-end; text-align: right; }
  .pick.thin { min-height: 0; }
  .name { font-size: 14px; font-weight: 600; }
  .player { font-size: 12px; color: var(--text-3); }
  .order { font-size: 11px; color: var(--text-3); }
  .pick.blue .order { margin-left: auto; }
  .pick.red .order { margin-right: auto; }
  .matchup { display: grid; align-content: center; gap: 3px; text-align: center; padding: 6px 0; border-bottom: 1px solid var(--hairline); }
  .role { font-size: 11px; text-transform: uppercase; color: var(--text-3); }
  .track { position: relative; height: 6px; background: var(--surface-2); border-radius: 2px; }
  .axis { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--text-3); }
  .fill { position: absolute; top: 0; bottom: 0; }
  .fill.left { right: 50%; border-radius: 2px 0 0 2px; }
  .fill.right { left: 50%; border-radius: 0 2px 2px 0; }
  .fill.ours { background: var(--up); }
  .fill.theirs { background: var(--down); }
  .fill.even { background: var(--text-3); }
  .value { font-size: 12.5px; font-weight: 600; color: var(--text-2); }
  .value.ours { color: var(--up-text); }
  .value.theirs { color: var(--down-text); }
  .value small { font-family: var(--font-ui); font-weight: 400; color: var(--text-3); }
  .value.nodata { color: var(--text-3); font-weight: 400; font-size: 12px; }

  .review { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 32px; }
  .facts2 { display: grid; gap: 16px; align-content: start; }
  .facts2 p { margin: 0; font-size: 13px; color: var(--text-2); }
  .ranks { display: flex; flex-wrap: wrap; gap: 8px; }
  .rk { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; font-size: 11px; color: var(--text-2); }
  .rk b { font-weight: 500; }
  .rk b.off { color: var(--warn); }
  .more { margin-top: 16px; }
  @media (max-width: 1180px) {
    .review { grid-template-columns: 1fr; }
    .board { grid-template-columns: minmax(0, 1fr) minmax(140px, 200px) minmax(0, 1fr); column-gap: 12px; }
    .bans { flex-direction: column; gap: 8px; }
    .banside.red { flex-direction: row; }
    .banside.red .label-caps { margin: 0 6px 0 0; }
  }
</style>
