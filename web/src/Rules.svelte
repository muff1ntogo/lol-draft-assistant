<script>
  // Synergy v2 rules (docs/synergy-v2.md, Stage 2). Written rules with a prior effect, fitted to crawled data
  // (ridge regression centred on the priors). Flagged = the data disagrees: more than 1.5pp from the prior,
  // or the other sign. Editing a prior writes data/style_rules.yaml or data/team_rules.yaml and refits.
  import { patchLabel } from './patch.js';

  const PAIRS = ['TOP-JG', 'JG-MID', 'ADC-SUP'];
  let data = $state(null);
  let tab = $state('style');
  let pairFilter = $state('all');
  let onlyFlagged = $state(false);
  let edits = $state({});
  let error = $state({});
  let busy = $state(false);

  const load = () => fetch('/api/rules').then((r) => r.json()).then((d) => (data = d));
  load();

  const fmt = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}`);
  const when = (r) => Object.entries(r.when).map(([role, t]) => `${role} ${t}`).join(' × ');
  function spec(f) {
    if (!f) return '';
    if (f.all) return f.all.map(spec).join(' × ');
    if (f.special) return f.special.replaceAll('_', ' ');
    if (f.protect) return `${f.protect} protected by ${f.by.join(' / ')}`;
    if (f.sum) return `amount of ${f.sum} (full at ${f.cap})`;
    if (f.lack) return `shortfall of ${f.lack} (below ${f.need})`;
    if (f.excess) return `${f.excess} beyond ${f.over}`;
    if (f.max) return `best ${f.max}`;
    if (f.mean) return `average ${f.mean}`;
    return JSON.stringify(f);
  }
  const rows = $derived.by(() => {
    if (!data) return [];
    let list = tab === 'style' ? data.style : data.team;
    if (tab === 'style' && pairFilter !== 'all') list = list.filter((r) => r.pair === pairFilter);
    if (onlyFlagged) list = list.filter((r) => r.flagged);
    return list;
  });
  const flaggedCount = (list) => list.filter((r) => r.flagged).length;

  async function save(r) {
    const value = Number(edits[r.id]);
    if (!Number.isFinite(value)) return;
    busy = true;
    const res = await fetch('/api/rules', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: tab, id: r.id, prior_pp: value }),
    });
    const body = await res.json();
    busy = false;
    if (!res.ok) { error[r.id] = body.detail; return; }
    delete error[r.id];
    delete edits[r.id];
    await load(); // every rule was refitted
  }
</script>

<main class="page">
  <h1>Rules <small>style and team rules, priors vs data</small> <a class="back" href="#/settings">← Settings</a></h1>
  {#if data}
    <div class="bar">
      <div class="group">
        <button class:on={tab === 'style'} onclick={() => (tab = 'style')}>Pair style ({data.style.length})</button>
        <button class:on={tab === 'team'} onclick={() => (tab = 'team')}>Team ({data.team.length})</button>
      </div>
      {#if tab === 'style'}
        <div class="group">
          <button class:on={pairFilter === 'all'} onclick={() => (pairFilter = 'all')}>All pairs</button>
          {#each PAIRS as p}<button class:on={pairFilter === p} onclick={() => (pairFilter = p)}>{p}</button>{/each}
        </div>
      {/if}
      <label><input type="checkbox" bind:checked={onlyFlagged} /> Flagged only
        ({flaggedCount(tab === 'style' ? data.style : data.team)})</label>
      <span class="muted small right">
        Window {patchLabel(data.window) ?? '—'} ·{data.matches.toLocaleString()} matches · prior SD {data.prior_sd_pp}pp
      </span>
    </div>
    <p class="muted small">
      Effects are in percentage points at full strength (both traits at maximum). The fit starts from the prior and
      moves only as far as the data supports, so rules with little support stay near what's written.
      {#if tab === 'style'}Support = games of champion pairs the rule applies to.{:else}Support = matches where the
      rule applied to either team. Cross rules multiply our feature by theirs.{/if}
    </p>
    <table>
      <thead><tr>
        {#if tab === 'style'}<th>Pair</th><th>When</th>{:else}<th>Rule</th><th>Feature</th>{/if}
        <th>Note</th><th class="num">Prior</th><th class="num">Fitted</th><th class="num">Support</th><th></th>
      </tr></thead>
      <tbody>
        {#each rows as r (r.id)}
          <tr class:flagged={r.flagged}>
            {#if tab === 'style'}
              <td class="nowrap">{r.pair}</td><td class="mono">{when(r)}</td>
            {:else}
              <td class="mono">{r.id}{#if r.cross}<span class="tag">vs</span>{/if}</td>
              <td class="small">{#if r.cross}ours: {spec(r.ours)} × theirs: {spec(r.theirs)}{:else}{spec(r.feature)}{/if}</td>
            {/if}
            <td>{r.note}</td>
            <td class="num">
              <input type="number" step="0.1" value={edits[r.id] ?? r.prior_pp}
                     oninput={(e) => (edits[r.id] = e.target.value)} />
            </td>
            <td class="num" class:pos={r.fitted_pp > 0} class:neg={r.fitted_pp < 0}>{fmt(r.fitted_pp)}</td>
            <td class="num">{r.support.toLocaleString()}</td>
            <td class="nowrap">
              {#if r.flagged}<span class="tag warn" title="More than 1.5pp from the prior, or the other sign">disagrees</span>{/if}
              {#if edits[r.id] != null && Number(edits[r.id]) !== r.prior_pp}
                <button class="primary" disabled={busy} onclick={() => save(r)}>Save</button>
              {/if}
              {#if error[r.id]}<div class="err">{error[r.id]}</div>{/if}
            </td>
          </tr>
        {:else}
          <tr><td colspan="7" class="muted">Nothing here.</td></tr>
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
  .group { display: inline-flex; gap: 4px; }
  .right { margin-left: auto; }
  button { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
           border-radius: 6px; padding: 3px 10px; cursor: pointer; }
  button.on { border-color: var(--gold); color: var(--gold); }
  button.primary { background: var(--gold); border-color: var(--gold); color: var(--on-gold); font-weight: 600; }
  button:disabled { opacity: 0.5; }
  input[type='number'] { font: inherit; font-size: 13px; width: 64px; text-align: right; color: var(--text);
                         background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 2px 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; background: var(--panel); border-radius: 8px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--line); font-size: 13px; }
  td { padding: 4px 8px; border-bottom: 1px solid var(--panel-2); vertical-align: middle; }
  tr.flagged td { background: var(--gold-bg); }
  .num { text-align: right; white-space: nowrap; }
  .pos { color: var(--up); }
  .neg { color: var(--down); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
  .nowrap { white-space: nowrap; }
  .tag { font-size: 11px; padding: 0 5px; border-radius: 4px; background: var(--line); color: var(--text-2); margin-left: 6px; }
  .tag.warn { background: var(--down-bg); color: var(--down-text); }
  .err { color: var(--red); font-size: 12px; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
</style>
