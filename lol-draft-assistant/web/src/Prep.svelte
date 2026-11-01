<script>
  // Per-role lane ban tables (ban-score.md) with each opponent's final team ban value
  // (ban-value-matchups.md). Ban colour: teal = banning helps us, amber = it hurts us, grey = even;
  // validated against the panel surface, and every value is also printed signed.
  let data = $state(null);
  fetch('/api/prep/lane-bans').then((r) => r.json()).then((d) => (data = d));
  const signed = (x, digits) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`;
</script>

<main class="page">
  <h1>Prep</h1>
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
</main>

<style>
  .page { max-width: 1500px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 32px; margin: 8px 0 8px; }
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
</style>
