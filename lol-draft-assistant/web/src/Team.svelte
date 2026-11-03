<script>
  let data = $state(null);
  let message = $state('');

  async function load() {
    data = await (await fetch('/api/team')).json();
  }
  load();

  async function setStyle(style) {
    const res = await fetch('/api/team/style', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ style: style || null }),
    });
    data = await res.json();
    message = data.applied ? 'Team style saved: suggestions pull towards it from now on.'
                           : 'Team style saved: it applies after this draft.';
  }

  const pct = (x) => `${Math.round(x * 100)}%`;
</script>

<main class="page">
  <h1>Team</h1>
  {#if data}
    <section>
      <h2>Recent form <small>last {data.recent.games} finished games · {data.recent.wins}W {data.recent.games - data.recent.wins}L</small></h2>
      <div class="form">
        {#each data.recent.form as g}
          <span class="g {g.result}" title="{g.result} · {g.style ? data.choices[g.style] : 'style unknown'} · {new Date(g.played_at).toLocaleDateString()}">
            {g.result === 'win' ? 'W' : 'L'}
          </span>
        {:else}
          <span class="muted">No finished games yet.</span>
        {/each}
      </div>
    </section>

    <section>
      <h2>Style</h2>
      <div class="styles">
        {#each Object.entries(data.styles) as [key, s]}
          <div class="style" class:suggested={data.suggested === key} class:chosen={data.hand_style === key}>
            <div class="label">{s.label}</div>
            <div class="nums">{s.games ? `${s.wins}W ${s.games - s.wins}L` : 'no games'}</div>
            {#if s.games}<div class="muted small">{pct(s.win_rate_adj)} (shrunk)</div>{/if}
            {#if data.suggested === key}<div class="tag">suggested</div>{/if}
            {#if data.hand_style === key}<div class="tag mine">our style</div>{/if}
          </div>
        {/each}
      </div>
      <p class="advice">
        {#if data.suggested}
          Suggested: <strong>{data.suggested_label}</strong>, our best record among the last 20 games. It's advice only.
        {:else}
          No suggestion yet: {data.suggestion_note}
        {/if}
      </p>
      <label class="set">
        Our style (set by hand)
        <select value={data.hand_style ?? ''} onchange={(e) => setStyle(e.currentTarget.value)}>
          <option value="">not set</option>
          {#each Object.entries(data.choices) as [key, label]}<option value={key}>{label}</option>{/each}
        </select>
      </label>
      {#if message}<p class="ok">{message}</p>{/if}
      <p class="muted small">
        Each logged draft is classified like the Live comp panel, from the champions' fight styles (Champions page):
        a style needs 45% of the comp and two champions of it (Front to back also a front-liner), else it is Mixed.
        In a draft, picks are pulled towards the style set here (from our pick 2, full strength at picks 4–5;
        Settings › Comp style). "Mixed" means no pull; "not set" follows the style our comp is heading for.
      </p>
    </section>
  {:else}
    <p class="muted">Loading…</p>
  {/if}
</main>

<style>
  .page { max-width: 900px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 32px; margin: 8px 0 16px; }
  h2 { font-size: 20px; margin: 0 0 12px; }
  h2 small { font-size: 14px; color: var(--muted); font-weight: 400; margin-left: 8px; }
  section { background: var(--panel); border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }
  .form { display: flex; gap: 6px; }
  .g { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 6px; font-weight: 800; background: #222a35; }
  .g.win { color: #1baf7a; }
  .g.loss { color: #e5737d; }
  .styles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .style { background: var(--panel-2); border: 2px solid transparent; border-radius: 8px; padding: 10px 12px; }
  .style.suggested { border-color: #e3b341; }
  .style.chosen { box-shadow: inset 0 0 0 1px #4c9aff; }
  .label { font-weight: 700; }
  .nums { font-size: 18px; margin-top: 4px; }
  .tag { display: inline-block; margin-top: 6px; font-size: 12px; color: #e3b341; }
  .tag.mine { color: #4c9aff; margin-left: 6px; }
  .advice { margin: 14px 0; }
  .set { display: flex; align-items: center; gap: 10px; }
  select { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; font: inherit; }
  .muted { color: var(--muted); }
  .small { font-size: 13px; }
  .ok { color: var(--good); }
</style>
