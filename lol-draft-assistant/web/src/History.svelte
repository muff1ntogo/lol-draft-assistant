<script>
  import Champ from './Champ.svelte';
  import Timeline from './Timeline.svelte';

  // Favourability colours: teal = we're favoured, amber = they are, grey = within ±1pp. Validated
  // (colour-blind safe, distinct from the side blue/red) on the panel surface; the signed number is
  // always shown as well.
  let games = $state(null);
  let filters = $state({ queue: [], patch: [], side: [], result: [] });
  let chosen = $state({ queue: '', side: '', result: '', patch: '' });
  let selected = $state(null);
  let recap = $state(null);
  let showTable = $state(false);

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

  const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const day = (iso) => new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short' });
  const pct = (p) => (p == null ? '—' : `${Math.round(p * 100)}%`);
  const length = (s) => (s == null ? '' : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);
  const signed = (x) => `${x > 0 ? '+' : x < 0 ? '−' : '±'}${Math.abs(x).toFixed(1)}pp`;
  const RESULT = { win: 'WIN', loss: 'LOSS', remake: 'REMAKE' };
  const BIG = { win: 'VICTORY', loss: 'DEFEAT', remake: 'REMAKE' };
  const MAX_PP = 10; // a full half-bar

  // Bar grows from the centre towards the favoured team's side.
  function bar(m, ourSide) {
    if (!m || m.delta_pp == null) return null;
    const favoured = m.delta_pp > 0 ? ourSide : ourSide === 'blue' ? 'red' : 'blue';
    const width = (Math.min(Math.abs(m.delta_pp), MAX_PP) / MAX_PP) * 50;
    return { width, toward: favoured === 'blue' ? 'left' : 'right', band: m.band };
  }
</script>

