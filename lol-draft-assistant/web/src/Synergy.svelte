<script>
  // Synergy visualiser (docs/synergy-visualizer.md, Stage B). A layered graph: one column per role,
  // pair edges between columns. Edge colour: teal = positive, amber = negative, grey = within ±0.5pp
  // (red/green fails colour-blind checks); every value is also printed signed. Dashed = low data.
  // Cards are HTML laid out on a fixed grid; edges are an SVG layer underneath at the same positions.
  import Champ from './Champ.svelte';

  const ROLES = ['top', 'jungle', 'mid', 'adc', 'support'];
  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const BRACKETS = ['all', 'iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald'];
  const ROW_H = 52, CARD_W = 200, ICON = 40, HEAD_H = 46, FOOT_H = 40, PAD = 8;
  const WEAK_EDGE_PP = 0.5;
  const DAMAGE_WARN = 0.8;

  const saved = JSON.parse(localStorage.getItem('synergy-ui') || '{}');
  let roles = $state(saved.roles ?? ['jungle', 'mid', 'support']);
  let columns = $state(saved.columns ?? {});
  let locks = $state(saved.locks ?? {});
  let threshold = $state(saved.threshold ?? 1.0);
  let showWeak = $state(saved.showWeak ?? false);
  let hideLowData = $state(saved.hideLowData ?? false);
  let useDraft = $state(saved.useDraft ?? false);

  let data = $state(null);
  let error = $state(null);
  let hovered = $state(null); // {role, id}
  let width = $state(1400);
  let adding = $state({});

  $effect(() => {
    localStorage.setItem('synergy-ui', JSON.stringify({ roles, columns, locks, threshold, showWeak, hideLowData, useDraft }));
  });

  let timer;
  $effect(() => {
    const body = JSON.stringify({ roles, columns: Object.fromEntries(roles.filter((r) => columns[r]).map((r) => [r, columns[r]])),
                                  locks, threshold_pp: threshold, use_draft: useDraft });
    clearTimeout(timer);
    timer = setTimeout(() => load(body), 120);
  });

  async function load(body) {
    const r = await fetch('/api/synergy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    if (!r.ok) { error = (await r.json()).detail; return; }
    error = null;
    const d = await r.json();
    // remember each column's full list, so unlocking restores it
    for (const col of d.columns) {
      const lockedHere = locks[col.role] || d.draft.locks?.[col.role];
      if (!columns[col.role] && !lockedHere) columns[col.role] = col.nodes.map((n) => n.id);
    }
    data = d;
  }

  const signed = (x, digits = 1) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`;
  const key = (role, id) => `${role}:${id}`;
  const draftLocked = (role) => data?.draft?.locks?.[role] != null;

  function toggleRole(r) {
    if (roles.includes(r)) { if (roles.length > 2) roles = roles.filter((x) => x !== r); }
    else roles = ROLES.filter((x) => x === r || roles.includes(x));
  }
  function toggleLock(node) {
    if (draftLocked(node.role) || node.unavailable) return;
    const next = { ...locks };
    if (next[node.role] === node.id) delete next[node.role];
    else next[node.role] = node.id;
    locks = next;
  }
  function remove(node, e) {
    e.stopPropagation();
    columns[node.role] = (columns[node.role] ?? []).filter((c) => c !== node.id);
  }
  function add(role) {
    const name = (adding[role] ?? '').trim().toLowerCase();
    const champ = data?.champions.find((c) => c.name.toLowerCase() === name);
    if (!champ) return;
    const list = columns[role] ?? [];
    if (!list.includes(champ.id) && list.length < 15) columns[role] = [...list, champ.id];
    adding[role] = '';
  }
  async function setBracket(b) {
    await fetch('/api/synergy/bracket', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bracket: b }) });
    columns = { ...columns }; // refetch
  }
  function resetColumns() { columns = {}; locks = {}; }

  // ---------- layout ----------
  const cols = $derived(data?.columns ?? []);
  const maxRows = $derived(Math.max(1, ...cols.map((c) => c.nodes.length)));
  const graphH = $derived(HEAD_H + maxRows * ROW_H + FOOT_H);
  const colX = (i) => (cols.length < 2 ? PAD : PAD + (i * (width - CARD_W - 2 * PAD)) / (cols.length - 1));
  const positions = $derived.by(() => {
    const pos = {};
    cols.forEach((c, i) => {
      const offset = ((maxRows - c.nodes.length) * ROW_H) / 2;
      c.nodes.forEach((n, j) => (pos[key(c.role, n.id)] = { x: colX(i), y: HEAD_H + offset + j * ROW_H + ROW_H / 2, col: i }));
    });
    return pos;
  });

  // trios through the hovered node
  const hoverTrios = $derived(
    hovered && data ? data.trios.filter((t) => t.champs.some((c) => c.role === hovered.role && c.id === hovered.id)) : [],
  );
  const trioEdges = $derived.by(() => {
    const m = new Map();
    for (const t of hoverTrios) {
      const [a, b, c] = t.champs;
      for (const [x, y] of [[a, b], [a, c], [b, c]]) {
        const k = [key(x.role, x.id), key(y.role, y.id)].sort().join('|');
        m.set(k, (m.get(k) ?? true) && t.weak_pair);
      }
    }
    return m; // edge key -> drawn dashed (only in weak-pair trios)
  });

  const edgeKey = (e) => [key(e.a.role, e.a.id), key(e.b.role, e.b.id)].sort().join('|');
  const touches = (e, h) => h && ((e.a.role === h.role && e.a.id === h.id) || (e.b.role === h.role && e.b.id === h.id));
  const colour = (pp) => (pp > WEAK_EDGE_PP ? '#1baf7a' : pp < -WEAK_EDGE_PP ? '#c98500' : '#5b6573');

  const edges = $derived.by(() => {
    if (!data) return [];
    const out = [];
    for (const e of data.edges) {
      const pa = positions[key(e.a.role, e.a.id)], pb = positions[key(e.b.role, e.b.id)];
      if (!pa || !pb) continue;
      const weak = Math.abs(e.pp) < WEAK_EDGE_PP;
      const k = edgeKey(e);
      const inTrio = trioEdges.has(k);
      const hot = touches(e, hovered) || inTrio;
      if (((weak && !showWeak) || (e.low_data && hideLowData)) && !hot) continue;
      const [l, r] = pa.col < pb.col ? [pa, pb] : [pb, pa];
      const x1 = l.x + CARD_W, x2 = r.x, dx = (x2 - x1) * 0.45;
      out.push({ ...e, k, inTrio, hot, adjacent: r.col - l.col === 1,
                 d: `M${x1},${l.y} C${x1 + dx},${l.y} ${x2 - dx},${r.y} ${x2},${r.y}`,
                 w: 1 + Math.min(Math.abs(e.pp), 6) * 1.1 });
    }
    return out.sort((a, b) => a.hot - b.hot || Math.abs(a.pp) - Math.abs(b.pp));
  });
  const opacity = (e) => {
    if (hovered) return e.hot ? 1 : 0.06;
    return (e.adjacent ? 0.6 : 0.3) * (e.low_data ? 0.6 : 1);
  };

  // ---------- side panel ----------
  const champById = $derived(Object.fromEntries((data?.champions ?? []).map((c) => [c.id, c])));
  const findNode = (role, id) => cols.find((c) => c.role === role)?.nodes.find((n) => n.id === id);
  const hoveredNode = $derived(hovered ? findNode(hovered.role, hovered.id) : null);
  const damageChamps = $derived.by(() => {
    const list = [...(data?.locked_team.champs ?? [])];
    if (hoveredNode && !list.some((c) => c.id === hoveredNode.id)) list.push({ ...hoveredNode, preview: true });
    return list;
  });
  const teamDamage = $derived.by(() => {
    const known = damageChamps.filter((c) => c.damage);
    if (!known.length) return null;
    const sum = (k) => known.reduce((s, c) => s + c.damage[k], 0) / known.length;
    return { phys: sum('phys'), magic: sum('magic'), true: sum('true'), n: known.length, missing: damageChamps.length - known.length };
  });
  const damageWarning = $derived.by(() => {
    if (!teamDamage || teamDamage.n < 2) return null;
    if (teamDamage.phys > DAMAGE_WARN) return `${Math.round(teamDamage.phys * 100)}% physical: easy to itemise armour against`;
    if (teamDamage.magic > DAMAGE_WARN) return `${Math.round(teamDamage.magic * 100)}% magic: easy to itemise magic resist against`;
    return null;
  });
  const pct = (x) => `${Math.round(x * 100)}%`;
  const panelTrios = $derived(hovered && hoverTrios.length ? hoverTrios : (data?.trios ?? []));
</script>

<main class="page">
  <div class="controls">
    <h1>Synergy</h1>
    <div class="group" role="group" aria-label="Roles">
      {#each ROLES as r}
        <button class="role" class:on={roles.includes(r)} onclick={() => toggleRole(r)}>{LABELS[r]}</button>
      {/each}
    </div>
    <label class="slider">Trio threshold <strong>{signed(threshold)}pp</strong>
      <input type="range" min="0" max="4" step="0.1" bind:value={threshold} /></label>
    <label><input type="checkbox" bind:checked={showWeak} /> Show edges within ±0.5pp</label>
    <label><input type="checkbox" bind:checked={hideLowData} /> Hide low data</label>
    <label><input type="checkbox" bind:checked={useDraft} /> Use current draft
      {#if useDraft}<span class="muted">({data?.draft?.live ? 'live' : 'no champ select'})</span>{/if}</label>
    <label>Data
      <select value={data?.synergy_bracket ?? 'all'} onchange={(e) => setBracket(e.target.value)}>
        {#each BRACKETS as b}<option value={b}>{b === 'all' ? 'All brackets' : b[0].toUpperCase() + b.slice(1)}</option>{/each}
      </select></label>
    <button class="reset" onclick={resetColumns}>Reset to pools</button>
  </div>

  {#if error}<p class="error">{error}</p>{/if}

  {#if data}
    <div class="layout">
      <div class="graph" bind:clientWidth={width} style="height:{graphH}px">
        <svg width={width} height={graphH} aria-label="Pair synergy edges">
          {#each edges as e (e.k)}
            <path d={e.d} fill="none" stroke={e.inTrio ? '#e3b341' : colour(e.pp)} stroke-width={e.inTrio ? e.w + 1.5 : e.w}
                  stroke-dasharray={(e.inTrio ? trioEdges.get(e.k) : e.low_data) ? '5 4' : null}
                  stroke-opacity={opacity(e)} stroke-linecap="round" />
            <path d={e.d} class="hit" fill="none" stroke="transparent" stroke-width="10">
              <title>{champById[e.a.id]?.name} + {champById[e.b.id]?.name}: {signed(e.pp)}pp ({e.games} games{e.low_data ? ', low data' : ''})</title>
            </path>
          {/each}
        </svg>
        {#each cols as col, i (col.role)}
          <div class="colhead" style="left:{colX(i)}px; width:{CARD_W}px">
            <strong>{col.label}</strong>{#if col.player}<span>{col.player.split('#')[0]}</span>{/if}
          </div>
          {#each col.nodes as n (n.id)}
            {@const p = positions[key(col.role, n.id)]}
            <div class="node" class:locked={n.locked} class:unavailable={n.unavailable}
                 class:hot={hovered && hovered.role === col.role && hovered.id === n.id}
                 style="left:{p.x}px; top:{p.y - ROW_H / 2 + 3}px; width:{CARD_W}px; height:{ROW_H - 6}px"
                 role="button" tabindex="0" onclick={() => toggleLock(n)} onkeydown={(e) => e.key === 'Enter' && toggleLock(n)}
                 onmouseenter={() => (hovered = { role: col.role, id: n.id })} onmouseleave={() => (hovered = null)}
                 title={n.unavailable ? `${n.name}: banned or picked` : n.locked ? (draftLocked(col.role) ? 'Locked in champ select' : 'Click to unlock') : 'Click to lock'}>
              <span class="icon"><Champ champ={n} size={ICON} banned={n.unavailable} />
                {#if n.tier}<span class="tier {n.tier}" title={n.tier}>{n.tier[0].toUpperCase()}</span>{/if}</span>
              <span class="meta">
                <span class="name" title={n.badge?.text || null}>{n.locked ? '🔒 ' : ''}{n.name}{#if n.badge?.text}<small class="nbadge"> {n.badge.text}</small>{/if}</span>
                <span class="chips">
                  {#each n.chips as c}
                    <span class="chip" class:pos={c.pp > WEAK_EDGE_PP} class:neg={c.pp < -WEAK_EDGE_PP} class:low={c.low_data}
                          title="{LABELS[c.role]} lock: {signed(c.pp)}pp{c.low_data ? ' (low data)' : ''}">{c.label} {signed(c.pp)}</span>
                  {/each}
                  {#if !n.tier && !n.locked}<span class="muted small">off-pool</span>{/if}
                </span>
              </span>
              {#if !n.locked}<button class="x" title="Remove" onclick={(e) => remove(n, e)}>×</button>{/if}
            </div>
          {/each}
          {#if !locks[col.role] && !draftLocked(col.role) && col.nodes.length < 15}
            <form class="add" style="left:{colX(i)}px; top:{graphH - FOOT_H + 6}px; width:{CARD_W}px"
                  onsubmit={(e) => { e.preventDefault(); add(col.role); }}>
              <input list="champ-names" placeholder="+ add champion" bind:value={adding[col.role]} onchange={() => add(col.role)} />
            </form>
          {/if}
        {/each}
        <datalist id="champ-names">{#each data.champions as c}<option value={c.name}></option>{/each}</datalist>
      </div>

      <aside>
        <section>
          <h2>{hovered && hoverTrios.length ? `Trios with ${hoveredNode?.name}` : 'Top trios'}
            <small>{data.trios_total} at ≥ {signed(threshold)}pp</small></h2>
          {#if roles.length < 3}<p class="muted small">Pick three or more roles to see trios.</p>{/if}
          <ol class="trios">
            {#each panelTrios.slice(0, 10) as t}
              <li class:weak={t.weak_pair}>
                <span class="icons">{#each t.champs as c}<Champ champ={champById[c.id]} size={28} />{/each}</span>
                <span class="tmeta">
                  <span>{t.champs.map((c) => c.name).join(' · ')}</span>
                  <span class="muted small">{t.players.filter(Boolean).join(' · ') || '—'}</span>
                </span>
                <span class="tvals">
                  <strong class="tval">{signed(t.pp)}</strong>
                  <span class="small" class:warn={t.weak_pair} class:muted={!t.weak_pair}
                        title="Weakest pair in this trio">{t.weak_pair ? '⚠ ' : ''}min {signed(t.weakest_pp)}</span>
                </span>
              </li>
            {:else}
              {#if roles.length >= 3}<li class="muted small">No trio reaches {signed(threshold)}pp.</li>{/if}
            {/each}
          </ol>
        </section>

        <section>
          <h2>Locked team synergy</h2>
          {#if data.locked_team.pairs.length}
            {#each data.locked_team.pairs as p}
              <div class="pair"><span>{champById[p.a.id]?.name} + {champById[p.b.id]?.name}</span>
                <span class="val" class:pos={p.pp > WEAK_EDGE_PP} class:neg={p.pp < -WEAK_EDGE_PP}>{signed(p.pp)}pp{p.low_data ? '*' : ''}</span></div>
            {/each}
            <div class="pair total"><span>Total</span><span>{signed(data.locked_team.total_pp)}pp</span></div>
          {:else}
            <p class="muted small">Click champions to lock them (two or more for pair totals).</p>
          {/if}
        </section>

        <section>
          <h2>Damage profile <small>locked{hoveredNode && !hoveredNode.locked ? ' + hovered' : ''}</small></h2>
          {#if teamDamage}
            <div class="dmgbar" title="Physical {pct(teamDamage.phys)} · Magic {pct(teamDamage.magic)} · True {pct(teamDamage.true)}">
              <span class="phys" style="flex-grow:{teamDamage.phys}"></span><span class="magic" style="flex-grow:{teamDamage.magic}"></span><span class="true" style="flex-grow:{teamDamage.true}"></span>
            </div>
            <div class="dmglegend"><span><i class="phys"></i>Physical {pct(teamDamage.phys)}</span><span><i class="magic"></i>Magic {pct(teamDamage.magic)}</span><span><i class="true"></i>True {pct(teamDamage.true)}</span></div>
            {#if damageWarning}<p class="warn">⚠ {damageWarning}</p>{/if}
            {#each damageChamps as c}
              <div class="dmgrow" class:preview={c.preview}>
                <span>{c.name}</span>
                {#if c.damage}
                  <span class="mini"><span class="phys" style="flex-grow:{c.damage.phys}"></span><span class="magic" style="flex-grow:{c.damage.magic}"></span><span class="true" style="flex-grow:{c.damage.true}"></span></span>
                {:else}<span class="muted small">no data</span>{/if}
              </div>
            {/each}
          {:else}
            <p class="muted small">Lock or hover champions to see their damage mix.</p>
          {/if}
        </section>

        <p class="muted small legend">
          <span class="sw pos"></span>positive <span class="sw neg"></span>negative <span class="sw"></span>within ±0.5pp ·
          dashed = under {data.low_data_games} games · gold = trio through the hovered champion ·
          tier badge M / C / L = main / comfortable / learning ·
          data: {data.bracket === 'all' ? 'all brackets' : data.bracket}, patch {data.window ?? '—'}
        </p>
      </aside>
    </div>
  {:else if !error}
    <p class="muted">Loading…</p>
  {/if}
</main>

<style>
  .page { padding: 10px 20px 20px; }
  .controls { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; margin-bottom: 8px; font-size: 14px; }
  h1 { font-size: 26px; margin: 0 8px 0 0; }
  .group { display: inline-flex; gap: 4px; }
  button { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
  .role.on { background: #3d3214; border-color: var(--gold); color: var(--gold); }
  .slider { display: inline-flex; align-items: center; gap: 8px; }
  .slider strong { min-width: 52px; font-variant-numeric: tabular-nums; }
  label { display: inline-flex; align-items: center; gap: 6px; color: #c9d1d9; }
  select, input:not([type]) { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
  .reset { margin-left: auto; }
  .error { color: var(--red); }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
  .warn { color: var(--warn); }

  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 16px; align-items: start; }
  .graph { position: relative; background: var(--panel); border-radius: 8px; overflow: hidden; }
  svg { position: absolute; inset: 0; }
  .hit { pointer-events: stroke; cursor: help; }
  .colhead { position: absolute; top: 10px; display: flex; gap: 8px; align-items: baseline; font-size: 15px; }
  .colhead span { color: var(--muted); font-size: 13px; }
  .node { position: absolute; display: flex; align-items: center; gap: 8px; padding: 0 6px; background: var(--panel-2);
          border: 1px solid var(--line); border-radius: 8px; cursor: pointer; user-select: none; }
  .node:hover, .node.hot { border-color: var(--gold); }
  .node.locked { border-color: var(--gold); background: #2a2412; }
  .node.unavailable { opacity: 0.45; cursor: not-allowed; }
  .icon { position: relative; flex: none; }
  .tier { position: absolute; right: -4px; bottom: -4px; width: 16px; height: 16px; border-radius: 50%; font-size: 10px; font-weight: 700;
          display: grid; place-items: center; background: #0d1117; border: 1px solid var(--line); }
  .tier.main { color: var(--gold); border-color: var(--gold); }
  .tier.comfortable { color: #c9d1d9; }
  .tier.learning { color: var(--muted); }
  .meta { display: flex; flex-direction: column; min-width: 0; gap: 2px; }
  .name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .nbadge { font-size: 11px; color: var(--muted); }
  .chips { display: flex; gap: 3px; flex-wrap: nowrap; }
  .chip { font-size: 11px; padding: 0 4px; border-radius: 4px; background: #2d3540; color: #c9d1d9; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .chip.pos { background: #0f3d2e; color: #4fd1a1; }
  .chip.neg { background: #3d2e0a; color: #e0a640; }
  .chip.low { outline: 1px dashed #5b6573; }
  .x { position: absolute; right: 4px; top: 2px; padding: 0 5px; line-height: 16px; font-size: 14px; background: none; border: none; color: var(--muted); visibility: hidden; }
  .node:hover .x { visibility: visible; }
  .add { position: absolute; margin: 0; }
  .add input { width: 100%; font-size: 13px; }

  aside { display: flex; flex-direction: column; gap: 12px; }
  section { background: var(--panel); border-radius: 8px; padding: 12px; }
  h2 { font-size: 16px; margin: 0 0 8px; }
  h2 small { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 6px; }
  .trios { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .trios li { display: flex; align-items: center; gap: 8px; }
  .trios li.weak .icons { outline: 1px dashed var(--warn); outline-offset: 2px; border-radius: 4px; }
  .icons { display: inline-flex; gap: 2px; flex: none; }
  .tmeta { display: flex; flex-direction: column; font-size: 13px; min-width: 0; flex: 1; }
  .tmeta > span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tvals { display: flex; flex-direction: column; align-items: flex-end; flex: none; font-variant-numeric: tabular-nums; }
  .tval { color: #4fd1a1; }
  .pair { display: flex; justify-content: space-between; font-size: 14px; padding: 2px 0; }
  .pair.total { border-top: 1px solid var(--line); margin-top: 4px; padding-top: 4px; font-weight: 600; }
  .val.pos { color: #4fd1a1; }
  .val.neg { color: #e0a640; }
  .dmgbar { display: flex; height: 14px; border-radius: 4px; overflow: hidden; gap: 2px; }
  .mini { display: flex; height: 8px; width: 160px; border-radius: 3px; overflow: hidden; gap: 1px; }
  .phys { background: #d95926; }
  .magic { background: #3987e5; }
  .true { background: #199e70; }
  .dmglegend { display: flex; gap: 12px; font-size: 13px; color: #c9d1d9; margin: 6px 0; }
  .dmglegend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
  .dmgrow { display: flex; justify-content: space-between; align-items: center; font-size: 13px; padding: 2px 0; }
  .dmgrow.preview { color: var(--muted); font-style: italic; }
  .legend { line-height: 1.6; }
  .sw { display: inline-block; width: 14px; height: 3px; background: #5b6573; vertical-align: middle; margin: 0 3px 0 6px; }
  .sw.pos { background: #1baf7a; }
  .sw.neg { background: #c98500; }
</style>
