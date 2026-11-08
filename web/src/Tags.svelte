<script>
  // Judgement tags review (docs/synergy-v2.md, Stage 1b). Each champion-role is rated 0-3 on eight traits that
  // match stats can't measure. The first pass was drafted by Claude (unreviewed: half weight in every model);
  // edits are written back to data/champion_tags.yaml. Measured traits are shown for context, read-only.
  import Champ from './Champ.svelte';

  const LABELS = { engage: 'Engage', disengage: 'Disengage', poke: 'Poke', dive: 'Dive', pick: 'Pick',
                   splitpush: 'Split', enchanter: 'Enchanter', hypercarry: 'Hypercarry', setup: 'Setup', followup: 'Follow-up',
                   needs_team: 'Needs team' };
  // kit interactions for matchups (docs/matchup-archetypes.md, Stage 2)
  const KIT_LABELS = {
    auto_reliant: ['Autos', 'Damage mainly from auto attacks'],
    auto_denial: ['Anti-auto', 'Blocks, dodges or slows auto attacks'],
    max_hp_damage: ['Max HP', '% max-health or true damage that shreds tanks'],
    hard_gap_close: ['Gap close', 'Reliable gap close through minions or kites'],
    kite: ['Kite', 'Can fight at range and move while doing it'],
    escape: ['Escape', 'Reliable disengage or escape'],
    extend_fight: ['Long fight', 'Forces long fights: pulls, sticky damage'],
    anti_dive: ['Anti-dive', 'Kit built to punish divers or assassins'],
    sustain: ['Sustain', 'Out-heals poke'],
    ability_range: ['Range', 'Long ability range and wave clear'],
  };
  const MEASURED = [['cc', 'CC'], ['frontline', 'Front'], ['peel', 'Peel'], ['power_curve', 'Scaling'],
                    ['lane_pressure', 'Lane']];
  let data = $state(null);
  let filter = $state('review');
  let query = $state('');
  let roleFilter = $state('all');
  let edits = $state({}); // "champ:role" -> values
  let archEdits = $state({}); // "champ:role" -> {archetype, archetype2}
  let kitEdits = $state({}); // "champ:role" -> kit tag -> 0..3
  let view = $state('fight'); // the group under review: fight (synergy tags), kit, or arch (archetypes)
  let error = $state({});
  let adding = $state({}); // champ id -> role being added

  fetch('/api/tags').then((r) => r.json()).then((d) => (data = d));

  // one row per champion-role entry; untagged champions get one empty row
  const rows = $derived.by(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const out = [];
    for (const c of data.champions) {
      if (q && !c.name.toLowerCase().includes(q)) continue;
      // tagged roles, plus archetype-only ones (written or edited: no 0-3 ratings yet)
      const roles = [...new Set([...Object.keys(c.entries), ...Object.keys(c.kit),
        ...Object.keys(c.archetypes).filter((r) => !c.archetypes[r].untagged)])];
      if (adding[c.champ_id] && !roles.includes(adding[c.champ_id])) roles.push(adding[c.champ_id]);
      if (!roles.length) {
        if (filter === 'untagged' || filter === 'all') out.push({ c, role: null, entry: null });
        continue;
      }
      for (const role of data.roles.filter((r) => roles.includes(r))) {
        if (roleFilter !== 'all' && role !== roleFilter) continue;
        const entry = c.entries[role] ?? null;
        if (filter === 'review' && !(inGroup(c, role, entry) && !reviewedIn(c, role, entry))) continue;
        if (filter === 'reviewed' && !(inGroup(c, role, entry) && reviewedIn(c, role, entry))) continue;
        if (filter === 'untagged') continue;
        out.push({ c, role, entry });
      }
    }
    return out;
  });

  const key = (c, role) => `${c.champ_id}:${role}`;
  // does this line have labels in the group under review, and are they reviewed?
  function inGroup(c, role, entry) {
    if (view === 'kit') return !!c.kit[role];
    if (view === 'arch') return !!c.archetypes[role] && !c.archetypes[role].untagged;
    return !!entry;
  }
  function reviewedIn(c, role, entry) {
    if (view === 'kit') return !!c.kit_reviewed[role];
    if (view === 'arch') return !!c.archetypes[role]?.reviewed;
    return !!entry?.reviewed;
  }
  function values(c, role, entry) {
    return edits[key(c, role)] ?? { ...(entry?.values ?? Object.fromEntries(data.tags.map((t) => [t, 0]))) };
  }
  function setValue(c, role, entry, tag, v) {
    edits[key(c, role)] = { ...values(c, role, entry), [tag]: v };
  }
  const changed = (c, role, entry) => {
    const e = edits[key(c, role)];
    return (e && (!entry || data.tags.some((t) => e[t] !== entry.values[t]))) || archEdits[key(c, role)]
      || kitEdits[key(c, role)];
  };
  const kitValues = (c, role) => kitEdits[key(c, role)] ?? c.kit[role] ?? {};
  function setKit(c, role, tag, v) {
    kitEdits[key(c, role)] = { ...kitValues(c, role), [tag]: v };
  }
  // archetype and optional secondary, per role (docs/matchup-archetypes.md)
  function arch(c, role) {
    const a = c.archetypes[role];
    const written = a && !a.untagged;
    return archEdits[key(c, role)] ?? { archetype: written ? a.archetype : '', archetype2: written ? a.archetype2 ?? '' : '' };
  }
  function setArch(c, role, field, v) {
    const next = { ...arch(c, role), [field]: v };
    if (!next.archetype || next.archetype2 === next.archetype) next.archetype2 = '';
    archEdits[key(c, role)] = next;
  }
  const archLabel = (a) => a.replaceAll('_', ' ');

  function replace(res) {
    const i = data.champions.findIndex((x) => x.champ_id === res.champ_id);
    data.champions[i] = res;
    const entries = data.champions.flatMap((x) => Object.values(x.entries));
    data.counts = { champions: data.champions.length, entries: entries.length,
                    reviewed: entries.filter((e) => e.reviewed).length,
                    untagged: data.champions.filter((x) => x.untagged).length };
  }
  async function send(c, role, method, body) {
    const r = await fetch(`/api/tags/${c.champ_id}/${role}`, {
      method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body),
    });
    const res = await r.json();
    if (!r.ok) { error[key(c, role)] = res.detail; return; }
    delete error[key(c, role)];
    delete edits[key(c, role)];
    delete archEdits[key(c, role)];
    delete kitEdits[key(c, role)];
    delete adding[c.champ_id];
    replace(res);
  }
  // Saving marks the groups edited as reviewed; "Looks right" marks the group under review. Others are left alone.
  // A kit is sent only when edited: it applies to all of the champion's lines.
  function save(c, role, entry, looksRight = false) {
    const k = key(c, role);
    const fightEdited = edits[k] && (!entry || data.tags.some((t) => edits[k][t] !== entry.values[t]));
    send(c, role, 'PUT', {
      values: values(c, role, entry), ...arch(c, role),
      kit: kitEdits[k],
      reviewed: !!entry?.reviewed || !!fightEdited || (looksRight && view === 'fight'),
      reviewed_kit: kitEdits[k] || (looksRight && view === 'kit') ? true : undefined,
      reviewed_arch: archEdits[k] || (looksRight && view === 'arch') ? true : undefined,
    });
  }
  const counts = $derived.by(() => {
    if (!data) return { reviewed: 0, total: 0 };
    let reviewed = 0, total = 0;
    for (const c of data.champions) {
      const lines = view === 'kit' ? Object.keys(c.kit)
        : view === 'arch' ? Object.keys(c.archetypes).filter((r) => !c.archetypes[r].untagged)
        : Object.keys(c.entries);
      for (const r of lines) { total++; if (reviewedIn(c, r, c.entries[r])) reviewed++; }
    }
    return { reviewed, total };
  });
  const remove = (c, role) => send(c, role, 'DELETE');
  const fmt = (v) => (v == null ? '—' : Math.round(v * 100));
