<script>
  // Prep: the Matchup tree (docs/matchup-archetypes.md, Stage 7). Per role, each enemy archetype, our player's best
  // answer from their pool, what the archetypes alone would suggest, and a flag where the best answer is below -1.5pp.
  import Champ from './Champ.svelte';

  let data = $state(null);
  let open = $state({}); // "role:archetype" -> the red chip's detail is showing
  fetch('/api/prep/matchup-tree').then((r) => r.json()).then((d) => (data = d));

  const signed = (x, digits = 1) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`);
  const words = (a) => a.replaceAll('_', ' ');
  const measured = (c) => (c >= 0.5 ? 'measured' : c >= 0.15 ? 'partly measured' : 'archetype estimate');
  const toggle = (k) => (open[k] = !open[k]);
</script>

{#if data}
  {#each data.roles as role}
    <section class="role">
      <h2>{role.role_label} <small>{role.bracket}</small></h2>
      <div class="chips">
        {#each role.rows.filter((r) => r.chip !== 'none') as r}
          {@const k = `${role.role}:${r.archetype}`}
          <button class="chip {r.chip}" onclick={() => r.chip === 'red' && toggle(k)} title={r.chip === 'red' ? 'Show ban candidates and who answers it' : ''}>
            {words(r.archetype)} <small>{r.chip === 'red' ? 'gap' : r.chip === 'amber' ? 'thin' : 'covered'}</small>
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
        <thead><tr><th>Enemy archetype</th><th>Most picked</th><th>Best answer</th><th class="num">Blend</th><th class="num">Lane</th>
          <th class="num">Gold at 15</th><th class="num">Game</th><th class="num" title="What the two archetypes alone suggest">Expected</th><th>Plan</th></tr></thead>
        <tbody>
          {#each role.rows.filter((r) => r.champs.length) as r}
            <tr class:gap={r.gap}>
              <td><strong>{words(r.archetype)}</strong>{#if r.gap}<span class="flag">gap</span>{/if}</td>
              <td class="icons">{#each r.champs as c}<span title={c.name}><Champ champ={c.card} size={26} /></span>{/each}</td>
              {#if r.best}
                <td class="answer"><Champ champ={r.best.card} size={26} /> {r.best.name} <small class="muted">{measured(r.best.confidence)}</small></td>
                <td class="num">{signed(r.best.pp)}</td>
                <td class="num">{signed(r.best.lane_pp)}</td>
                <td class="num">{signed(r.best.gold, 0)}</td>
                <td class="num">{signed(r.best.game_pp)}</td>
                <td class="num">{signed(r.expectation, 2)}</td>
              {:else}
                <td class="muted" colspan="6">Nothing in the pool</td>
              {/if}
              <td class="plan muted">{r.plan ?? ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/each}
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .role { background: var(--panel); border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; }
  h2 { font-size: 20px; margin: 0 0 8px; }
  h2 small { font-size: 13px; font-weight: 400; color: var(--muted); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
  .chip { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 12px; padding: 2px 10px; }
  .chip small { color: var(--muted); }
  .chip.green { border-color: #1baf7a; cursor: default; }
  .chip.amber { border-color: #c98500; cursor: default; }
  .chip.red { border-color: #c0423a; background: #3a1d1b; cursor: pointer; }
  .detail { font-size: 13px; margin: 4px 0; }
  .health { display: flex; gap: 14px; margin: 4px 0 8px; color: var(--muted); }
  .health .missing { color: #e0a640; }
  .small { font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--line); font-size: 12px; }
  td { padding: 3px 6px; border-bottom: 1px solid #1f2630; vertical-align: middle; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .icons { white-space: nowrap; }
  .icons span { display: inline-block; margin-right: 2px; }
  .answer { white-space: nowrap; }
  .flag { margin-left: 6px; font-size: 11px; padding: 0 5px; border-radius: 4px; background: #3a1d1b; color: #e8a09a; }
  tr.gap strong { color: #e8a09a; }
  .plan { font-size: 12px; }
  .muted { color: var(--muted); }
</style>
