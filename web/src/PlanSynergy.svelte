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
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';
  import Table from './ui/Table.svelte';
  import Stat from './ui/Stat.svelte';
  import Tag from './ui/Tag.svelte';

  let { locks = $bindable({}), onplan = () => {} } = $props();

  const ROLES = ['top', 'jungle', 'mid', 'adc', 'support'];
  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const BRACKETS = ['all', 'iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald'];
  const ROW_H = 46, ICON = 34, HEAD_H = 40, FOOT_H = 36, PAD = 8;
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
  let sort = $state({ key: 'overall_pp', dir: 'desc' });
  let showAll = $state(false);

  $effect(() => {
    try {
      localStorage.setItem('prep-synergy', JSON.stringify({ scope, view, threshold, columns, useDraft }));
    } catch { /* storage unavailable: settings just aren't remembered */ }
  });

  let timer;
  let first = true;
  $effect(() => {
    const body = JSON.stringify({ scope, view, threshold_pp: threshold, locks,
                                  columns: Object.fromEntries(ROLES.filter((r) => columns[r]?.length).map((r) => [r, columns[r]])),
                                  use_draft: useDraft });
    clearTimeout(timer);
    if (first) { first = false; load(body); return; } // at once on open, so the page appears with it (loading.js)
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
  const tone = (v) => (v > NEUTRAL ? 'up' : v < -NEUTRAL ? 'down' : null);

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
  // in-lane cards share the width, leaving room between columns for the lines
  const CARD_W = $derived(Math.max(104, Math.min(178, (width - 2 * PAD) / ROLES.length - 36)));
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

  // ---------- tables ----------
  const num = (key, label, extra = {}) => ({ key, label, num: true, width: '72px', format: (v) => signed(v), ...extra });
  const COMPLETION_COLUMNS = [
    { key: 'add', label: 'Add', value: (r) => r.champ.name },
    num('gain_pp', 'Gain', { tone, title: 'Overall gain if added to the plan, pp' }), num('stat_pp', 'Stat'), num('style_pp', 'Style'),
    num('team_pp', 'Team'), num('overall_pp', 'Overall'), { key: 'why', label: 'Why' },
  ];
  const PAIR_COLUMNS = [
    { key: 'combo', label: 'Combo', sortable: true, value: (r) => `${r.a.name} + ${r.b.name}` },
    num('stat_pp', 'Stat', { sortable: true, format: (v, r) => `${signed(v)}${r.low_data ? '*' : ''}` }),
    num('style_pp', 'Style', { sortable: true }), num('overall_pp', 'Overall', { sortable: true, tone }),
    num('games', 'Games', { sortable: true, format: (v) => v.toLocaleString() }), { key: 'why', label: 'Why' },
  ];
  const plan = $derived(data?.plan);
  const pct = (x) => `${Math.round(x * 100)}%`;
  const BARS = [['engage', 'Engage'], ['frontline', 'Frontline'], ['peel', 'Peel'], ['poke', 'Poke'], ['scaling', 'Scaling']];
</script>

<Toolbar label="Plan options">
  <ToolGroup label="Scope"><Segmented label="Scope" bind:value={scope} options={[['lane', 'In-lane'], ['teamfight', 'Teamfight']]} /></ToolGroup>
  <ToolGroup label="View"><Segmented label="View" bind:value={view} options={[['overall', 'Overall'], ['stat', 'Stat'], ['style', 'Style'], ['team', 'Team']]} /></ToolGroup>
  <ToolGroup label="Threshold">
    <input class="range" type="range" min="0" max="3" step="0.1" bind:value={threshold} aria-label="Threshold: hide lines weaker than this" />
    <span class="mono val">{signed(threshold)}pp</span>
  </ToolGroup>
  <ToolGroup label="Data">
    <Field label="Synergy data bracket" hideLabel width="132px" value={data?.synergy_bracket ?? 'all'} onchange={(e) => setBracket(e.target.value)}
           options={BRACKETS.map((b) => [b, b === 'all' ? 'All brackets' : b[0].toUpperCase() + b.slice(1)])} />
  </ToolGroup>
  <ToolGroup><Field type="checkbox" label={useDraft ? `Use current draft (${data?.draft?.live ? 'live' : 'no champ select'})` : 'Use current draft'} bind:checked={useDraft} /></ToolGroup>
  <ToolGroup end><Button variant="ghost" onclick={reset} title="Unlock every champion and restore the pools' columns">Reset</Button></ToolGroup>
</Toolbar>
{#if error}<p class="error" role="alert">⚠ {typeof error === 'string' ? error : JSON.stringify(error)}</p>{/if}

{#if data}
  <div class="top" class:lane={!ring}>
    <div class="graph" bind:clientWidth={width} style="height:{graphH}px">
      <svg width={width} height={graphH} aria-label="Pair synergy edges">
        {#each edges as e (e.k)}
          <path d={e.d} fill="none" stroke={colour(e.value_pp)} stroke-width={e.w} stroke-dasharray={e.low_data ? '5 4' : null}
                stroke-opacity={opacity(e)} stroke-linecap="round" />
          <path d={e.d} class="hit" fill="none" stroke="transparent" stroke-width="9"><title>{tip(e)}</title></path>
        {/each}
      </svg>
      {#if view === 'team'}<p class="teamnote">Team has no lines: it is in the plan.</p>{/if}
      {#snippet addBox(col)}
        <input list="prep-champ-names" placeholder="+ add champion" aria-label="Add a champion to {col.label}" bind:value={adding[col.role]} onchange={() => add(col.role)} />
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
                 style="left:{p.x}px; top:{p.y}px" role="button" tabindex="0" aria-label={ringTitle(n)} aria-pressed={n.locked}
                 use:arcMove={{ x: p.x, y: p.y, cx: ring.cx, cy: ring.cy, width, base: 'translate(-50%, -50%)' }}
                 onclick={() => !n.unavailable && toggleLock(col.role, n.id)}
                 onkeydown={(e) => e.key === 'Enter' && !n.unavailable && toggleLock(col.role, n.id)}
                 onmouseenter={() => (hovered = { role: col.role, id: n.id })} onmouseleave={() => (hovered = null)}
                 title={ringTitle(n)}>
              <Champ champ={n} size={ring.icon} banned={n.unavailable} />
              {#if n.tier}<span class="tier mono {n.tier}" title={n.tier}>{n.tier[0].toUpperCase()}</span>{/if}
              {#if n.gain_pp != null}<span class="gain mono {cls(n.gain_pp)}" title="Overall gain if added to the plan">{signed(n.gain_pp)}</span>{/if}
              {#if !n.locked}<button class="x" title="Remove {n.name}" aria-label="Remove {n.name} from {col.label}" onclick={(e) => remove(n, e)}>×</button>{/if}
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
               role="button" tabindex="0" aria-pressed={n.locked} onclick={() => !n.unavailable && toggleLock(col.role, n.id)}
               onkeydown={(e) => e.key === 'Enter' && !n.unavailable && toggleLock(col.role, n.id)}
               onmouseenter={() => (hovered = { role: col.role, id: n.id })} onmouseleave={() => (hovered = null)}
               title={(n.unavailable ? `${n.name}: banned or picked` : n.locked ? 'Click to unlock' : `Lock ${n.name} into the plan`)
                      + (n.badge?.text ? `\n${n.badge.text}` : '')}>
            <span class="icon"><Champ champ={n} size={ICON} banned={n.unavailable} />
              {#if n.tier}<span class="tier mono {n.tier}" title={n.tier}>{n.tier[0].toUpperCase()}</span>{/if}</span>
            <span class="meta">
              <span class="name">{n.name}</span>
              <span class="chips">
                {#if n.locked}<Tag bare>in plan</Tag>{/if}
                {#if n.gain_pp != null}<span class="gain mono {cls(n.gain_pp)}" title="Overall gain if added to the plan">{signed(n.gain_pp)}</span>{/if}
                {#if n.untagged}<Tag bare warn title="No judgement tags: measured traits only">untagged</Tag>{/if}
                {#if !n.tier && !n.locked}<Tag bare>off-pool</Tag>{/if}
              </span>
            </span>
            {#if !n.locked}<button class="x" title="Remove from this column" aria-label="Remove {n.name} from {col.label}" onclick={(e) => remove(n, e)}>×</button>{/if}
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
      <p class="legend">
        <span class="sw pos"></span>positive <span class="sw neg"></span>negative <span class="sw"></span>within ±0.5pp ·
        dashed = low data · tier M / C / L · {data.synergy_bracket === 'all' ? 'all brackets' : data.synergy_bracket}, patch {patchLabel(data.window) ?? '—'}
      </p>
    </div>

    <aside class="plan" aria-label="Plan">
      <h2>Plan</h2>
      {#if plan.champs.length}
        <div class="plancols"><div>
        <div class="locked">
          {#each plan.champs as c}
            <button class="lockchip" title="Unlock {c.name}" onclick={() => toggleLock(c.role, c.id)}>
              <Champ champ={c} size={28} /> <span>{c.name}<small>{LABELS[c.role]}</small></span><span class="un" aria-hidden="true">×</span></button>
          {/each}
        </div>
        <div class="numbers">
          <Stat value={signed(plan.team.overall_pp)} unit="pp" label="Overall" tone={tone(plan.team.overall_pp)} />
          <Stat size="sm" value={signed(plan.team.stat_total_pp)} label="Stat" tone={tone(plan.team.stat_total_pp)} />
          <Stat size="sm" value={signed(plan.team.style_total_pp)} label="Style" tone={tone(plan.team.style_total_pp)} />
          <Stat size="sm" value={plan.team.show_team ? signed(plan.team.team_weighted_pp) : null} label="Team"
                tone={plan.team.show_team ? tone(plan.team.team_weighted_pp) : null} note={plan.team.show_team ? '' : 'from 3 roles'} />
        </div>
        <p class="cap">pp of win chance; Stat, Style and Team add up to Overall. Team is shown at its Overall weight
          (team model <span class="mono">{signed(plan.team.team_pp)}pp</span> vs an average comp).</p>
        {#if plan.team.identity.label}
          <p class="ident"><span class="label-caps">Identity</span> <strong>{plan.team.identity.name}</strong> <span class="mono">{pct(plan.team.identity.coherence)}</span></p>
        {/if}
        </div><div>
        <div class="dmg">
          <div class="dmgbar" role="img" aria-label="Damage: physical {pct(plan.team.damage.physical)}, magic {pct(plan.team.damage.magic)}, true {pct(plan.team.damage.true)}">
            <span class="phys" style="flex-grow:{plan.team.damage.physical}"></span><span class="magic" style="flex-grow:{plan.team.damage.magic}"></span><span class="true" style="flex-grow:{plan.team.damage.true}"></span>
          </div>
          <div class="dmglegend"><span><i class="phys"></i><span class="mono">{pct(plan.team.damage.physical)}</span> AD</span><span><i class="magic"></i><span class="mono">{pct(plan.team.damage.magic)}</span> AP</span><span><i class="true"></i><span class="mono">{pct(plan.team.damage.true)}</span> true</span></div>
        </div>
        <div class="bars">
          {#each BARS as [k, l]}
            <div class="bar"><span>{l}</span><span class="track"><span style="width:{pct(plan.team.bars[k] ?? 0)}"></span></span><span class="mono">{pct(plan.team.bars[k] ?? 0)}</span></div>
          {/each}
        </div>
        {#if plan.team.show_team && plan.team.team_rules.length}
          <h3 class="label-caps">Team model</h3>
          <div class="rules">{#each plan.team.team_rules as r}<div class="rule"><span>{r.note}</span><span class="mono {cls(r.pp)}">{signed(r.pp)}</span></div>{/each}</div>
        {/if}
        {#if plan.pairs.length}
          <h3 class="label-caps">Pairs</h3>
          <div class="rules">
            {#each plan.pairs as p}
              <div class="rule" title={p.rules.map((r) => r.label).join('\n')}>
                <span class="pairname">{p.champs[0].name} + {p.champs[1].name}{#if p.core}<Tag>lane</Tag>{/if}</span>
                <span class="mono {cls(p.overall_pp)}">{signed(p.overall_pp)}</span>
              </div>
            {/each}
          </div>
        {/if}
        </div></div>
      {:else}
        <p class="emptyplan">No champions in the plan yet. Click a champion in the graph, or a row in the synergy table, to lock it in.</p>
      {/if}
    </aside>
  </div>

  <section class="tables">
    <h2>Synergy table <span class="sub">click a row to lock it into the plan</span></h2>
    {#if data.table.completions.length}
      <h3 class="label-caps">Best completions of the plan</h3>
      <Table dense sticky={false} columns={COMPLETION_COLUMNS} rows={data.table.completions} key={(r) => `${r.role}:${r.champ.id}`} onrowclick={lockRow}>
        {#snippet cell(row, col, i, text)}
          {#if col.key === 'add'}<span class="combo"><Champ champ={row.champ} size={20} /><strong>{row.champ.name}</strong><span class="pairrole">{LABELS[row.role]}</span></span>
          {:else if col.key === 'why'}<span class="why">{row.why ?? ''}</span>
          {:else}{text(row, col)}{/if}
        {/snippet}
      </Table>
      <h3 class="label-caps second">Core pairs</h3>
    {/if}
    <Table dense sticky={false} columns={PAIR_COLUMNS} rows={data.table.pairs} key={(r) => `${r.pair}:${r.a.id}:${r.b.id}`} bind:sort
           limit={showAll ? null : 20} onrowclick={lockRow} empty="No pairs above the threshold: lower it, or add champions to the columns">
      {#snippet cell(row, col, i, text)}
        {#if col.key === 'combo'}<span class="combo"><Champ champ={row.a} size={20} /><Champ champ={row.b} size={20} /><strong>{row.a.name} + {row.b.name}</strong><span class="pairrole">{row.pair}</span></span>
        {:else if col.key === 'why'}<span class="why">{row.why ?? ''}</span>
        {:else}{text(row, col)}{/if}
      {/snippet}
    </Table>
    <p class="cap">Overall = the pair's part of Overall (its Stat weight moves to Style while it has few games together).
      * = under {data.low_data_games} games.
      {#if data.table.pairs.length > 20}<Button variant="ghost" onclick={() => (showAll = !showAll)}>
        {showAll ? 'Show the top 20' : `Show all ${data.table.pairs.length}`}</Button>{/if}</p>
  </section>
{:else if !error}
  <p class="emptyplan">Loading…</p>
{/if}

<style>
  .range { width: 120px; }
  .val { font-size: 12.5px; color: var(--text-1); min-width: 52px; }
  .error { color: var(--error); font-size: 13px; margin: 0 0 12px; }
  .cap { margin: 8px 0 0; font-size: 12px; color: var(--text-3); }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; }

  /* the ring takes 60%, the plan 40%; one column on narrow windows */
  .top { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 32px; align-items: start; }
  /* in-lane columns need the full width: the plan goes under them, in two columns */
  .top.lane { grid-template-columns: minmax(0, 1fr); gap: 20px; }
  .top.lane .plancols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 32px; }
  .plancols > div + div { margin-top: 16px; }
  .top.lane .plancols > div + div { margin-top: 0; }
  @media (max-width: 1180px) { .top.lane .plancols { grid-template-columns: minmax(0, 1fr); } .top.lane .plancols > div + div { margin-top: 16px; } .top { grid-template-columns: minmax(0, 1fr); gap: 20px; } }
  .graph { position: relative; background: var(--surface-1); border-radius: var(--radius); overflow: hidden; margin-bottom: 28px; }
  svg { position: absolute; inset: 0; }
  .hit { pointer-events: stroke; cursor: help; }
  .teamnote { position: absolute; left: 0; right: 0; bottom: 6px; text-align: center; font-size: 12px; margin: 0; color: var(--text-3); }
  .legend { position: absolute; left: 0; right: 0; top: 100%; margin: 0; }
  .graph { overflow: visible; }
  .legend { padding-top: 6px; font-size: 11px; color: var(--text-3); }
  .sw { display: inline-block; width: 14px; height: 3px; background: var(--hairline-strong); vertical-align: middle; margin: 0 4px 0 6px; }
  .sw:first-child { margin-left: 0; }
  .sw.pos { background: var(--up); }
  .sw.neg { background: var(--down); }

  .colhead { position: absolute; top: 10px; display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
  .colhead strong, .ringhead strong { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-2); }
  .colhead span, .ringhead span { color: var(--text-3); font-size: 12px; }
  .node { position: absolute; display: flex; align-items: center; gap: 8px; padding: 0 6px; background: var(--surface-2);
          border: 1px solid var(--hairline); border-radius: var(--radius-sm); cursor: pointer; user-select: none; }
  .node:hover, .node.hot { border-color: var(--text-3); }
  /* in the plan is a state, not a call to act: light ink, never lime */
  .node.locked { border-color: var(--text-1); background: var(--surface-3); }
  .node.locked .name { font-weight: 600; }
  .node.unavailable, .ringnode.unavailable { opacity: 0.45; cursor: not-allowed; }
  .icon { position: relative; flex: none; }
  .tier { position: absolute; right: -3px; bottom: -3px; min-width: 13px; padding: 0 2px; font-size: 11px; font-weight: 600; line-height: 13px;
          text-align: center; background: var(--surface-1); border-radius: 2px; color: var(--text-2); }
  .tier.main { color: var(--text-1); }
  .tier.learning { color: var(--text-3); }
  .meta { display: flex; flex-direction: column; min-width: 0; gap: 1px; }
  .name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chips { display: flex; align-items: baseline; gap: 6px; }
  .gain { font-size: 11px; color: var(--text-2); white-space: nowrap; }
  .pos { color: var(--up-text); }
  .neg { color: var(--down-text); }
  .x { position: absolute; right: 3px; top: 1px; padding: 0 5px; line-height: 16px; font-size: 14px; background: none; border: none; color: var(--text-3); visibility: hidden; }
  .x:hover { color: var(--text-1); }
  .node:hover .x, .ringnode:hover .x, .node:focus-within .x, .ringnode:focus-within .x { visibility: visible; }
  .add { position: absolute; margin: 0; }
  .graph input { width: 100%; height: 24px; padding: 0 6px; font-size: 12px; color: var(--text-1); background: var(--surface-2);
                 border: 1px solid var(--hairline); border-radius: var(--radius-sm); }
  .graph input::placeholder { color: var(--text-3); }
  .graph input:focus-visible { outline: none; border-color: var(--signal); }

  .ringhead { position: absolute; width: 134px; }
  .ringhead.above { transform: translate(-50%, -100%); text-align: center; }
  .ringhead.left { transform: translate(-100%, -50%); text-align: right; }
  .ringhead.right { transform: translateY(-50%); }
  .ringhead span { margin-left: 6px; }
  .ringhead form { margin: 4px 0 0; }
  .ringnode { position: absolute; transform: translate(-50%, -50%); display: grid; place-items: center; border-radius: var(--radius);
              background: var(--surface-1); cursor: pointer; user-select: none; }
  .ringnode:hover, .ringnode.hot { outline: 1px solid var(--text-3); outline-offset: 1px; z-index: 2; }
  .ringnode.locked { outline: 2px solid var(--text-1); outline-offset: 1px; }
  .ringnode:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; z-index: 2; }
  .ringnode .gain { position: absolute; left: 50%; top: 100%; transform: translate(-50%, -6px); padding: 0 3px; line-height: 13px; font-size: 10px;
                    background: var(--surface-1); border-radius: 2px; }
  .node.isolated-out, .ringnode.isolated-out { opacity: 0.25; }
  .ringnode .x { right: -9px; top: -9px; width: 16px; height: 16px; padding: 0; line-height: 14px; background: var(--surface-2);
                 border: 1px solid var(--hairline-strong); border-radius: 50%; }

  h2 { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 12px; }
  h3 { margin: 20px 0 6px; }
  .tables { margin-top: 28px; }
  .tables h3 { margin: 0 0 4px; }
  .tables h3.second { margin-top: 24px; }
  .combo { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
  .combo strong { font-weight: 600; }
  .pairrole { font-size: 12px; color: var(--text-3); }
  .why { color: var(--text-3); font-size: 12px; }

  .plan { min-width: 0; }
  .locked { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }
  .lockchip { display: inline-flex; align-items: center; gap: 8px; padding: 3px 8px 3px 3px; background: var(--surface-2);
              border: 1px solid var(--hairline); color: var(--text-1); }
  .lockchip:hover { border-color: var(--hairline-strong); background: var(--surface-3); filter: none; }
  .lockchip > span:not(.un) { display: flex; flex-direction: column; align-items: flex-start; font-size: 13px; font-weight: 600; line-height: 1.15; }
  .lockchip small { color: var(--text-3); font-size: 11px; font-weight: 400; }
  .un { color: var(--text-3); font-size: 14px; }
  .numbers { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px 32px; }
  .ident { margin: 16px 0 8px; font-size: 14px; }
  .ident strong { margin: 0 4px; }
  .dmgbar { display: flex; height: 8px; border-radius: 2px; overflow: hidden; gap: 2px; }
  .phys { background: var(--viz-matchup); }
  .magic { background: var(--viz-threat); }
  .true { background: var(--viz-comp); }
  .dmglegend { display: flex; gap: 14px; font-size: 11px; color: var(--text-2); margin: 6px 0 14px; }
  .dmglegend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; }
  .bars { display: flex; flex-direction: column; gap: 6px; }
  .bar { display: grid; grid-template-columns: 72px 1fr 36px; align-items: center; gap: 8px; font-size: 12px; color: var(--text-2); }
  .bar .mono { text-align: right; font-size: 11.5px; color: var(--text-3); }
  .track { height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
  .track span { display: block; height: 100%; background: var(--text-2); transition: width var(--dur-enter) var(--ease-out); }
  .rules { border-top: 1px solid var(--hairline); }
  .rule { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; font-size: 13px; padding: 5px 0; border-bottom: 1px solid var(--hairline); }
  .pairname { display: inline-flex; align-items: baseline; gap: 6px; }
  .rule .mono { font-size: 12.5px; color: var(--text-2); }
  .rule .mono.pos { color: var(--up-text); }
  .rule .mono.neg { color: var(--down-text); }
  .emptyplan { margin: 0; font-size: 13px; color: var(--text-3); max-width: 44ch; }
</style>
