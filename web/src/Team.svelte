<script>
  import { pulse } from './motion.js';
  import { signed, statName } from './format.js';
  import Champ from './Champ.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Field from './ui/Field.svelte';
  import Stat from './ui/Stat.svelte';
  import Table from './ui/Table.svelte';
  import Tag from './ui/Tag.svelte';

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
  const tone = (b, lowerIsBetter = false) => (!excludesZero(b) ? null : (b.mean > 0) !== lowerIsBetter ? 'up' : 'down');
</script>

<main class="console">
  <PageHeader title="Team trends" description="Where our games are won and lost across games: draft, early game and conversion, per player, and by comp style.">
    {#snippet actions()}
      <Segmented label="Review pages" value="team" onchange={(v) => (location.hash = `#/${v}`)} options={[['history', 'History'], ['team', 'Team trends']]} />
    {/snippet}
  </PageHeader>
  {#if data}
    <section class="first">
      <h2>Recent form <span class="sub">last {data.recent.games} finished games · <span class="mono">{data.recent.wins}W {data.recent.games - data.recent.wins}L</span></span></h2>
      <div class="form">
        {#each data.recent.form as g}
          <span class="g mono {g.result}" title="{g.result} · {g.style ? data.choices[g.style] : 'style unknown'} · {new Date(g.played_at).toLocaleDateString()}">
            {g.result === 'win' ? 'W' : 'L'}
          </span>
        {:else}
          <span class="none">No finished games yet</span>
        {/each}
      </div>
    </section>

    <section>
      <div class="sechead">
        <h2>Draft vs execution</h2>
        <span class="label-caps">Games</span>
        <Segmented label="Games" bind:value={trendWindow} options={[['10', 'Last 10'], ['20', 'Last 20'], ['all', 'All']]} />
      </div>
      {#if !trends}
        <p class="none">Loading…</p>
      {:else if trends.team.too_few}
        <p class="none">Too few games for a trend: {trends.team.games} with execution data, {trends.min_games} needed.</p>
      {:else}
        {@const t = trends.team}
        <p class="lever">
          {#if t.lever}Biggest lever: <strong>{t.lever.name}</strong>, <span class="mono">{bandText(t.lever, 1, 'pp')}</span> a game on average.
          {:else}Biggest lever: no clear pattern yet.{/if}
        </p>
        <div class="parts">
          {#each Object.entries(PART_NAMES) as [key, name]}
            <Stat value={signed(t.parts[key].mean, 1)} unit="pp" label={name} tone={tone(t.parts[key])} note={t.parts[key].se != null ? `± ${t.parts[key].se.toFixed(1)}` : ''} />
          {/each}
        </div>
        <div class="split">
          <Table dense sticky={false}>
            <thead><tr><th scope="col">Early game by part</th><th scope="col" class="num">Mean a game</th></tr></thead>
            <tbody>
              {#each Object.entries(EARLY_NAMES) as [key, name]}
                <tr><td>{name}</td><td class="num mono {tone(t.early_parts[key]) ?? ''}">{bandText(t.early_parts[key], 1, 'pp')}</td></tr>
              {/each}
            </tbody>
          </Table>
          <Table dense sticky={false}>
            <thead><tr><th scope="col">Verdict</th><th scope="col" class="num">Games</th></tr></thead>
            <tbody>
              {#each Object.entries(t.verdicts) as [v, n]}
                <tr class:zero={!n}><td>{v}</td><td class="num mono">{n}</td></tr>
              {/each}
            </tbody>
          </Table>
        </div>
        <p class="cap">Means over the last {t.games} games with execution data, ± one standard error. A figure is coloured only when its band is clear of 0.</p>
      {/if}

      {#if trends}
        <h3>Per player <span class="sub">the lineup's games in their role and the champions in their pool; mean z against expectation, ± one standard error; for deaths, fewer is better</span></h3>
        <div class="players">
          {#each trends.roles as r}
            <div class="player">
              <div class="who"><span class="label-caps">{r.name}</span>{#if r.player} <strong>{r.player}</strong>{/if} <span class="sub"><span class="mono">{r.games}</span> games</span></div>
              {#if !r.player}
                <p class="none">No player in this role on the Roster</p>
              {:else if r.too_few}
                <p class="none">Too few games for a trend</p>
              {:else}
                <Table dense sticky={false}>
                  <tbody>
                    {#each r.stats as s}
                      <tr><td>{statName(s.stat)}</td><td class="num mono {tone(s, s.lower_is_better) ?? ''}">{bandText(s, 2)}</td></tr>
                    {/each}
                    {#each r.champions as c}
                      <tr class="champrow"><td colspan="2"><span class="cname"><Champ champ={c.champ} size={20} /><strong>{c.champ?.name}</strong> <span class="sub"><span class="mono">{c.games}</span> games</span></span></td></tr>
                      {#each c.stats as s}
                        <tr><td>{statName(s.stat)}</td><td class="num mono {tone(s, s.lower_is_better) ?? ''}">{bandText(s, 2)}</td></tr>
                      {/each}
                    {/each}
                  </tbody>
                </Table>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </section>

    <section>
      <div class="sechead">
        <h2>Style</h2>
        <Field label="Our style (set by hand)" inline width="auto" value={data.hand_style ?? ''} onchange={(e) => setStyle(e.currentTarget.value)}
               options={[['', 'Not set'], ...Object.entries(data.choices)]} />
      </div>
      {#if message}<p class="saved" role="status" use:pulse={{ value: message, ok: !/^(Not|No) |fail|error|couldn/i.test(message) }}>{message}</p>{/if}
      <div class="stylewrap">
        <Table dense sticky={false}>
          <thead><tr><th scope="col">Style</th><th scope="col" class="num">Record</th><th scope="col" class="num" title="Win rate shrunk towards 50%">Win rate, shrunk</th><th scope="col"></th></tr></thead>
          <tbody>
            {#each Object.entries(data.styles) as [key, s]}
              <tr class:zero={!s.games}>
                <td><strong>{s.label}</strong></td>
                <td class="num mono">{s.games ? `${s.wins}W ${s.games - s.wins}L` : '—'}</td>
                <td class="num mono">{s.games ? pct(s.win_rate_adj) : '—'}</td>
                <td><span class="marks">{#if data.suggested === key}<Tag bare>suggested</Tag>{/if}{#if data.hand_style === key}<Tag bare>our style</Tag>{/if}</span></td>
              </tr>
            {/each}
          </tbody>
        </Table>
      </div>
      <p class="advice">
        {#if data.suggested}
          Suggested: <strong>{data.suggested_label}</strong>, our best record among the last 20 games. It's advice only.
        {:else}
          No suggestion yet: {data.suggestion_note}
        {/if}
      </p>
      <p class="cap">
        Each logged draft is classified like the Live comp panel, from the champions' tags: an
        identity (Engage, Poke, Pick, Protect, Split) needs 35% of the comp and two champions of it, else it is Mixed.
        In a draft, picks are pulled towards the style set here (from our pick 2, full strength at picks 4–5;
        Settings › Comp style). "Mixed" means no pull; "Not set" follows the style our comp is heading for.
      </p>
    </section>
  {:else}
    <p class="none">Loading…</p>
  {/if}
</main>

<style>
  section { padding: 20px 0 24px; border-top: 1px solid var(--hairline); }
  section.first { border-top: 0; padding-top: 0; }
  h2 { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 12px; }
  h3 { font-size: 14px; font-weight: 650; margin: 28px 0 10px; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; text-transform: none; }
  .sechead { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .sechead h2 { margin: 0 16px 0 0; }
  .none { margin: 0; font-size: 13px; color: var(--text-3); }
  .cap { margin: 10px 0 0; font-size: 12px; color: var(--text-3); max-width: 110ch; }
  /* a win is bright ink, a loss is quiet: the letter says which, so no colour is needed */
  .form { display: flex; gap: 4px; }
  .g { width: 28px; height: 28px; display: grid; place-items: center; border-radius: var(--radius-sm); font-size: 13px; font-weight: 600; background: var(--surface-1); color: var(--text-3); }
  .g.win { background: var(--surface-3); color: var(--text-1); }
  .lever { margin: 0 0 16px; font-size: 14px; color: var(--text-2); }
  .lever strong { color: var(--text-1); }
  .parts { display: flex; flex-wrap: wrap; gap: 16px 48px; margin-bottom: 20px; }
  .split { display: grid; grid-template-columns: repeat(2, minmax(0, 420px)); gap: 20px 48px; }
  .players { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 24px 32px; align-items: start; }
  .who { margin-bottom: 4px; font-size: 13px; }
  .who strong { font-weight: 600; margin-left: 6px; }
  .player .none { padding: 6px 0; }
  .cname { display: inline-flex; align-items: center; gap: 8px; }
  .cname strong { font-weight: 600; }
  .player :global(tr.champrow td) { padding-top: 10px; height: 38px; }
  section :global(tr.zero td) { color: var(--text-3); }
  .stylewrap { max-width: 640px; }
  .marks { display: inline-flex; gap: 12px; }
  .advice { margin: 14px 0 0; font-size: 13px; color: var(--text-2); }
  .saved { margin: 0 0 10px; font-size: 13px; color: var(--text-2); }
  @media (max-width: 820px) { .split { grid-template-columns: minmax(0, 1fr); } }
</style>
