<script>
  import { reveal, rise, swap } from './motion.js';
  // docs/draft-vs-execution.md, Stage 4. Teal = moved in our favour, amber = against, grey = within 1pp. Validated on the
  // panel surface (CLAUDE.md); the signed number and the label are always written too.
  import { STATS, signed as signedNum } from './format.js';
  import Stat from './ui/Stat.svelte';
  import Table from './ui/Table.svelte';
  import Button from './ui/Button.svelte';

  let { e } = $props();
  let openBot = $state(false);
  let openTable = $state(false);

  function value(stat, x) {
    const kind = STATS[stat]?.[1];
    if (kind === 'lead') return signedNum(x, 0);
    if (kind === 'lead1') return signedNum(x, 1);
    if (kind === 'pct') return `${Math.round(x * 100)}%`;
    return x.toFixed(1);
  }
  // The SD in the stat's own unit: a percentage stat's spread is a percentage too
  const spread = (stat, sd) => (STATS[stat]?.[1] === 'pct' ? `${Math.round(sd * 100)}%` : sd.toFixed(1));
  // Whether the label is good or bad for us. z stays raw (for deaths a positive z is bad), so the colour says which way it went
  const fav = (label) => (label.endsWith('ahead') ? 'up' : label.endsWith('behind') ? 'down' : '');
  const pp = (x, digits = 0) => `${signedNum(x, digits)}pp`;
  const pct = (p) => `${Math.round(p * 100)}%`;
  const tone = (x) => (x >= 1 ? 'up' : x <= -1 ? 'down' : null);

  // Each step runs from where the last one ended, on a 0-100% axis
  const steps = $derived.by(() => {
    const out = [{ name: 'Draft', from: 0.5, to: e.p0, part: e.parts.draft,
                   hint: `Chance at lock-in ${pct(e.p0)}, scaled as crawled games say it should be${e.model_p0 != null ? `; the screen showed ${pct(e.model_p0)} on the day` : ''}` }];
    if (e.available) {
      out.push({ name: 'Early game', from: e.p0, to: e.p15, part: e.parts.early, hint: `${pct(e.p0)} at lock-in to ${pct(e.p15)} at 15:00` });
      out.push({ name: 'Conversion', from: e.p15, to: e.result, part: e.parts.conversion, hint: `${pct(e.p15)} at 15:00 to the result` });
    }
    return out;
  });

  const rows = $derived(e.chips.flatMap((c) => [c, ...c.players]));
  const after = [['Deaths while ahead', 'deaths_while_ahead'], ['Barons', 'barons'], ['Dragons after 15', 'dragons_after_15'], ['Towers after 15', 'towers_after_15']];
</script>

