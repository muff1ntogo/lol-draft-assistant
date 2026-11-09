<script>
  // Prep page, top half (docs/synergy-v2.md, Stage 4): the synergy visualiser (our five role pools: columns in-lane, a
  // pentagon of icons in teamfight, see ringLayout.js), the plan panel and the synergy table. Clicking a node (or a table
  // row) locks it into the plan; locked nodes collapse their group and every other node shows its Overall gain if added.
  // Edge colour: teal = positive, amber = negative, grey = within ±0.5pp (red/green fails colour-blind checks).
  import Champ from './Champ.svelte';
  import { patchLabel } from './patch.js';
  import { ringLayout } from './ringLayout.js';
  import { arcMove } from './motion.js';
  import { DOWN, LINE_STRONG, UP } from './palette.js';

  let { locks = $bindable({}), onplan = () => {} } = $props();

  const ROLES = ['top', 'jungle', 'mid', 'adc', 'support'];
  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const BRACKETS = ['all', 'iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald'];
  const ROW_H = 46, CARD_W = 178, ICON = 34, HEAD_H = 40, FOOT_H = 36, PAD = 8;
  const NEUTRAL = 0.5;

  const saved = (() => { try { return JSON.parse(localStorage.getItem('prep-synergy') || '{}'); } catch { return {}; } })();
  let scope = $state(saved.scope ?? 'teamfight');
  let view = $state(saved.view ?? 'overall');
  let threshold = $state(saved.threshold ?? 0.5);
  let columns = $state(saved.columns ?? {});
  let useDraft = $state(saved.useDraft ?? false);
  let data = $state(null);
  let error = $state(null);
  let hovered = $state(null);
  let width = $state(1100);
  let adding = $state({});
  let sortKey = $state('overall_pp');
  let sortDir = $state(-1);
  let showAll = $state(false);

  $effect(() => {
    try {
      localStorage.setItem('prep-synergy', JSON.stringify({ scope, view, threshold, columns, useDraft }));
    } catch { /* storage unavailable: settings just aren't remembered */ }
  });

  let timer;
  $effect(() => {
    const body = JSON.stringify({ scope, view, threshold_pp: threshold, locks,
                                  columns: Object.fromEntries(ROLES.filter((r) => columns[r]?.length).map((r) => [r, columns[r]])),
                                  use_draft: useDraft });
    clearTimeout(timer);
    timer = setTimeout(() => load(body), 120);
  });

  async function load(body) {
    const r = await fetch('/api/prep/synergy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    if (!r.ok) { error = (await r.json()).detail; return; }
    error = null;
    data = await r.json();
    onplan(data);
  }

  const signed = (x, d = 1) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(d)}`);
  const key = (role, id) => `${role}:${id}`;
  const draftLocked = (role) => data?.draft?.locks?.[role] != null;
  const colour = (v) => (v > NEUTRAL ? UP : v < -NEUTRAL ? DOWN : LINE_STRONG);
  const cls = (v) => (v > NEUTRAL ? 'pos' : v < -NEUTRAL ? 'neg' : '');

  function toggleLock(role, id) {
    if (draftLocked(role)) return;
    const next = { ...locks };
    if (next[role] === id) delete next[role];
    else next[role] = id;
    locks = next;
  }
  function lockRow(row) {
    if (row.a && row.b) locks = { ...locks, [row.a.role]: row.a.id, [row.b.role]: row.b.id };
    else if (row.champ) locks = { ...locks, [row.role]: row.champ.id };
  }
  function remove(n, e) {
    e.stopPropagation();
    const current = columns[n.role]?.length ? columns[n.role] : cols.find((c) => c.role === n.role).nodes.map((x) => x.id);
    columns[n.role] = current.filter((c) => c !== n.id);
  }
  function add(role) {
    const name = (adding[role] ?? '').trim().toLowerCase();
    const champ = data?.champions.find((c) => c.name.toLowerCase() === name);
    if (!champ) return;
    const current = columns[role]?.length ? columns[role] : cols.find((c) => c.role === role).nodes.map((x) => x.id);
    if (!current.includes(champ.id) && current.length < 15) columns[role] = [...current, champ.id];
    adding[role] = '';
  }
  async function setBracket(b) {
    await fetch('/api/synergy/bracket', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bracket: b }) });
    columns = { ...columns };
  }
  const reset = () => { columns = {}; locks = {}; };

  // ---------- layout ----------
  const cols = $derived(data?.columns ?? []);
  const maxRows = $derived(Math.max(1, ...cols.map((c) => c.nodes.length)));
  const ring = $derived(data?.scope === 'teamfight' ? ringLayout(cols, width) : null);
  const graphH = $derived(ring ? ring.height : HEAD_H + maxRows * ROW_H + FOOT_H);
  const colX = (i) => PAD + (i * (width - CARD_W - 2 * PAD)) / (ROLES.length - 1);
  const positions = $derived.by(() => {
    if (ring) return ring.nodes;
    const pos = {};
    cols.forEach((c, i) => {
      const offset = ((maxRows - c.nodes.length) * ROW_H) / 2;
      c.nodes.forEach((n, j) => (pos[key(c.role, n.id)] = { x: colX(i), y: HEAD_H + offset + j * ROW_H + ROW_H / 2, col: i }));
    });
    return pos;
  });
  const touches = (e, h) => h && ((e.a.role === h.role && e.a.id === h.id) || (e.b.role === h.role && e.b.id === h.id));
  const champById = $derived(Object.fromEntries((data?.champions ?? []).map((c) => [c.id, c])));
  const edges = $derived.by(() => {
    if (!data) return [];
    const out = [];
    for (const e of data.edges) {
      const pa = positions[key(e.a.role, e.a.id)], pb = positions[key(e.b.role, e.b.id)];
      if (!pa || !pb) continue;
      let d;
      if (ring) d = `M${pa.x},${pa.y} L${pb.x},${pb.y}`;
      else {
        const [l, r] = pa.col < pb.col ? [pa, pb] : [pb, pa];
        const x1 = l.x + CARD_W, x2 = r.x, dx = (x2 - x1) * 0.45;
        d = `M${x1},${l.y} C${x1 + dx},${l.y} ${x2 - dx},${r.y} ${x2},${r.y}`;
      }
      out.push({ ...e, k: `${key(e.a.role, e.a.id)}|${key(e.b.role, e.b.id)}`, hot: touches(e, hovered), d,
                 w: 1 + Math.min(Math.abs(e.value_pp), 6) * 1.0 });
    }
    return out.sort((a, b) => a.hot - b.hot || Math.abs(a.value_pp) - Math.abs(b.value_pp));
  });
  const opacity = (e) => {
    if (hovered) return e.hot ? 1 : 0;
    return (ring ? 0.5 : 0.7) * (e.low_data ? 0.6 : 1);
  };
  // Hover isolates a champion: only their lines stay, and only the champions those lines reach stay at full strength.
  const linked = $derived.by(() => {
    if (!hovered) return null;
    const keep = new Set([key(hovered.role, hovered.id)]);
    for (const e of edges) {
      if (e.hot) {
        keep.add(key(e.a.role, e.a.id));
        keep.add(key(e.b.role, e.b.id));
      }
    }
    return keep;
  });
  const isolatedOut = (role, id) => linked != null && !linked.has(key(role, id));
  const ringTitle = (n) => {
    const lines = [n.unavailable ? `${n.name}: banned or picked` : n.locked ? `${n.name}: click to unlock` : `Lock ${n.name} into the plan`];
    if (!n.tier && !n.locked) lines.push('Off-pool');
    if (n.untagged) lines.push('No judgement tags: measured traits only');
    if (n.badge?.text) lines.push(n.badge.text);
    return lines.join('\n');
  };
  const tip = (e) => {
    const a = champById[e.a.id]?.name, b = champById[e.b.id]?.name;
    const lines = [`${a} + ${b}`, `Stat ${signed(e.stat_pp)}pp (${e.games} games${e.low_data ? ', low data' : ''})`];
    if (e.core) lines.push(`Style ${signed(e.style_pp)}pp · Lane ${signed(e.lane_pp)}pp`);
    lines.push(`Part of Overall ${signed(e.overall_pp)}pp`);
    for (const r of e.rules) lines.push(`  ${r.label}`);
    return lines.join('\n');
  };

  // ---------- table ----------
  const SORTS = { combo: (r) => `${r.a.name}${r.b.name}`, stat_pp: (r) => r.stat_pp, style_pp: (r) => r.style_pp,
                  overall_pp: (r) => r.overall_pp, games: (r) => r.games };
  const tableRows = $derived.by(() => {
    const rows = [...(data?.table.pairs ?? [])];
    const f = SORTS[sortKey];
    rows.sort((x, y) => {
      const a = f(x), b = f(y);
      return (a > b ? 1 : a < b ? -1 : 0) * sortDir;
    });
    return showAll ? rows : rows.slice(0, 20);
  });
  function sortBy(k) {
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = k === 'combo' ? 1 : -1; }
  }
  const plan = $derived(data?.plan);
  const pct = (x) => `${Math.round(x * 100)}%`;
  const BARS = [['engage', 'Engage'], ['frontline', 'Frontline'], ['peel', 'Peel'], ['poke', 'Poke'], ['scaling', 'Scaling']];
</script>

<div class="controls">
  <div class="group" role="group" aria-label="Scope">
    <span class="lbl">Scope</span>
    <button class:on={scope === 'lane'} onclick={() => (scope = 'lane')}>In-lane</button>
    <button class:on={scope === 'teamfight'} onclick={() => (scope = 'teamfight')}>Teamfight</button>
  </div>
  <div class="group" role="group" aria-label="View">
    <span class="lbl">View</span>
    {#each [['overall', 'Overall'], ['stat', 'Stat'], ['style', 'Style'], ['team', 'Team']] as [k, l]}
      <button class:on={view === k} onclick={() => (view = k)}>{l}</button>
    {/each}
  </div>
  <label class="slider">Threshold <strong>{signed(threshold)}pp</strong>
    <input type="range" min="0" max="3" step="0.1" bind:value={threshold} /></label>
  <label><input type="checkbox" bind:checked={useDraft} /> Use current draft
    {#if useDraft}<span class="muted">({data?.draft?.live ? 'live' : 'no champ select'})</span>{/if}</label>
  <label>Data
    <select value={data?.synergy_bracket ?? 'all'} onchange={(e) => setBracket(e.target.value)}>
      {#each BRACKETS as b}<option value={b}>{b === 'all' ? 'All brackets' : b[0].toUpperCase() + b.slice(1)}</option>{/each}
    </select></label>
  <button class="reset" onclick={reset}>Reset plan and columns</button>
</div>
{#if error}<p class="error">{typeof error === 'string' ? error : JSON.stringify(error)}</p>{/if}

{#if data}
  <div class="top">
    <div class="left">
      <div class="graph" bind:clientWidth={width} style="height:{graphH}px">
        <svg width={width} height={graphH} aria-label="Pair synergy edges">
          {#each edges as e (e.k)}
            <path d={e.d} fill="none" stroke={colour(e.value_pp)} stroke-width={e.w} stroke-dasharray={e.low_data ? '5 4' : null}
                  stroke-opacity={opacity(e)} stroke-linecap="round" />
            <path d={e.d} class="hit" fill="none" stroke="transparent" stroke-width="9"><title>{tip(e)}</title></path>
          {/each}
        </svg>
        {#if view === 'team'}<p class="teamnote muted">Team has no edges: it is in the plan panel.</p>{/if}
        {#snippet addBox(col)}
          <input list="prep-champ-names" placeholder="+ add champion" bind:value={adding[col.role]} onchange={() => add(col.role)} />
        {/snippet}
        {#if ring}
          {#each cols as col (col.role)}
            {@const h = ring.heads[col.role]}
            <div class="ringhead {h.place}" style="left:{h.x}px; top:{h.y}px">
              <div><strong>{col.label}</strong>{#if col.player} <span>{col.player.split('#')[0]}</span>{/if}</div>
              {#if !locks[col.role] && !draftLocked(col.role) && col.nodes.length < 15}
                <form onsubmit={(e) => { e.preventDefault(); add(col.role); }}>{@render addBox(col)}</form>
              {/if}
            </div>
            {#each col.nodes as n (n.id)}
              {@const p = positions[key(col.role, n.id)]}
              <div class="ringnode" class:locked={n.locked} class:unavailable={n.unavailable} class:isolated-out={isolatedOut(col.role, n.id)}
                   class:hot={hovered && hovered.role === col.role && hovered.id === n.id}
                   style="left:{p.x}px; top:{p.y}px" role="button" tabindex="0" aria-label={ringTitle(n)}
                   use:arcMove={{ x: p.x, y: p.y, cx: ring.cx, cy: ring.cy, width, base: 'translate(-50%, -50%)' }}
                   onclick={() => !n.unavailable && toggleLock(col.role, n.id)}
                   onkeydown={(e) => e.key === 'Enter' && !n.unavailable && toggleLock(col.role, n.id)}
                   onmouseenter={() => (hovered = { role: col.role, id: n.id })} onmouseleave={() => (hovered = null)}
                   title={ringTitle(n)}>
                <Champ champ={n} size={ring.icon} banned={n.unavailable} />
                {#if n.tier}<span class="tier {n.tier}" title={n.tier}>{n.tier[0].toUpperCase()}</span>{/if}
                {#if n.gain_pp != null}<span class="chip gain {cls(n.gain_pp)}" title="Overall gain if added to the plan">{signed(n.gain_pp)}</span>{/if}
                {#if !n.locked}<button class="x" title="Remove {n.name}" onclick={(e) => remove(n, e)}>×</button>{/if}
              </div>
            {/each}
          {/each}
        {:else}
        {#each cols as col, i (col.role)}
          <div class="colhead" style="left:{colX(i)}px; width:{CARD_W}px">
            <strong>{col.label}</strong>{#if col.player}<span>{col.player.split('#')[0]}</span>{/if}
          </div>
          {#each col.nodes as n (n.id)}
            {@const p = positions[key(col.role, n.id)]}
            <div class="node" class:locked={n.locked} class:unavailable={n.unavailable} class:isolated-out={isolatedOut(col.role, n.id)}
                 class:hot={hovered && hovered.role === col.role && hovered.id === n.id}
                 style="left:{p.x}px; top:{p.y - ROW_H / 2 + 3}px; width:{CARD_W}px; height:{ROW_H - 6}px"
                 use:arcMove={{ x: p.x, y: p.y, width, bow: 0 }}
                 role="button" tabindex="0" onclick={() => !n.unavailable && toggleLock(col.role, n.id)}
                 onkeydown={(e) => e.key === 'Enter' && !n.unavailable && toggleLock(col.role, n.id)}
                 onmouseenter={() => (hovered = { role: col.role, id: n.id })} onmouseleave={() => (hovered = null)}
                 title={(n.unavailable ? `${n.name}: banned or picked` : n.locked ? 'Click to unlock' : `Lock ${n.name} into the plan`)
                        + (n.badge?.text ? `\n${n.badge.text}` : '')}>
              <span class="icon"><Champ champ={n} size={ICON} banned={n.unavailable} />
                {#if n.tier}<span class="tier {n.tier}" title={n.tier}>{n.tier[0].toUpperCase()}</span>{/if}</span>
              <span class="meta">
                <span class="name">{n.locked ? '🔒 ' : ''}{n.name}</span>
                <span class="chips">
                  {#if n.gain_pp != null}<span class="chip {cls(n.gain_pp)}" title="Overall gain if added to the plan">{signed(n.gain_pp)}</span>{/if}
                  {#if n.untagged}<span class="chip untagged" title="No judgement tags: measured traits only">untagged</span>{/if}
                  {#if !n.tier && !n.locked}<span class="muted small">off-pool</span>{/if}
                </span>
              </span>
              {#if !n.locked}<button class="x" title="Remove from this column" onclick={(e) => remove(n, e)}>×</button>{/if}
            </div>
          {/each}
          {#if !locks[col.role] && !draftLocked(col.role) && col.nodes.length < 15}
            <form class="add" style="left:{colX(i)}px; top:{graphH - FOOT_H + 4}px; width:{CARD_W}px"
                  onsubmit={(e) => { e.preventDefault(); add(col.role); }}>
              {@render addBox(col)}
            </form>
          {/if}
        {/each}
        {/if}
        <datalist id="prep-champ-names">{#each data.champions as c}<option value={c.name}></option>{/each}</datalist>
      </div>

      <section class="table">
        <h2>Synergy table <small>click a row to lock it into the plan</small></h2>
        {#if data.table.completions.length}
          <h3>Best completions of the plan</h3>
          <table>
            <thead><tr><th>Add</th><th class="num">Gain</th><th class="num">Stat</th><th class="num">Style</th>
              <th class="num">Team</th><th class="num">Overall</th><th>Why</th></tr></thead>
            <tbody>
              {#each data.table.completions as c}
                <tr onclick={() => lockRow(c)}>
                  <td class="combo"><Champ champ={c.champ} size={22} /> {c.champ.name} <span class="muted small">{LABELS[c.role]}</span></td>
                  <td class="num {cls(c.gain_pp)}">{signed(c.gain_pp)}</td><td class="num">{signed(c.stat_pp)}</td>
                  <td class="num">{signed(c.style_pp)}</td><td class="num">{signed(c.team_pp)}</td>
                  <td class="num">{signed(c.overall_pp)}</td><td class="why">{c.why ?? ''}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <h3>Core pairs</h3>
        {/if}
        <table>
          <thead><tr>
            {#each [['combo', 'Combo'], ['stat_pp', 'Stat'], ['style_pp', 'Style'], ['overall_pp', 'Overall'], ['games', 'Games']] as [k, l]}
              <th class:num={k !== 'combo'} class="sortable" onclick={() => sortBy(k)}>{l}{sortKey === k ? (sortDir < 0 ? ' ▾' : ' ▴') : ''}</th>
            {/each}
            <th>Why</th>
          </tr></thead>
          <tbody>
            {#each tableRows as r}
              <tr onclick={() => lockRow(r)}>
                <td class="combo"><Champ champ={r.a} size={22} /><Champ champ={r.b} size={22} /> {r.a.name} + {r.b.name}
                  <span class="muted small">{r.pair}</span></td>
                <td class="num">{signed(r.stat_pp)}{r.low_data ? '*' : ''}</td><td class="num">{signed(r.style_pp)}</td>
                <td class="num {cls(r.overall_pp)}">{signed(r.overall_pp)}</td><td class="num">{r.games}</td>
                <td class="why">{r.why ?? ''}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="muted small">Overall = the pair's part of Overall (its Stat weight moves to Style while it has few games together).
          * = under {data.low_data_games} games.
          {#if data.table.pairs.length > 20}<button class="linkbtn" onclick={() => (showAll = !showAll)}>
            {showAll ? 'Show the top 20' : `Show all ${data.table.pairs.length}`}</button>{/if}</p>
      </section>
    </div>

    <aside class="panel">
      <h2>Plan</h2>
      {#if plan.champs.length}
        <div class="locked">
          {#each plan.champs as c}
            <button class="lockchip" title="Unlock {c.name}" onclick={() => toggleLock(c.role, c.id)}>
              <Champ champ={c} size={28} /> <span>{c.name}<small>{LABELS[c.role]}</small></span></button>
          {/each}
        </div>
        <div class="numbers">
          <div><span>Stat</span><strong class={cls(plan.team.stat_total_pp)}>{signed(plan.team.stat_total_pp)}</strong></div>
          <div><span>Style</span><strong class={cls(plan.team.style_total_pp)}>{signed(plan.team.style_total_pp)}</strong></div>
          <div><span>Team</span>{#if plan.team.show_team}<strong class={cls(plan.team.team_weighted_pp)}>{signed(plan.team.team_weighted_pp)}</strong>
            {:else}<em class="muted small">from 3 roles</em>{/if}</div>
          <div class="overall"><span>Overall</span><strong class={cls(plan.team.overall_pp)}>{signed(plan.team.overall_pp)}</strong></div>
        </div>
        <p class="muted small">pp of win chance; they add up to Overall. Team is shown at its Overall weight
          (team model {signed(plan.team.team_pp)}pp vs an average comp).</p>
        {#if plan.team.identity.label}
          <p class="ident">Identity: <strong>{plan.team.identity.name}</strong> {pct(plan.team.identity.coherence)}</p>
        {/if}
        <div class="dmg">
          <div class="dmgbar" title="Physical {pct(plan.team.damage.physical)} · Magic {pct(plan.team.damage.magic)} · True {pct(plan.team.damage.true)}">
            <span class="phys" style="flex-grow:{plan.team.damage.physical}"></span><span class="magic" style="flex-grow:{plan.team.damage.magic}"></span><span class="true" style="flex-grow:{plan.team.damage.true}"></span>
          </div>
          <div class="dmglegend"><span><i class="phys"></i>{pct(plan.team.damage.physical)} AD</span><span><i class="magic"></i>{pct(plan.team.damage.magic)} AP</span><span><i class="true"></i>{pct(plan.team.damage.true)} true</span></div>
        </div>
        <div class="bars">
          {#each BARS as [k, l]}
            <div class="bar"><span>{l}</span><span class="track"><span style="width:{pct(plan.team.bars[k] ?? 0)}"></span></span></div>
          {/each}
        </div>
        {#if plan.team.show_team && plan.team.team_rules.length}
          <h3>Team model</h3>
          {#each plan.team.team_rules as r}<div class="rule"><span>{r.note}</span><span class={cls(r.pp)}>{signed(r.pp)}</span></div>{/each}
        {/if}
        {#if plan.pairs.length}
          <h3>Pairs</h3>
          {#each plan.pairs as p}
            <div class="rule" title={p.rules.map((r) => r.label).join('\n')}>
              <span>{p.champs[0].name} + {p.champs[1].name}{#if p.core}<span class="lane">lane</span>{/if}</span>
              <span class={cls(p.overall_pp)}>{signed(p.overall_pp)}</span>
            </div>
          {/each}
        {/if}
      {:else}
        <p class="muted">Click champions in the graph (or rows in the table) to lock them into a plan.</p>
      {/if}
      <p class="muted small legend">
        <span class="sw pos"></span>positive <span class="sw neg"></span>negative <span class="sw"></span>within ±0.5pp ·
        dashed = low data · tier M / C / L · data {data.synergy_bracket === 'all' ? 'all brackets' : data.synergy_bracket}, patch {patchLabel(data.window) ?? '—'}
      </p>
    </aside>
  </div>
{:else if !error}
  <p class="muted">Loading…</p>
{/if}

<style>
  .controls { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; font-size: 14px; }
  .group { display: inline-flex; gap: 4px; align-items: center; }
  .lbl { color: var(--muted); margin-right: 2px; }
  button { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px; cursor: pointer; }
  button.on { background: var(--gold-bg); border-color: var(--gold); color: var(--gold); }
  .slider { display: inline-flex; align-items: center; gap: 8px; }
  .slider strong { min-width: 52px; font-variant-numeric: tabular-nums; }
  label { display: inline-flex; align-items: center; gap: 6px; color: var(--text-2); }
  select, input:not([type]) { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
  .reset { margin-left: auto; }
  .error { color: var(--red); }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }

  .top { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }
  .left { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .graph { position: relative; background: var(--panel); border-radius: 8px; overflow: hidden; }
  svg { position: absolute; inset: 0; }
  .hit { pointer-events: stroke; cursor: help; }
  .teamnote { position: absolute; left: 0; right: 0; bottom: 4px; text-align: center; font-size: 13px; margin: 0; }
  .colhead { position: absolute; top: 9px; display: flex; gap: 8px; align-items: baseline; font-size: 15px; }
  .colhead span { color: var(--muted); font-size: 13px; }
  .node { position: absolute; display: flex; align-items: center; gap: 7px; padding: 0 6px; background: var(--panel-2);
          border: 1px solid var(--line); border-radius: 8px; cursor: pointer; user-select: none; }
  .node:hover, .node.hot { border-color: var(--gold); }
  .node.locked { border-color: var(--gold); background: var(--gold-bg); }
  .node.unavailable { opacity: 0.45; cursor: not-allowed; }
  .icon { position: relative; flex: none; }
  .tier { position: absolute; right: -4px; bottom: -4px; width: 15px; height: 15px; border-radius: 50%; font-size: 9px; font-weight: 700;
          display: grid; place-items: center; background: var(--bg); border: 1px solid var(--line); }
  .tier.main { color: var(--gold); border-color: var(--gold); }
  .tier.comfortable { color: var(--text-2); }
  .tier.learning { color: var(--muted); }
  .meta { display: flex; flex-direction: column; min-width: 0; gap: 1px; }
  .name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chips { display: flex; gap: 3px; }
  .chip { font-size: 11px; padding: 0 4px; border-radius: 4px; background: var(--line); color: var(--text-2); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .chip.pos { background: var(--up-bg); color: var(--up-text); }
  .chip.neg { background: var(--down-bg); color: var(--down-text); }
  .chip.untagged { background: var(--down-bg); color: var(--down-text); }
  .x { position: absolute; right: 3px; top: 1px; padding: 0 5px; line-height: 16px; font-size: 14px; background: none; border: none; color: var(--muted); visibility: hidden; }
  .node:hover .x { visibility: visible; }
  .add { position: absolute; margin: 0; }
  .add input { width: 100%; font-size: 13px; box-sizing: border-box; }

  .ringhead { position: absolute; width: 134px; font-size: 15px; }
  .ringhead.above { transform: translate(-50%, -100%); text-align: center; }
  .ringhead.left { transform: translate(-100%, -50%); text-align: right; }
  .ringhead.right { transform: translateY(-50%); }
  .ringhead span { color: var(--muted); font-size: 13px; margin-left: 6px; }
  .ringhead form { margin: 3px 0 0; }
  .ringhead input { width: 100%; font-size: 12px; box-sizing: border-box; }
  .ringnode { position: absolute; transform: translate(-50%, -50%); display: grid; place-items: center; border-radius: 6px;
              background: var(--panel); cursor: pointer; user-select: none; }
  .ringnode:hover, .ringnode.hot { outline: 2px solid var(--gold); z-index: 2; }
  .ringnode.locked { outline: 2px solid var(--gold); }
  .ringnode.unavailable { opacity: 0.45; cursor: not-allowed; }
  .ringnode .chip.gain { position: absolute; left: 50%; bottom: -7px; transform: translateX(-50%); line-height: 13px; font-size: 10px; }
  .ringnode:hover .x { visibility: visible; }
  .node.isolated-out, .ringnode.isolated-out { opacity: 0.25; }
  .ringnode .x { right: -9px; top: -9px; width: 16px; height: 16px; padding: 0; line-height: 14px; background: var(--bg); border: 1px solid var(--line); border-radius: 50%; }

  section.table, .panel { background: var(--panel); border-radius: 8px; padding: 12px 14px; }
  h2 { font-size: 17px; margin: 0 0 8px; }
  h2 small { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 6px; }
  h3 { font-size: 14px; margin: 10px 0 4px; color: var(--text-2); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th.sortable { cursor: pointer; }
  td { padding: 3px 6px; border-bottom: 1px solid var(--panel-2); }
  tbody tr { cursor: pointer; }
  tbody tr:hover td { background: var(--panel-2); }
  .combo { display: flex; align-items: center; gap: 4px; white-space: nowrap; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .why { color: var(--muted); font-size: 12px; }
  .pos { color: var(--up-text); }
  .neg { color: var(--down-text); }

  .panel { position: sticky; top: 8px; }
  .locked { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .lockchip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px 3px 3px; border-color: var(--gold); }
  .lockchip span { display: flex; flex-direction: column; align-items: flex-start; font-size: 13px; line-height: 1.1; }
  .lockchip small { color: var(--muted); font-size: 11px; }
  .numbers { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
  .numbers div { background: var(--panel-2); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column; }
  .numbers span { color: var(--muted); font-size: 12px; }
  .numbers strong { font-size: 20px; font-variant-numeric: tabular-nums; }
  .numbers .overall { outline: 1px solid var(--gold); }
  .ident { margin: 8px 0 4px; }
  .dmgbar { display: flex; height: 12px; border-radius: 4px; overflow: hidden; gap: 2px; }
  .phys { background: #d95926; }
  .magic { background: #3987e5; }
  .true { background: #199e70; }
  .dmglegend { display: flex; gap: 12px; font-size: 12px; color: var(--text-2); margin: 4px 0 8px; }
  .dmglegend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 4px; }
  .bars { display: flex; flex-direction: column; gap: 4px; }
  .bar { display: grid; grid-template-columns: 72px 1fr; align-items: center; font-size: 12px; color: var(--text-2); }
  .track { height: 7px; background: var(--panel-2); border-radius: 4px; overflow: hidden; }
  .track span { display: block; height: 100%; background: #3987e5; }
  .rule { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; padding: 2px 0; }
  .rule span:last-child { font-variant-numeric: tabular-nums; }
  .legend { margin-top: 10px; line-height: 1.6; }
  .lane { font-size: 10px; color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 0 3px; margin-left: 5px; }
  .linkbtn { background: none; border: none; color: var(--gold); padding: 0 4px; font-size: 12px; }
  .sw { display: inline-block; width: 14px; height: 3px; background: var(--line-strong); vertical-align: middle; margin: 0 3px 0 6px; }
  .sw.pos { background: var(--up); }
  .sw.neg { background: var(--down); }
</style>
