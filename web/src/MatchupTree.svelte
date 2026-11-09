<script>
  // Prep: the Matchup tree (docs/matchup-archetypes.md, Stage 7). Per role, each enemy archetype, up to 3 answers (the pool's
  // best if it covers it, then the best outside the pool), what the archetypes alone would suggest, and a flag where the
  // pool's best answer is below -1.5pp.
  import Champ from './Champ.svelte';
  import { patchLabel } from './patch.js';

  let data = $state(null);
  let open = $state({}); // "role:archetype" -> the red chip's detail is showing
  let stop = $state(null); // index into HALF_LIVES
  let saving = $state(false);

  // Small half-lives get more stops: that's where the newest patch starts to outweigh a much bigger older one.
  // The last stop, 0, is no decay: every game counts the same.
  const HALF_LIVES = [0.1, 0.15, 0.25, 0.5, 1, 2, 3, 6, 12, 24, 0];
  const nearest = (h) => (h === 0 ? HALF_LIVES.length - 1
    : HALF_LIVES.reduce((best, x, i) => (x && Math.abs(x - h) < Math.abs(HALF_LIVES[best] - h) ? i : best), 0));
  const counterTitle = (a) => `${a.name} averages ${signed(a.own_pp)}pp into this role's archetypes, and gains ${signed(a.counter_pp)}pp here beyond what the role's other champions gain`;

  async function load() {
    data = await (await fetch('/api/prep/matchup-tree')).json();
    stop = nearest(data.half_life);
  }
  load();

  async function save() {
    saving = true;
    await fetch('/api/prep/matchup-tree/half-life', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                                      body: JSON.stringify({ half_life: HALF_LIVES[stop] }) });
    await load();
    saving = false;
  }

  // Each patch's share of the pooled games: its matches times its weight.
  const shares = $derived.by(() => {
    const rows = (data?.matches ?? []).filter((m) => m.weight != null);
    const total = rows.reduce((sum, m) => sum + m.matches * m.weight, 0);
    return rows.map((m) => ({ ...m, share: total ? (m.matches * m.weight) / total : 0 })).reverse();
  });
  const percent = (x) => (x > 0 && x < 0.01 ? '<1%' : `${Math.round(x * 100)}%`);

  const signed = (x, digits = 1) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`);
  const words = (a) => a.replaceAll('_', ' ');
  const measured = (c) => (c >= 0.5 ? 'measured' : c >= 0.15 ? 'partly measured' : 'archetype estimate');
  const toggle = (k) => (open[k] = !open[k]);
</script>

{#if data}
  <p class="muted source">From {data.matches.map((m) => `${m.matches.toLocaleString()} matches on ${patchLabel(m.patch)}`).join(', ')},
    {data.master ? 'Master, Grandmaster and Challenger Solo/Duo' : 'every tier from Iron to Emerald pooled'},
    {data.half_life === 0 ? 'every game counting the same' : 'newer patches counting more'}.
    {data.master ? 'The Blind pick, Setup and Counters line under each role still reads the main data.' : '"Most picked" is each player\'s tier.'}</p>
  {#if !data.master}
    <p class="waiting">Master+ crawl: {data.master_matches.toLocaleString()} of {data.master_needed.toLocaleString()} matches. This page moves to Master+ once it has them, and shows Iron to Emerald until then.</p>
  {/if}
  <div class="tune">
    <label>{#if HALF_LIVES[stop] === 0}<strong>Every game counts the same</strong>{:else}Older patches halve every <strong>{HALF_LIVES[stop]}</strong> {HALF_LIVES[stop] === 1 ? 'patch' : 'patches'}{/if}
      <input type="range" min="0" max={HALF_LIVES.length - 1} step="1" bind:value={stop} onchange={save} disabled={saving}
             aria-label="Patch half-life" /></label>
    <span class="muted">Left favours new patches. Share of the games used:
      {shares.map((m) => `${patchLabel(m.patch)} ${percent(m.share)} (${m.matches.toLocaleString()} matches × ${percent(m.weight)})`).join(' · ')}</span>
  </div>
  {#each data.roles as role}
    <section class="role">
      <h2>{role.role_label} <small>{data.master ? 'Master+' : role.bracket}</small></h2>
      <div class="chips">
        {#each role.rows.filter((r) => r.chip !== 'none') as r}
          {@const k = `${role.role}:${r.archetype}`}
          <button class="chip {r.chip}" onclick={() => r.chip === 'red' && toggle(k)} title={r.chip === 'red' ? 'Show ban candidates and who answers it' : r.chip === 'grey' ? 'Too few games behind this answer to call it covered or thin' : ''}>
            {words(r.archetype)} <small>{r.chip === 'red' ? 'gap' : r.chip === 'amber' ? 'thin' : r.chip === 'grey' ? 'no data' : 'covered'}</small>
          </button>
        {/each}
      </div>
      {#each role.rows.filter((r) => r.chip === 'red' && open[`${role.role}:${r.archetype}`]) as r}
        <p class="detail">
          <strong>{words(r.archetype)}:</strong>
          ban {r.ban_candidates.map((b) => `${b.name} (${signed(b.pp)}pp)`).join(', ') || '—'}
          {#if r.answering.length}· answered by {r.answering.map(words).join(', ')} champions{/if}
        </p>
      {/each}
      <div class="health small">
        <span class:missing={!role.health.blind.ok}>Blind pick: {role.health.blind.ok ? role.health.blind.name : 'none safe'}</span>
        {#if role.health.setup.applies}<span class:missing={!role.health.setup.ok}>Setup: {role.health.setup.ok ? role.health.setup.name : 'none'}</span>{/if}
        {#if role.health.counter.main}<span class:missing={!role.health.counter.ok}>Counters to {role.health.counter.main}'s worst: {role.health.counter.worst.filter((w) => w.answered).length} of {role.health.counter.worst.length}</span>{/if}
      </div>
      <table>
        <thead><tr><th>Enemy archetype</th><th>Most picked</th><th>Best answers</th><th class="num">Blend</th><th class="num">Lane</th>
          <th class="num">Gold at 15</th><th class="num">Game</th><th class="num" title="What the two archetypes alone suggest">Expected</th><th>Plan</th></tr></thead>
        <tbody>
          {#each role.rows.filter((r) => r.champs.length) as r}
            {@const span = Math.max(1, r.answers.length)}
            {#each r.answers.length ? r.answers : [null] as a, i}
              <tr class:gap={r.gap} class:more={i > 0}>
                {#if i === 0}
                  <td rowspan={span}><strong>{words(r.archetype)}</strong>{#if r.gap}<span class="flag">gap</span>{/if}
                    {#if !r.answers.some((x) => x.in_pool)}<small class="poolnote muted">{r.best ? `Pool's best: ${r.best.name} ${signed(r.best.pp)}` : 'Nothing in the pool'}</small>{/if}</td>
                  <td rowspan={span} class="icons">{#each r.champs as c}<span title={c.name}><Champ champ={c.card} size={26} /></span>{/each}</td>
                {/if}
                {#if a}
                  <td class="answer"><Champ champ={a.card} size={i === 0 ? 26 : 22} /> {a.name} <small class="muted">{measured(a.confidence)}</small>{#if a.in_pool}<span class="inpool" title="From {role.role_label}'s pool">in pool</span>{/if}{#if a.counter}<span class="counter" title={counterTitle(a)}>counter {signed(a.counter_pp)}pp{a.own_pp < 0 ? ' · weak overall' : ''}</span>{/if}</td>
                  <td class="num">{signed(a.pp)}</td>
                  <td class="num">{signed(a.lane_pp)}</td>
                  <td class="num">{signed(a.gold, 0)}</td>
                  <td class="num">{signed(a.game_pp)}</td>
                  <td class="num">{signed(a.expectation, 2)}</td>
                {:else}
                  <td class="muted" colspan="6">No answers</td>
                {/if}
                {#if i === 0}<td rowspan={span} class="plan muted">{r.plan ?? ''}</td>{/if}
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
    </section>
  {/each}
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .source { font-size: 13px; margin: 0 0 10px; }
  .waiting { font-size: 13px; margin: 0 0 10px; padding: 6px 10px; border-radius: 6px; background: var(--gold-bg); color: var(--down-text); }
  .tune { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; font-size: 13px; margin: 0 0 12px; }
  .tune label { display: inline-flex; align-items: center; gap: 8px; }
  .tune strong { font-variant-numeric: tabular-nums; }
  .role { background: var(--panel); border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; }
  h2 { font-size: 20px; margin: 0 0 8px; }
  h2 small { font-size: 13px; font-weight: 400; color: var(--muted); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
  .chip { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 12px; padding: 2px 10px; }
  .chip small { color: var(--muted); }
  .chip.green { border-color: var(--up); cursor: default; }
  .chip.amber { border-color: var(--down); cursor: default; }
  .chip.red { border-color: var(--side-red); background: var(--side-red-bg); cursor: pointer; }
  .chip.grey { border-style: dashed; color: var(--muted); cursor: default; }
  .detail { font-size: 13px; margin: 4px 0; }
  .health { display: flex; gap: 14px; margin: 4px 0 8px; color: var(--muted); }
  .health .missing { color: var(--down-text); }
  .small { font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--line); font-size: 12px; }
  td { padding: 3px 6px; border-bottom: 1px solid var(--panel-2); vertical-align: middle; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .icons { white-space: nowrap; }
  .icons span { display: inline-block; margin-right: 2px; }
  .answer { white-space: nowrap; }
  .flag { margin-left: 6px; font-size: 11px; padding: 0 5px; border-radius: 4px; background: var(--side-red-bg); color: var(--side-red-text); }
  tr.gap strong { color: var(--side-red-text); }
  tr.more td { font-size: 13px; color: var(--text-2); }
  .poolnote { display: block; font-size: 11px; font-weight: 400; }
  .inpool { margin-left: 6px; font-size: 11px; padding: 0 5px; border-radius: 4px; background: var(--gold-bg); color: var(--gold); }
  .counter { margin-left: 6px; font-size: 11px; padding: 0 5px; border-radius: 4px; background: var(--up-bg); color: var(--up-text); }
  .plan { font-size: 12px; }
  .muted { color: var(--muted); }
</style>
