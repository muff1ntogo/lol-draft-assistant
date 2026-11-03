<script>
  // Per-role lane ban tables (ban-score.md) with each opponent's final team ban value
  // (ban-value-matchups.md). Ban colour: teal = banning helps us, amber = it hurts us, grey = even;
  // validated against the panel surface, and every value is also printed signed.
  import Champ from './Champ.svelte';
  import PlanSynergy from './PlanSynergy.svelte';

  const savedPlan = (() => { try { return JSON.parse(localStorage.getItem('prep-plan') || '{}'); } catch { return {}; } })();
  let locks = $state(savedPlan);
  let planView = $state(null);
  $effect(() => {
    try { localStorage.setItem('prep-plan', JSON.stringify(locks)); } catch { /* not remembered */ }
  });

  let data = $state(null);
  let sheet = $state(null);
  fetch('/api/prep/lane-bans').then((r) => r.json()).then((d) => (data = d));
  fetch('/api/prep').then((r) => r.json()).then((d) => (sheet = d));
  const pct = (p) => `${Math.round(p * 100)}%`;
  const signed = (x, digits) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`;
</script>

<main class="page">
  <h1>Prep <a class="synlink" href="#/tags">Tags →</a> <a class="synlink" href="#/rules">Rules →</a></h1>
  <PlanSynergy bind:locks onplan={(d) => (planView = d)} />

  <details class="overview">
  <summary>Patch overview <span class="muted">(without a plan: priority bans, best 3, blind and flex picks, lane ban tables)</span></summary>
  {#if sheet}
    <div class="sheet">
      {#each ['blue', 'red'] as side}
        <section class="bans {side}">
          <h2>Priority bans · {side} side</h2>
          <ol>
            {#each sheet.bans[side].bans as b}
              <li><Champ champ={b.champ} size={36} /><div><strong>{b.champ.name}</strong><span>{b.reason}</span></div></li>
            {:else}<li class="muted">No useful ban</li>{/each}
          </ol>
        </section>
      {/each}
      <p class="muted small wide">{sheet.notes.bans}</p>

      <section class="wide">
        <h2>Best 3 this patch</h2>
        <div class="best">
          {#each sheet.best as r}
            <div>
              <h3>{r.role_label} <small>{r.bracket}</small></h3>
              {#each r.best as b}
                <div class="pickrow">
                  <Champ champ={b.champ} size={36} />
                  <div><strong>{b.champ.name}</strong><span>{b.tier} · {b.win_rate == null ? '—' : pct(b.win_rate)} WR · comfort {b.comfort}</span></div>
                </div>
              {:else}<p class="muted">empty pool</p>{/each}
            </div>
          {/each}
        </div>
      </section>

      <section>
        <h2>Safe blind first picks</h2>
        {#each sheet.blind_safe as b}
          <div class="pickrow">
            <Champ champ={b.champ} size={36} />
            <div><strong>{b.champ.name}</strong>
              <span>{b.role_label} · {pct(b.win_prob)} as blue's first pick{#if b.worst_counter} · worst counter {b.worst_counter.name}{/if}</span></div>
          </div>
        {:else}<p class="muted">None of our pool champions is safe to blind pick right now.</p>{/each}
        <p class="muted small">{sheet.notes.blind_safe}</p>
      </section>

      <section>
        <h2>Flex picks</h2>
        {#each sheet.flex as f}
          <div class="pickrow">
            <Champ champ={f.champ} size={36} />
            <div><strong>{f.champ.name}</strong><span>{Object.entries(f.tiers).map(([r, t]) => `${r} ${t}`).join(' · ')}</span></div>
          </div>
        {:else}<p class="muted">No champion is in two of our pools.</p>{/each}
      </section>
    </div>
  {/if}

  <h2 class="lanehead">Lane ban tables</h2>
  <p class="muted">
    Lane ban scores per role: how much banning each opponent improves that player's expected lane matchup
    (pp), and its final team ban value (meta threat + our matchups + counters, log-odds).
  </p>
  {#if data}
    <div class="roles">
      {#each data.roles as role}
        <section>
          <h2>{role.role_label} <small>{role.player_bracket} · μ {signed(role.mu, 2)}pp</small></h2>
          <table>
            <thead><tr><th>Opp</th><th>PR</th><th>Resp</th><th>via</th><th>Ban</th><th>Team ban value</th></tr></thead>
            <tbody>
              {#each role.rows.slice(0, 15) as r}
                <tr>
                  <td>{r.opp}</td>
                  <td class="num">{r.pr.toFixed(1)}%</td>
                  <td class="num">{signed(r.resp, 1)}</td>
                  <td class="via">{r.via}</td>
                  <td class="num ban {r.band}">{signed(r.ban, 2)}</td>
                  <td class="num">{r.team_ban_value == null ? '—' : signed(r.team_ban_value, 4)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </section>
      {/each}
    </div>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
  </details>
</main>

<style>
  .page { max-width: 1880px; margin: 0 auto; padding: 12px 20px 60px; }
  h1 { font-size: 28px; margin: 4px 0 8px; }
  .overview { margin-top: 24px; }
  .overview > summary { cursor: pointer; font-size: 20px; font-weight: 600; margin-bottom: 12px; }
  .overview > summary span { font-size: 14px; font-weight: 400; }
  .synlink { font-size: 15px; font-weight: 400; color: var(--gold); margin-left: 16px; text-decoration: none; }
  h2 { font-size: 18px; margin: 0 0 8px; }
  h2 small { color: var(--muted); font-weight: 400; font-size: 14px; }
  .roles { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 16px; }
  section { background: var(--panel); border-radius: 8px; padding: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 6px; border-bottom: 1px solid var(--line); }
  td { padding: 3px 6px; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .via { color: var(--muted); }
  .ban.positive { color: #1baf7a; }
  .ban.negative { color: #c98500; }
  .ban.neutral { color: #8b96a3; }
  .muted { color: var(--muted); }
  .small { font-size: 13px; }
  .sheet { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .wide { grid-column: 1 / -1; }
  .bans.blue { border-top: 3px solid #2f6fb7; }
  .bans.red { border-top: 3px solid #c0423a; }
  ol { margin: 0; padding-left: 20px; }
  li { margin: 6px 0; }
  li, .pickrow { display: flex; align-items: center; gap: 10px; }
  li div, .pickrow div { display: flex; flex-direction: column; }
  li span, .pickrow span { font-size: 13px; color: #c9d1d9; }
  .pickrow { margin: 6px 0; }
  .best { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
  h3 { font-size: 15px; margin: 0 0 6px; }
  h3 small { color: var(--muted); font-weight: 400; }
  .lanehead { font-size: 22px; margin: 8px 0; }
  @media (max-width: 1100px) { .sheet { grid-template-columns: 1fr; } .best { grid-template-columns: 1fr 1fr; } }
</style>