</script>

<main class="page">
  <h1>Champions <small>tags, fight style and timing per role</small> <a class="back" href="#/settings">← Settings</a></h1>
  {#if data}
    <div class="bar">
      <div class="progress" title="{counts.reviewed} of {counts.total} reviewed">
        <span style="width:{counts.total ? (100 * counts.reviewed) / counts.total : 0}%"></span>
      </div>
      <span>{counts.reviewed} / {counts.total} reviewed · {data.counts.untagged} untagged</span>
      <div class="group">
        {#each [['review', 'To review'], ['reviewed', 'Reviewed'], ['untagged', 'Untagged'], ['all', 'All']] as [k, l]}
          <button class:on={filter === k} onclick={() => (filter = k)}>{l}</button>
        {/each}
      </div>
      <div class="group">
        <button class:on={view === 'fight'} onclick={() => (view = 'fight')}>Fight tags</button>
        <button class:on={view === 'kit'} onclick={() => (view = 'kit')}>Kit tags</button>
        <button class:on={view === 'arch'} onclick={() => (view = 'arch')}>Archetypes</button>
      </div>
      <select bind:value={roleFilter}>
        <option value="all">All roles</option>
        {#each data.roles as r}<option value={r}>{data.role_labels[r]}</option>{/each}
      </select>
      <input placeholder="Search" bind:value={query} />
    </div>
    <p class="muted small">
      0 = not at all, 3 = defining. The tags are the one source of fight style: Style shows the identity they give
      (Engage, Poke, Pick, Protect, Split), used by the badges, comp panels, comp coherence and the Team page.
      Unreviewed tags count at half weight in the synergy and team models (labels read them as written); "Looks right"
      marks them reviewed. Untagged champions fall back to their Data Dragon classes. Timing (Early / Late) and the
      measured traits (percentile in the role, 0–100) come from crawled matches and aren't edited here.
      Archetype is per role, for matchups; a blank one uses the suggestion from the champion's class, shown in the
      list. "guide" marks one named in a matchup guide. Kit tags feed the matchup model; hover a column for its meaning.
      A kit rating belongs to the champion: changing it in one role changes all its roles. Each of Fight tags, Kit tags
      and Archetypes has its own review state; "Looks right" marks only the one you're viewing.
    </p>
    <table>
      <thead><tr><th></th><th>Champion</th><th>Role</th><th>Archetype</th>
        {#if view === 'kit'}
          {#each data.kit_tags as t}<th class="num" title={KIT_LABELS[t][1]}>{KIT_LABELS[t][0]}</th>{/each}
        {:else if view === 'fight'}
          {#each data.tags as t}<th class="num">{LABELS[t]}</th>{/each}
        {/if}
        {#if view === 'fight'}<th>Style</th><th>Measured</th>{/if}<th></th></tr></thead>
      <tbody>
        {#each rows as { c, role, entry } (key(c, role))}
          {@const v = role ? values(c, role, entry) : null}
          {@const m = role ? c.measured[role] : null}
          {@const hasArch = role && c.archetypes[role] && !c.archetypes[role].untagged}
          {@const here = role && inGroup(c, role, entry)}
          {@const isReviewed = !here || reviewedIn(c, role, entry)}
          <tr class:reviewed={here && isReviewed}>
            <td><Champ champ={c.champ} size={30} /></td>
            <td><strong>{c.name}</strong>
              {#if c.untagged && !role}<span class="tag untagged">untagged</span>{/if}
              {#if !isReviewed}<span class="tag">draft</span>{/if}</td>
            <td>{#if role}{data.role_labels[role]}{:else}
              <select onchange={(ev) => (adding[c.champ_id] = ev.target.value)}>
                <option value="">Add role…</option>
                {#each data.roles as r}<option value={r}>{data.role_labels[r]}</option>{/each}
              </select>{/if}</td>
            <td class="arch">{#if role}
              {@const a = arch(c, role)}
              {@const fb = c.archetypes[role]?.untagged ? c.archetypes[role].archetype : null}
              <select aria-label="Archetype" value={a.archetype} onchange={(ev) => setArch(c, role, 'archetype', ev.target.value)}>
                <option value="">{fb ? `(${archLabel(fb)})` : '—'}</option>
                {#each data.archetypes[role] as w}<option value={w}>{archLabel(w)}</option>{/each}
              </select>
              <select aria-label="Secondary archetype" value={a.archetype2} disabled={!a.archetype}
                      onchange={(ev) => setArch(c, role, 'archetype2', ev.target.value)}>
                <option value="">—</option>
                {#each data.archetypes[role].filter((w) => w !== a.archetype) as w}<option value={w}>{archLabel(w)}</option>{/each}
              </select>
              {#if c.archetypes[role]?.source === 'video'}<span class="tag">guide</span>{/if}
            {/if}</td>
            {#if view === 'kit'}
              {#each data.kit_tags as t}
                {@const kv = role ? kitValues(c, role)[t] ?? 0 : 0}
                <td class="num">{#if role}
                  <select class="lvl l{kv}" aria-label={KIT_LABELS[t][0]} value={kv} onchange={(ev) => setKit(c, role, t, +ev.target.value)}>
                    {#each [0, 1, 2, 3] as n}<option value={n}>{n}</option>{/each}
                  </select>{/if}</td>
              {/each}
            {:else if view === 'fight'}
              {#each data.tags as t}
                <td class="num">{#if role}
                  <select class="lvl l{v[t]}" value={v[t]} onchange={(ev) => setValue(c, role, entry, t, +ev.target.value)}>
                    {#each [0, 1, 2, 3] as n}<option value={n}>{n}</option>{/each}
                  </select>{/if}</td>
              {/each}
            {/if}
            {#if view === 'fight'}
            <td class="stylecol small">
              {#if role && c.identity[role]}
                {@const id = c.identity[role]}
                {@const t = c.timing[role]}
                <span title={Object.entries(id.shares).map(([k, v]) => `${data.styles[k]} ${Math.round(v * 100)}%`).join(' · ')}>
                  {id.badge.styles.map((k) => data.styles[k]).join(' / ') || 'Spread'}</span>
                {#if t?.label === 'early' || t?.label === 'late'}<span class="tm {t.label}">{data.timings[t.label]}</span>{/if}
                {#if id.source === 'classes'}<span class="muted">(classes)</span>{/if}
              {/if}
            </td>
            <td class="measured small">
              {#if m}
                {#each MEASURED as [k, l]}<span title="{l}: percentile in role">{l} {fmt(m[k])}</span>{/each}
              {:else if role}<span class="muted">no data</span>{/if}
            </td>
            {/if}
            <td class="actions">
              {#if role}
                {#if changed(c, role, entry)}
                  <button class="primary" onclick={() => save(c, role, entry)}>Save</button>
                {:else if !isReviewed}
                  <button onclick={() => save(c, role, entry, true)}>Looks right</button>
                {/if}
                {#if entry || hasArch}<button class="link" onclick={() => remove(c, role)}>Remove</button>{/if}
              {/if}
              {#if error[key(c, role)]}<div class="err">{error[key(c, role)]}</div>{/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan={view === 'kit' ? data.kit_tags.length + 5 : view === 'arch' ? 5 : data.tags.length + 7} class="muted">Nothing here.</td></tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
</main>

<style>
  .page { max-width: 1700px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 30px; margin: 8px 0 12px; }
  h1 small { font-size: 16px; color: var(--muted); font-weight: 400; }
  .back { font-size: 14px; font-weight: 400; color: var(--muted); margin-left: 12px; }
  .bar { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; flex-wrap: wrap; }
  .progress { width: 220px; height: 8px; background: var(--panel-2); border-radius: 4px; overflow: hidden; }
  .progress span { display: block; height: 100%; background: #1baf7a; }
  .group { display: inline-flex; gap: 4px; }
  button { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
           border-radius: 6px; padding: 3px 10px; cursor: pointer; }
  button.on { border-color: var(--gold); color: var(--gold); }
  button.primary { background: var(--blue); border-color: var(--blue); color: white; }
  button.link { background: none; border: none; color: var(--muted); text-decoration: underline; }
  input, select { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
                  border-radius: 6px; padding: 3px 6px; }
  .bar input { margin-left: auto; width: 200px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; background: var(--panel); border-radius: 8px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 6px; border-bottom: 1px solid var(--line); font-size: 13px; }
  td { padding: 3px 6px; border-bottom: 1px solid #1f2630; vertical-align: middle; }
  tr.reviewed { opacity: 0.85; }
  .num { text-align: center; }
  select.lvl { width: 44px; text-align: center; }
  select.l0 { color: var(--muted); }
  select.l2 { border-color: #2f6fb7; }
  select.l3 { border-color: #2f6fb7; background: #12304d; }
  .tag { font-size: 11px; padding: 0 5px; border-radius: 4px; background: #2d3540; color: #c9d1d9; margin-left: 6px; }
  .tag.untagged { background: #3d2e0a; color: #e0a640; }
  .measured { white-space: nowrap; color: var(--muted); }
  .stylecol { white-space: nowrap; }
  .arch { white-space: nowrap; }
  .arch select { width: 128px; }
  .arch select:disabled { opacity: 0.45; }
  .tm { margin-left: 6px; font-size: 11px; padding: 0 4px; border-radius: 3px; background: #2d3540; }
  .tm.early { color: #e0a640; }
  .tm.late { color: #79b8ff; }
  .measured span { margin-right: 8px; }
  .actions { white-space: nowrap; }
  .err { color: var(--red); font-size: 12px; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
</style>
