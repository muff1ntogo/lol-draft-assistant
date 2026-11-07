<script>
  import { signed, statName } from './format.js';

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

  // docs/draft-vs-execution.md, Stage 5
  let trendWindow = $state('10');
  let trends = $state(null);
  async function loadTrends() {
    trends = await (await fetch(`/api/team/trends?window=${trendWindow}`)).json();
  }
  $effect(() => { trendWindow; loadTrends(); });

  const PART_NAMES = { draft: 'Draft', early: 'Early game', conversion: 'Conversion' };
  const EARLY_NAMES = { top: 'Top', jungle: 'Jungle', mid: 'Mid', bot: 'Bot', objectives: 'Objectives and team' };
  const bandText = (b, digits, unit = '') => `${signed(b.mean, digits)}${unit}${b.se != null ? ` ± ${b.se.toFixed(digits)}` : ''}`;
  const excludesZero = (b) => b.se != null && (b.mean + b.se < 0 || b.mean - b.se > 0);
  // Teal = in our favour, amber = against, grey = the band reaches 0. z is raw, so for deaths a positive mean is against us
  const tone = (b, lowerIsBetter = false) => (!excludesZero(b) ? '' : (b.mean > 0) !== lowerIsBetter ? 'good' : 'bad');
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

    <section class="trends">
      <h2>Draft vs execution
        <span class="windows" role="group" aria-label="Games">
          {#each [['10', 'Last 10'], ['20', 'Last 20'], ['all', 'All']] as [key, label]}
            <button class:on={trendWindow === key} aria-pressed={trendWindow === key} onclick={() => (trendWindow = key)}>{label}</button>
          {/each}
        </span>
      </h2>
      {#if !trends}
        <p class="muted">Loading…</p>
      {:else if trends.team.too_few}
        <p>Too few games for a trend: {trends.team.games} with execution data, {trends.min_games} needed.</p>
      {:else}
        {@const t = trends.team}
        <p class="lever">
          {#if t.lever}Biggest lever: <strong>{t.lever.name}</strong>, {bandText(t.lever, 1, 'pp')} a game on average.
          {:else}Biggest lever: no clear pattern yet.{/if}
        </p>
        <div class="parts">
          {#each Object.entries(PART_NAMES) as [key, name]}
            <div class="part {tone(t.parts[key])}"><span>{name}</span><b>{bandText(t.parts[key], 1, 'pp')}</b></div>
          {/each}
        </div>
        <div class="early">
          <span class="muted small">Early game by part:</span>
          {#each Object.entries(EARLY_NAMES) as [key, name]}
            <span class="ep {tone(t.early_parts[key])}">{name} <b>{bandText(t.early_parts[key], 1, 'pp')}</b></span>
          {/each}
        </div>
        <div class="verdicts">
          {#each Object.entries(t.verdicts) as [v, n]}
            <span class="vc" class:zero={!n}>{v} <b>{n}</b></span>
          {/each}
        </div>
        <p class="muted small">Means over the last {t.games} games with execution data, ± one standard error. A lever needs its
          band clear of 0.</p>
      {/if}

      {#if trends}
        <h3>Per player <small class="muted">mean z against expectation, ± one standard error. Teal is in our favour, amber against
          (for deaths, fewer is better), grey when the band reaches 0.</small></h3>
        <div class="players">
          {#each trends.roles as r}
            <div class="player">
              <div class="who"><b>{r.name}</b>{#if r.player} <span class="muted">{r.player}</span>{/if} <span class="muted small">· {r.games} games</span></div>
              {#if r.too_few}
                <p class="muted small">Too few games for a trend.</p>
              {:else}
                <table>
                  <tbody>
                    {#each r.stats as s}
                      <tr><td>{statName(s.stat)}</td>
                        <td class="n {tone(s, s.lower_is_better)}">{bandText(s, 2)}</td></tr>
                    {/each}
                  </tbody>
                </table>
              {/if}
              {#each r.champions as c}
                <div class="champ">
                  <div class="cname">{#if c.champ?.icon}<img src={c.champ.icon} alt="" width="20" height="20" />{/if}{c.champ?.name} <span class="muted small">· {c.games} games</span></div>
                  <table>
                    <tbody>
                      {#each c.stats as s}
                        <tr><td>{statName(s.stat)}</td><td class="n {tone(s, s.lower_is_better)}">{bandText(s, 2)}</td></tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/each}
            </div>
          {/each}
        </div>
      {/if}
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
        Each logged draft is classified like the Live comp panel, from the champions' tags (Champions page): an
        identity (Engage, Poke, Pick, Protect, Split) needs 35% of the comp and two champions of it, else it is Mixed.
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
  /* trends: teal / amber as validated (CLAUDE.md); the signed number is always written */
  .trends h2 { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .windows { display: inline-flex; gap: 4px; margin-left: auto; }
  .windows button { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px;
                    font: inherit; font-size: 13px; cursor: pointer; }
  .windows button.on { border-color: #e3b341; }
  h3 { font-size: 16px; margin: 18px 0 8px; }
  h3 small { font-weight: 400; font-size: 13px; margin-left: 6px; }
  .lever { margin: 0 0 10px; }
  .parts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .part { background: var(--panel-2); border-left: 4px solid #8b96a3; border-radius: 4px; padding: 6px 10px; display: grid; }
  .part span { font-size: 13px; color: #c9d1d9; }
  .part b { font-size: 18px; font-variant-numeric: tabular-nums; }
  .part.good { border-left-color: #1baf7a; }
  .part.bad { border-left-color: #c98500; }
  .early, .verdicts { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 10px; font-size: 13px; align-items: baseline; }
  .ep b, .vc b { font-variant-numeric: tabular-nums; }
  .ep.good b, td.good { color: #1baf7a; }
  .ep.bad b, td.bad { color: #c98500; }
  .vc { background: var(--panel-2); border-radius: 4px; padding: 2px 8px; }
  .vc.zero { opacity: 0.55; }
  .players { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .player { background: var(--panel-2); border-radius: 6px; padding: 8px 10px; }
  .player p { margin: 6px 0 0; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px; }
  td { padding: 2px 0; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; padding-left: 8px; }
  .champ { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 6px; }
  .cname { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; }
  .cname img { border-radius: 3px; }
  @media (max-width: 700px) { .parts { grid-template-columns: 1fr; } }
</style>
