<script>
  // Champion style review (docs/comp-style.md). Each champion's team-fight style weights (front to back /
  // dive / poke, adding up to 1) and front-line flag start from a seed; confirm or edit them here.
  // Timing (early / mid / late) comes from crawled game lengths and isn't edited.
  import Champ from './Champ.svelte';

  const ROLE_SHORT = { top: 'Top', jungle: 'Jg', mid: 'Mid', adc: 'ADC', support: 'Sup' };
  let data = $state(null);
  let filter = $state('review');
  let query = $state('');
  let edits = $state({}); // champ id -> {front_to_back, dive, poke, frontline}
  let error = $state({});

  fetch('/api/champions/styles').then((r) => r.json()).then((d) => (data = d));

  const rows = $derived.by(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    let list = data.champions.filter((c) => !q || c.champ.name.toLowerCase().includes(q));
    if (filter === 'review') list = list.filter((c) => !c.reviewed).sort((a, b) => b.unsure - a.unsure);
    else if (filter === 'reviewed') list = list.filter((c) => c.reviewed);
    return list;
  });

  function draft(c) {
    return edits[c.champ.id] ?? { ...c.weights, frontline: c.frontline };
  }
  function setField(c, field, value) {
    edits[c.champ.id] = { ...draft(c), [field]: value };
  }
  const total = (e) => e.front_to_back + e.dive + e.poke;
  const changed = (c) => {
    const e = edits[c.champ.id];
    return e && (e.front_to_back !== c.weights.front_to_back || e.dive !== c.weights.dive || e.poke !== c.weights.poke
                 || e.frontline !== c.frontline);
  };

  async function send(c, method, body) {
    const r = await fetch(`/api/champions/styles/${c.champ.id}`, {
      method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body),
    });
    const res = await r.json();
    if (!r.ok) { error[c.champ.id] = res.detail; return; }
    delete error[c.champ.id];
    delete edits[c.champ.id];
    const i = data.champions.findIndex((x) => x.champ.id === c.champ.id);
    data.champions[i] = res;
    data.reviewed = data.champions.filter((x) => x.reviewed).length;
  }
  const save = (c) => {
    const e = draft(c);
    send(c, 'PUT', { weights: { front_to_back: +e.front_to_back, dive: +e.dive, poke: +e.poke }, frontline: e.frontline });
  };
  const confirm = (c) => send(c, 'PUT', { weights: null, frontline: null });
  const reset = (c) => send(c, 'DELETE');
</script>

<main class="page">
  <h1>Champions <small>team-fight style review</small></h1>
  {#if data}
    <div class="bar">
      <div class="progress" title="{data.reviewed} of {data.total} reviewed">
        <span style="width:{(100 * data.reviewed) / data.total}%"></span>
      </div>
      <span>{data.reviewed} / {data.total} reviewed</span>
      <div class="group">
        <button class:on={filter === 'review'} onclick={() => (filter = 'review')}>To review</button>
        <button class:on={filter === 'reviewed'} onclick={() => (filter = 'reviewed')}>Reviewed</button>
        <button class:on={filter === 'all'} onclick={() => (filter = 'all')}>All</button>
      </div>
      <input placeholder="Search" bind:value={query} />
    </div>
    <p class="muted small">
      Weights say which team-fight style a champion fits and must add up to 1. Front-line = soaks damage at the front.
      "Unsure" champions were seeded mostly from their Data Dragon classes: check those first. Timing comes from
      our crawl (win rate in games over 32 min vs under 25 min); "—" means not enough games yet.
    </p>
    <table>
      <thead><tr><th></th><th>Champion</th><th>Timing</th><th class="num">Front to back</th><th class="num">Dive</th>
        <th class="num">Poke</th><th>Front-line</th><th></th></tr></thead>
      <tbody>
        {#each rows as c (c.champ.id)}
          {@const e = draft(c)}
          <tr class:reviewed={c.reviewed}>
            <td><Champ champ={c.champ} size={32} /></td>
            <td><strong>{c.champ.name}</strong>
              {#if c.unsure && !c.reviewed}<span class="tag unsure">unsure</span>{/if}
              {#if c.source === 'user'}<span class="tag">edited</span>{:else if c.reviewed}<span class="tag">confirmed</span>{/if}
              <div class="muted small">{c.tags.join(', ')}</div></td>
            <td class="timing">
              {#each c.roles as r}
                {@const t = c.timing[r]}
                <span class="chip {t?.label ?? ''}" title={t ? `curve ${t.curve > 0 ? '+' : ''}${t.curve} from ${t.games} games` : 'no data'}>
                  {ROLE_SHORT[r]} {t?.label ? data.timings[t.label] : '—'}</span>
              {/each}
            </td>
            {#each ['front_to_back', 'dive', 'poke'] as k}
              <td class="num"><input type="number" min="0" max="1" step="0.1" value={e[k]}
                                     oninput={(ev) => setField(c, k, +ev.target.value)} /></td>
            {/each}
            <td><input type="checkbox" checked={e.frontline} onchange={(ev) => setField(c, 'frontline', ev.target.checked)} /></td>
            <td class="actions">
              {#if changed(c)}
                <button class="primary" disabled={Math.abs(total(e) - 1) > 0.01} onclick={() => save(c)}
                        title={Math.abs(total(e) - 1) > 0.01 ? `Adds up to ${total(e).toFixed(2)}, must be 1` : ''}>Save</button>
              {:else if !c.reviewed}
                <button onclick={() => confirm(c)}>Looks right</button>
              {/if}
              {#if c.reviewed}<button class="link" onclick={() => reset(c)}>Reset to seed</button>{/if}
              {#if error[c.champ.id]}<div class="err">{error[c.champ.id]}</div>{/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan="8" class="muted">Nothing here.</td></tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
</main>

<style>
  .page { max-width: 1200px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 30px; margin: 8px 0 12px; }
  h1 small { font-size: 16px; color: var(--muted); font-weight: 400; }
  .bar { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
  .progress { width: 220px; height: 8px; background: var(--panel-2); border-radius: 4px; overflow: hidden; }
  .progress span { display: block; height: 100%; background: #1baf7a; }
  .group { display: inline-flex; gap: 4px; margin-left: 12px; }
  button { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
           border-radius: 6px; padding: 3px 10px; cursor: pointer; }
  button.on { border-color: var(--gold); color: var(--gold); }
  button.primary { background: var(--blue); border-color: var(--blue); color: white; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.link { background: none; border: none; color: var(--muted); text-decoration: underline; }
  input:not([type='checkbox']) { font: inherit; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
                                  border-radius: 6px; padding: 3px 6px; }
  .bar input { margin-left: auto; width: 200px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; background: var(--panel); border-radius: 8px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 4px 8px; border-bottom: 1px solid #1f2630; vertical-align: middle; }
  tr.reviewed { opacity: 0.85; }
  .num { text-align: right; }
  td.num input { width: 64px; text-align: right; }
  .tag { font-size: 11px; padding: 0 5px; border-radius: 4px; background: #2d3540; color: #c9d1d9; margin-left: 6px; }
  .tag.unsure { background: #3d2e0a; color: #e0a640; }
  .timing { white-space: nowrap; }
  .chip { font-size: 12px; padding: 1px 6px; border-radius: 4px; background: #2d3540; color: #c9d1d9; margin-right: 4px; }
  .chip.early { background: #3d2e0a; color: #e0a640; }
  .chip.late { background: #12304d; color: #79b8ff; }
  .actions { white-space: nowrap; }
  .err { color: var(--red); font-size: 12px; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
</style>
