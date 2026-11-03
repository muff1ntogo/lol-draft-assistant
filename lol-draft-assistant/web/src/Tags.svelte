<script>
  // Judgement tags review (docs/synergy-v2.md, Stage 1b). Each champion-role is rated 0-3 on eight traits that
  // match stats can't measure. The first pass was drafted by Claude (unreviewed: half weight in every model);
  // edits are written back to data/champion_tags.yaml. Measured traits are shown for context, read-only.
  import Champ from './Champ.svelte';

  const LABELS = { engage: 'Engage', disengage: 'Disengage', poke: 'Poke', dive: 'Dive', pick: 'Pick',
                   splitpush: 'Split', enchanter: 'Enchanter', hypercarry: 'Hypercarry' };
  const MEASURED = [['cc', 'CC'], ['frontline', 'Front'], ['peel', 'Peel'], ['power_curve', 'Scaling'],
                    ['lane_pressure', 'Lane']];
  let data = $state(null);
  let filter = $state('review');
  let query = $state('');
  let roleFilter = $state('all');
  let edits = $state({}); // "champ:role" -> values
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
      const roles = Object.keys(c.entries);
      if (adding[c.champ_id] && !roles.includes(adding[c.champ_id])) roles.push(adding[c.champ_id]);
      if (!roles.length) {
        if (filter === 'untagged' || filter === 'all') out.push({ c, role: null, entry: null });
        continue;
      }
      for (const role of data.roles.filter((r) => roles.includes(r))) {
        if (roleFilter !== 'all' && role !== roleFilter) continue;
        const entry = c.entries[role] ?? null;
        if (filter === 'review' && entry?.reviewed) continue;
        if (filter === 'reviewed' && !entry?.reviewed) continue;
        if (filter === 'untagged') continue;
        out.push({ c, role, entry });
      }
    }
    return out;
  });

  const key = (c, role) => `${c.champ_id}:${role}`;
  function values(c, role, entry) {
    return edits[key(c, role)] ?? { ...(entry?.values ?? Object.fromEntries(data.tags.map((t) => [t, 0]))) };
  }
  function setValue(c, role, entry, tag, v) {
    edits[key(c, role)] = { ...values(c, role, entry), [tag]: v };
  }
  const changed = (c, role, entry) => {
    const e = edits[key(c, role)];
    return e && (!entry || data.tags.some((t) => e[t] !== entry.values[t]));
  };

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
    delete adding[c.champ_id];
    replace(res);
  }
  const save = (c, role, entry, reviewed) => send(c, role, 'PUT', { values: values(c, role, entry), reviewed });
  const remove = (c, role) => send(c, role, 'DELETE');
  const fmt = (v) => (v == null ? '—' : Math.round(v * 100));
</script>

<main class="page">
  <h1>Champions <small>tags, fight style and timing per role</small> <a class="back" href="#/prep">← Prep</a></h1>
  {#if data}
    <div class="bar">
      <div class="progress" title="{data.counts.reviewed} of {data.counts.entries} reviewed">
        <span style="width:{data.counts.entries ? (100 * data.counts.reviewed) / data.counts.entries : 0}%"></span>
      </div>
      <span>{data.counts.reviewed} / {data.counts.entries} reviewed · {data.counts.untagged} untagged</span>
      <div class="group">
        {#each [['review', 'To review'], ['reviewed', 'Reviewed'], ['untagged', 'Untagged'], ['all', 'All']] as [k, l]}
          <button class:on={filter === k} onclick={() => (filter = k)}>{l}</button>
        {/each}
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
    </p>
    <table>
      <thead><tr><th></th><th>Champion</th><th>Role</th>
        {#each data.tags as t}<th class="num">{LABELS[t]}</th>{/each}
        <th>Style</th><th>Measured</th><th></th></tr></thead>
      <tbody>
        {#each rows as { c, role, entry } (key(c, role))}
          {@const v = role ? values(c, role, entry) : null}
          {@const m = role ? c.measured[role] : null}
          <tr class:reviewed={entry?.reviewed}>
            <td><Champ champ={c.champ} size={30} /></td>
            <td><strong>{c.name}</strong>
              {#if c.untagged && !role}<span class="tag untagged">untagged</span>{/if}
              {#if entry && !entry.reviewed}<span class="tag">draft</span>{/if}</td>
            <td>{#if role}{data.role_labels[role]}{:else}
              <select onchange={(ev) => (adding[c.champ_id] = ev.target.value)}>
                <option value="">Add role…</option>
                {#each data.roles as r}<option value={r}>{data.role_labels[r]}</option>{/each}
              </select>{/if}</td>
            {#each data.tags as t}
              <td class="num">{#if role}
                <select class="lvl l{v[t]}" value={v[t]} onchange={(ev) => setValue(c, role, entry, t, +ev.target.value)}>
                  {#each [0, 1, 2, 3] as n}<option value={n}>{n}</option>{/each}
                </select>{/if}</td>
            {/each}
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
            <td class="actions">
              {#if role}
                {#if changed(c, role, entry)}
                  <button class="primary" onclick={() => save(c, role, entry, true)}>Save</button>
                {:else if entry && !entry.reviewed}
                  <button onclick={() => save(c, role, entry, true)}>Looks right</button>
                {/if}
                {#if entry}<button class="link" onclick={() => remove(c, role)}>Remove</button>{/if}
              {/if}
              {#if error[key(c, role)]}<div class="err">{error[key(c, role)]}</div>{/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan={data.tags.length + 6} class="muted">Nothing here.</td></tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
</main>

<style>
  .page { max-width: 1500px; margin: 0 auto; padding: 20px 28px 60px; }
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
  .tm { margin-left: 6px; font-size: 11px; padding: 0 4px; border-radius: 3px; background: #2d3540; }
  .tm.early { color: #e0a640; }
  .tm.late { color: #79b8ff; }
  .measured span { margin-right: 8px; }
  .actions { white-space: nowrap; }
  .err { color: var(--red); font-size: 12px; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
</style>