<main class="history">
  <aside class="list">
    <div class="filters">
      {#each [['queue', 'Queue'], ['side', 'Side'], ['result', 'Result'], ['patch', 'Patch']] as [key, label]}
        <select bind:value={chosen[key]} onchange={loadList} aria-label={label}>
          <option value="">{label}: all</option>
          {#each filters[key] as v}<option value={v}>{v}</option>{/each}
        </select>
      {/each}
    </div>
    {#if games === null}
      <p class="muted">Loading…</p>
    {:else if games.length === 0}
      <p class="muted">No games logged yet. Drafts are logged automatically once a game starts.</p>
    {:else}
      {#each games as g}
        <button class="row" class:active={g.draft_id === selected} onclick={() => open(g.draft_id)}>
          <span class="chip {g.result ?? 'unknown'}">{RESULT[g.result] ?? '?'}</span>
          <span class="teams">
            <span class="five">{#each g.our_champs as c}<Champ champ={c} size={26} />{/each}</span>
            <span class="vs">vs</span>
            <span class="five">{#each g.their_champs as c}<Champ champ={c} size={26} />{/each}</span>
          </span>
          <span class="meta">
            <span>{pct(g.pred_win_prob)} predicted</span>
            <span class="muted">{g.queue} · <span class="side {g.side}">{g.side}</span> · {g.patch ?? '—'} · {day(g.played_at)}</span>
          </span>
        </button>
      {/each}
    {/if}
  </aside>

  <section class="recap">
    {#if recap}
      {@const h = recap.header}
      {@const ours = h.side}
      <header class="head {h.result}">
        <div class="big">{BIG[h.result] ?? 'RESULT UNKNOWN'}</div>
        <div class="facts">
          <span>{h.queue}</span><span>Patch {h.patch ?? '—'}</span><span>{when(h.played_at)}</span>
          {#if h.game_length_s}<span>{length(h.game_length_s)}</span>{/if}
          <span class="side {ours}">{ours} side</span>
        </div>
        <div class="pred">Predicted {pct(h.pred_win_prob)} → {h.result === 'win' ? 'Won' : h.result === 'loss' ? 'Lost' : '—'}</div>
      </header>
      {#if recap.note}<p class="note">{recap.note}</p>{/if}

      <div class="bans">
        {#each ['blue', 'red'] as side}
          <div class="banside {side}">
            <span class="bantitle">{side === ours ? 'Our' : 'Their'} bans</span>
            {#each recap.bans as phase, pi}
              {#if pi > 0}<span class="divider" title="Phase 2"></span>{/if}
              {#each phase[side] as c}
                {#if c}<Champ champ={c} size={40} banned />{:else}<span class="skip" title="skipped">—</span>{/if}
              {/each}
            {/each}
          </div>
        {/each}
      </div>

      <div class="board">
        <div class="colhead blue">{ours === 'blue' ? 'Us' : 'Them'} · blue</div>
        <div class="colhead mid">Lane matchups</div>
        <div class="colhead red">{ours === 'red' ? 'Us' : 'Them'} · red</div>
        {#each recap.picks as p}
          {@const b = bar(p.matchup, ours)}
          <div class="pick blue">
            {#if p.blue}
              <Champ champ={p.blue.champ} size={64} />
              <div class="who"><div class="name">{p.blue.champ.name}</div>{#if p.blue.player}<div class="player">{p.blue.player}</div>{/if}</div>
              <span class="tag">{p.blue.tag}</span>
            {/if}
          </div>
          <div class="matchup">
            <div class="role">{p.role_label}</div>
            {#if b}
              <div class="track" title="{signed(p.matchup.delta_pp)} for {ours === 'blue' ? 'blue (us)' : 'red (us)'} · {p.matchup.games} games">
                <span class="axis"></span>
                <span class="fill {b.band} {b.toward}" style="width:{b.width}%"></span>
              </div>
              <div class="value {b.band}">{signed(p.matchup.delta_pp)} <small>{p.matchup.band === 'ours' ? 'us' : p.matchup.band === 'theirs' ? 'them' : 'even'}</small></div>
            {:else}
              <div class="track"><span class="axis"></span></div>
              <div class="value nodata">no data</div>
            {/if}
          </div>
          <div class="pick red">
            {#if p.red}
              <span class="tag">{p.red.tag}</span>
              <div class="who"><div class="name">{p.red.champ.name}</div>{#if p.red.player}<div class="player">{p.red.player}</div>{/if}</div>
              <Champ champ={p.red.champ} size={64} />
            {/if}
          </div>
        {/each}
      </div>

      <div class="review">
        <div class="chart"><Timeline points={recap.timeline} swingStep={recap.review.biggest_swing?.step} /></div>
        <div class="facts2">
          <div>
            <h4>Followed our suggestions?</h4>
            <div class="ranks">
              {#each recap.review.followed as f}
                <span class="rk" title="{f.type} · {f.champ?.name ?? 'skipped'}">
                  <Champ champ={f.champ} size={28} banned={f.type === 'ban'} />
                  <b class:off={f.rank === 'off-list'}>{f.rank === 'off-list' ? 'off' : f.rank === '—' ? '—' : `#${f.rank}`}</b>
                </span>
              {/each}
            </div>
          </div>
          {#if recap.review.biggest_swing}
            {@const s = recap.review.biggest_swing}
            <div>
              <h4>Biggest swing</h4>
              <p>Step {s.step} ({s.side} {s.type}{s.label !== 'ban' ? ' ' + s.label : ''}): {s.champ?.name ?? 'skipped'},
                {signed(s.delta * 100)} to {pct(s.win_prob)}</p>
            </div>
          {/if}
          <div>
            <h4>Synergy</h4>
            <p>Us {signed(recap.review.synergy.ours_pp)} · Them {signed(recap.review.synergy.theirs_pp)} · net {signed(recap.review.synergy.net_pp)}</p>
          </div>
        </div>
      </div>

      <button class="toggle" onclick={() => (showTable = !showTable)}>{showTable ? 'Hide' : 'Show'} full table</button>
      {#if showTable}
        <table>
          <thead><tr><th>Step</th><th>Team</th><th>Action</th><th>Champion</th><th>Our rank</th><th>P(win) after</th></tr></thead>
          <tbody>
            {#each recap.table as a}
              <tr class:ours={a.ours}>
                <td>{a.step}</td><td class="side {a.side}">{a.ours ? 'us' : 'them'}</td><td>{a.type}</td>
                <td>{a.champ?.name ?? '— skipped'}</td><td>{a.rank ? `#${a.rank}` : ''}</td><td>{pct(a.win_prob)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  </section>
</main>

<style>
  .history { display: grid; grid-template-columns: 380px minmax(0, 1fr); gap: 20px; padding: 16px 24px 40px; }
  .muted { color: #8b96a3; }
  /* list */
  .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 10px; }
  select { background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 5px 6px; font: inherit; font-size: 13px; }
  .row { width: 100%; display: grid; grid-template-columns: 64px 1fr; grid-template-rows: auto auto; gap: 4px 10px; align-items: center;
         background: var(--panel); border: 1px solid transparent; border-radius: 8px; padding: 8px 10px; margin-bottom: 6px;
         color: var(--text); font: inherit; text-align: left; cursor: pointer; }
  .row.active { border-color: #e3b341; }
  .row:hover { background: var(--panel-2); }
  .chip { grid-row: span 2; font-weight: 800; font-size: 14px; text-align: center; border-radius: 4px; padding: 4px 0; background: #222a35; }
  .chip.win { color: #1baf7a; }
  .chip.loss { color: #e5737d; }
  .chip.remake, .chip.unknown { color: #8b96a3; }
  .teams { display: flex; align-items: center; gap: 6px; }
  .five { display: inline-flex; gap: 2px; }
  .vs { font-size: 11px; color: #8b96a3; }
  .meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 8px; font-size: 12px; }
  .side { text-transform: uppercase; font-weight: 700; }
  .side.blue { color: #5b9be0; }
  .side.red { color: #e5737d; }
  /* recap */
  .recap { min-width: 0; }
  .head { border-left: 6px solid #4a5361; background: var(--panel); border-radius: 8px; padding: 14px 20px;
          display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 20px; }
  .head.win { border-left-color: #1baf7a; }
  .head.loss { border-left-color: #e5737d; }
  .big { font-size: 44px; font-weight: 900; letter-spacing: 2px; }
  .facts { display: flex; flex-wrap: wrap; gap: 14px; color: #c9d1d9; font-size: 15px; }
  .pred { font-size: 18px; font-weight: 600; }
  .note { color: #e3b341; font-size: 13px; margin: 6px 2px 0; }
  .bans { display: flex; justify-content: space-between; gap: 20px; margin: 14px 0; }
  .banside { display: flex; align-items: center; gap: 6px; padding: 8px 10px; background: var(--panel); border-radius: 8px; }
  .banside.blue { border-top: 3px solid #2f6fb7; }
  .banside.red { border-top: 3px solid #c0423a; flex-direction: row-reverse; }
  .bantitle { font-size: 12px; color: #8b96a3; text-transform: uppercase; margin: 0 6px; }
  .divider { width: 1px; align-self: stretch; background: #4a5361; margin: 0 4px; }
  .skip { width: 40px; height: 40px; display: inline-grid; place-items: center; border: 1px dashed #4a5361; border-radius: 6px; color: #8b96a3; }
  .board { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 300px) minmax(0, 1fr); gap: 8px 14px; }
  .colhead { font-size: 13px; text-transform: uppercase; color: #8b96a3; padding-bottom: 4px; }
  .colhead.blue { border-bottom: 3px solid #2f6fb7; }
  .colhead.red { border-bottom: 3px solid #c0423a; text-align: right; }
  .colhead.mid { text-align: center; }
  .pick { display: flex; align-items: center; gap: 12px; background: var(--panel); border-radius: 8px; padding: 8px 12px; min-height: 80px; }
  .pick.red { justify-content: flex-end; text-align: right; }
  .name { font-size: 20px; font-weight: 700; }
  .player { font-size: 14px; color: #c9d1d9; }
  .tag { font-size: 13px; font-weight: 700; color: #8b96a3; border: 1px solid #4a5361; border-radius: 4px; padding: 1px 6px; }
  .pick.blue .tag { margin-left: auto; }
  .pick.red .tag { margin-right: auto; }
  .matchup { display: grid; align-content: center; gap: 4px; text-align: center; }
  .role { font-size: 12px; text-transform: uppercase; color: #8b96a3; }
  .track { position: relative; height: 12px; background: #1f2630; border-radius: 3px; }
  .axis { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 2px; background: #8b96a3; transform: translateX(-1px); }
  .fill { position: absolute; top: 0; bottom: 0; }
  .fill.left { right: 50%; border-radius: 4px 0 0 4px; }
  .fill.right { left: 50%; border-radius: 0 4px 4px 0; }
  .fill.ours { background: #1baf7a; }
  .fill.theirs { background: #c98500; }
  .fill.even { background: #8b96a3; }
  .value { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value small { font-weight: 400; color: #8b96a3; }
  .value.nodata { color: #8b96a3; font-weight: 400; }
  .review { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 16px; margin-top: 16px;
            background: var(--panel); border-radius: 8px; padding: 14px 16px; }
  h4 { margin: 0 0 6px; font-size: 14px; color: #c9d1d9; }
  .facts2 { display: grid; gap: 12px; align-content: start; }
  .facts2 p { margin: 0; font-size: 14px; }
  .ranks { display: flex; flex-wrap: wrap; gap: 6px; }
  .rk { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; font-size: 12px; }
  .rk b { font-weight: 700; }
  .rk b.off { color: #c98500; }
  .toggle { margin-top: 12px; background: none; border: 1px solid var(--line); color: var(--text); border-radius: 6px; padding: 6px 12px; cursor: pointer; font: inherit; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }
  th { text-align: left; color: #8b96a3; font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 4px 8px; border-bottom: 1px solid #1f2630; }
  tr.ours td { background: #18202b; }
  @media (max-width: 1100px) {
    .history { grid-template-columns: 1fr; }
    .review { grid-template-columns: 1fr; }
    .head { grid-template-columns: 1fr; gap: 6px; }
    .board { grid-template-columns: minmax(0, 1fr) minmax(150px, 220px) minmax(0, 1fr); }
    .name { font-size: 16px; }
    .bans { flex-direction: column; }
    .banside.red { flex-direction: row; }
  }
</style>