<section class="strip" aria-label="Draft vs execution">
  <h3>Draft vs execution</h3>
  {#if e.verdict}
    <p class="verdict"><strong>{e.verdict}</strong>{#if e.contributors.length}: {e.contributors.join(', ')}{/if}</p>
  {/if}

  <!-- One figure per part, as on the Team page: the signed number first, the chance it moved from and to underneath -->
  <div class="parts">
    {#each steps as s}
      <Stat value={signedNum(s.part, 0)} unit="pp" label={s.name} tone={tone(s.part)} note="{pct(s.from)} → {pct(s.to)}" title={s.hint} />
    {/each}
    {#if e.available}
      <Stat value={e.result ? 'Won' : 'Lost'} label="Result" note="the parts add up to {e.result ? '+50' : '−50'}pp" />
    {/if}
  </div>

  {#if e.available}
    <div class="roles">
      <span class="label-caps">Early game by part</span>
      {#each e.chips as c}
        {#if c.players.length}
          <button class="rolechip" aria-expanded={openBot} onclick={() => (openBot = !openBot)}>
            <b>{c.name} <span class="chev">{#key openBot}<span in:swap out:swap>{openBot ? '▾' : '▸'}</span>{/key}</span></b><span>{c.label}</span><em class="mono {tone(c.share_pp) ?? ''}">{pp(c.share_pp, 1)}</em>
          </button>
          {#if openBot}
            {#each c.players as p, i}
              <div class="rolechip sub" in:rise={{ i }} out:rise><b>{p.name}</b><span>{p.label}</span><em class="mono {tone(p.share_pp) ?? ''}">{pp(p.share_pp, 1)}</em></div>
            {/each}
          {/if}
        {:else}
          <div class="rolechip"><b>{c.name}</b><span>{c.label}</span><em class="mono {tone(c.share_pp) ?? ''}">{pp(c.share_pp, 1)}</em></div>
        {/if}
      {/each}
    </div>
    <p class="key">Each part's share of the early game, against what this draft was expected to do. Single games are noisy.</p>

    <div><Button variant="ghost" aria-expanded={openTable} onclick={() => (openTable = !openTable)}>{openTable ? 'Hide' : 'Show'} every stat</Button></div>
    {#if openTable}
      <div class="tables" transition:reveal>
        <Table dense sticky={false}>
          <thead><tr><th scope="col">Stat</th><th scope="col" class="num">Actual</th><th scope="col" class="num">Expected</th><th scope="col" class="num">SD</th><th scope="col" class="num">z</th><th scope="col">Label</th></tr></thead>
          <tbody>
            {#each rows as c}
              <tr class="group"><td colspan="6"><strong>{c.name}</strong> <span><span class="mono">{pp(c.share_pp, 1)}</span> · {c.label}</span></td></tr>
              {#if !c.stats.some((s) => s.stat === c.headline.stat)}
                <tr class="basis">
                  <td>{STATS[c.headline.stat][0]} (what the label reads)</td><td class="num mono">{value(c.headline.stat, c.headline.actual)}</td>
                  <td class="num mono">{value(c.headline.stat, c.headline.expected)}</td><td class="num mono">{spread(c.headline.stat, c.headline.sd)}</td>
                  <td class="num mono {fav(c.headline.label)}">{signedNum(c.headline.z, 1)}</td><td>{c.headline.label}</td>
                </tr>
              {/if}
              {#each c.stats as s}
                <tr>
                  <td>{STATS[s.stat][0]}</td><td class="num mono">{value(s.stat, s.actual)}</td><td class="num mono">{value(s.stat, s.expected)}</td>
                  <td class="num mono">{spread(s.stat, s.sd)}</td><td class="num mono {fav(s.label)}">{signedNum(s.z, 1)}</td><td>{s.label}</td>
                </tr>
              {/each}
            {/each}
          </tbody>
        </Table>
        {#if e.detail}
          <Table dense sticky={false}>
            <thead><tr><th scope="col">After 15:00</th><th scope="col" class="num">Us</th><th scope="col" class="num">Them</th></tr></thead>
            <tbody>
              {#each after as [name, key]}
                {#if e.detail[key]}<tr><td>{name}</td><td class="num mono">{e.detail[key][0]}</td><td class="num mono">{e.detail[key][1]}</td></tr>{/if}
              {/each}
              {#if e.detail.gold_25}
                <tr><td>{STATS.gold_25[0]}: expected <span class="mono">{value('gold_25', e.detail.gold_25.expected)}</span>, {e.detail.gold_25.label}</td>
                  <td class="num mono" colspan="2">{value('gold_25', e.detail.gold_25.actual)}</td></tr>
              {/if}
            </tbody>
          </Table>
        {/if}
      </div>
    {/if}
  {:else}
    <p class="key">Execution data not available: the game's timeline hasn't arrived, or it ended before 15:00.</p>
  {/if}
</section>

<style>
  .strip { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--hairline); display: grid; gap: 14px; }
  h3 { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3); }
  .verdict { margin: 0; font-size: 15px; color: var(--text-2); }
  .verdict strong { color: var(--text-1); }
  .parts { display: flex; flex-wrap: wrap; gap: 16px 48px; }
  .roles { display: flex; flex-wrap: wrap; gap: 8px 28px; align-items: flex-start; }
  .roles > .label-caps { align-self: center; }
  .rolechip { display: grid; gap: 1px; padding: 0; background: none; border: 0; color: var(--text-1); font: inherit; font-size: 13px; text-align: left; }
  button.rolechip { cursor: pointer; }
  button.rolechip:hover b { text-decoration: underline; filter: none; }
  .rolechip.sub { padding-left: 12px; border-left: 1px solid var(--hairline-strong); }
  .rolechip b { font-weight: 600; }
  .rolechip span { color: var(--text-3); font-size: 12px; }
  .rolechip em { font-style: normal; font-weight: 600; font-size: 12.5px; color: var(--text-2); }
  .up { color: var(--up-text) !important; }
  .down { color: var(--down-text) !important; }
  .key { margin: 0; font-size: 12px; color: var(--text-3); }
  .tables { display: grid; grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr); gap: 32px; align-items: start; }
  .group td { padding-top: 8px; }
  .group span { color: var(--text-3); }
  .basis td { color: var(--text-3); }
  @media (max-width: 1180px) { .tables { grid-template-columns: 1fr; } }
  .chev { display: inline-grid; width: 1ch; }
  .chev > span { grid-area: 1 / 1; }
</style>
