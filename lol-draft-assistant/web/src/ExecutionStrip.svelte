<script>
  // docs/draft-vs-execution.md, Stage 4. Teal = moved in our favour, amber = against, grey = within 1pp. Validated on the
  // panel surface (CLAUDE.md); the signed number and the label are always written too.
  let { e } = $props();
  let openBot = $state(false);
  let openTable = $state(false);

  const STATS = {
    gold: ['Gold lead', 'lead'], xp: ['XP lead', 'lead'], cs: ['CS lead at 14', 'lead1'], gold_25: ['Gold lead at 25', 'lead'],
    deaths_solo: ['Solo deaths before 14', 'count'], deaths_ganked: ['Ganked deaths before 14', 'count'],
    jungle_cs: ['Jungle CS at 15', 'count'], kp: ['Kill participation before 15', 'pct'], vision: ['Wards placed + killed per minute', 'count'],
    dragons: ['Dragons by 15 (us − them)', 'lead1'], grubs: ['Grubs by 15 (us − them)', 'lead1'],
    towers: ['Towers by 15 (us − them)', 'lead1'], plates: ['Plates by 15 (us − them)', 'lead1'],
  };
  const signedNum = (x, digits) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(digits)}`;
  function value(stat, x) {
    const kind = STATS[stat]?.[1];
    if (kind === 'lead') return signedNum(x, 0);
    if (kind === 'lead1') return signedNum(x, 1);
    if (kind === 'pct') return `${Math.round(x * 100)}%`;
    return x.toFixed(1);
  }
  const pp = (x, digits = 0) => `${signedNum(x, digits)}pp`;
  const pct = (p) => `${Math.round(p * 100)}%`;
  const tone = (x) => (x >= 1 ? 'up' : x <= -1 ? 'down' : 'flat');

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
  const left = (s) => 100 * Math.min(s.from, s.to);
  const width = (s) => 100 * Math.abs(s.to - s.from);

  const rows = $derived(e.chips.flatMap((c) => [c, ...c.players]));
  const after = [['Deaths while ahead', 'deaths_while_ahead'], ['Barons', 'barons'], ['Dragons after 15', 'dragons_after_15'], ['Towers after 15', 'towers_after_15']];
</script>

<section class="strip" aria-label="Draft vs execution">
  <h3>Draft vs execution</h3>
  {#if e.verdict}
    <p class="verdict"><strong>{e.verdict}</strong>{#if e.contributors.length}: {e.contributors.join(', ')}{/if}</p>
  {/if}

  <div class="fall">
    {#each steps as s}
      <div class="step" title={s.hint}>
        <span class="name">{s.name}</span>
        <span class="track">
          <span class="axis"></span>
          <span class="seg {tone(s.part)}" style="left:{left(s)}%;width:max({width(s)}%, 2px)"></span>
        </span>
        <span class="num">{pp(s.part)}</span>
        <span class="range">{pct(s.from)} → {pct(s.to)}</span>
      </div>
    {/each}
    {#if e.available}
      <div class="step">
        <span class="name">Result</span>
        <span class="track">
          <span class="axis"></span>
          <span class="dot" style="left:{100 * e.result}%"></span>
        </span>
        <span class="num">{e.result ? 'Won' : 'Lost'}</span>
        <span class="range"></span>
      </div>
    {/if}
    <div class="scale" aria-hidden="true"><span></span><span class="ticks"><i>0%</i><i>50%</i><i>100%</i></span></div>
  </div>

  {#if e.available}
    <div class="chips">
      {#each e.chips as c}
        {#if c.players.length}
          <button class="chip {tone(c.share_pp)}" aria-expanded={openBot} onclick={() => (openBot = !openBot)}>
            <b>{c.name} {openBot ? '▾' : '▸'}</b><span>{c.label}</span><em>{pp(c.share_pp, 1)}</em>
          </button>
          {#if openBot}
            {#each c.players as p}
              <div class="chip sub {tone(p.share_pp)}"><b>{p.name}</b><span>{p.label}</span><em>{pp(p.share_pp, 1)}</em></div>
            {/each}
          {/if}
        {:else}
          <div class="chip {tone(c.share_pp)}"><b>{c.name}</b><span>{c.label}</span><em>{pp(c.share_pp, 1)}</em></div>
        {/if}
      {/each}
    </div>
    <p class="key">Each chip's share of the early game, against what this draft was expected to do. Single games are noisy.</p>

    <button class="toggle" aria-expanded={openTable} onclick={() => (openTable = !openTable)}>{openTable ? 'Hide' : 'Show'} every stat</button>
    {#if openTable}
      <div class="tables">
        <table>
          <thead><tr><th>Stat</th><th class="n">Actual</th><th class="n">Expected</th><th class="n">SD</th><th class="n">z</th><th>Label</th></tr></thead>
          <tbody>
            {#each rows as c}
              <tr class="group"><td colspan="6">{c.name} <span>{pp(c.share_pp, 1)} · {c.label}</span></td></tr>
              {#if !c.stats.some((s) => s.stat === c.headline.stat)}
                <tr class="basis">
                  <td>{STATS[c.headline.stat][0]} (what the label reads)</td><td class="n">{value(c.headline.stat, c.headline.actual)}</td>
                  <td class="n">{value(c.headline.stat, c.headline.expected)}</td><td class="n">{c.headline.sd.toFixed(1)}</td>
                  <td class="n">{signedNum(c.headline.z, 1)}</td><td>{c.headline.label}</td>
                </tr>
              {/if}
              {#each c.stats as s}
                <tr>
                  <td>{STATS[s.stat][0]}</td><td class="n">{value(s.stat, s.actual)}</td><td class="n">{value(s.stat, s.expected)}</td>
                  <td class="n">{s.sd.toFixed(1)}</td><td class="n">{signedNum(s.z, 1)}</td><td>{s.label}</td>
                </tr>
              {/each}
            {/each}
          </tbody>
        </table>
        <table class="after">
          <thead><tr><th>After 15:00</th><th class="n">Us</th><th class="n">Them</th></tr></thead>
          <tbody>
            {#each after as [name, key]}
              <tr><td>{name}</td><td class="n">{e.detail[key][0]}</td><td class="n">{e.detail[key][1]}</td></tr>
            {/each}
            {#if e.detail.gold_25}
              <tr><td>{STATS.gold_25[0]}: expected {value('gold_25', e.detail.gold_25.expected)}, {e.detail.gold_25.label}</td>
                <td class="n" colspan="2">{value('gold_25', e.detail.gold_25.actual)}</td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {/if}
  {:else}
    <p class="key">Execution data not available: the game's timeline hasn't arrived, or it ended before 15:00.</p>
  {/if}
</section>

<style>
  .strip { background: var(--panel); border-radius: 8px; padding: 12px 16px; margin-top: 10px; display: grid; gap: 10px; }
  h3 { margin: 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b96a3; font-weight: 600; }
  .verdict { margin: 0; font-size: 16px; color: #c9d1d9; }
  .verdict strong { color: var(--text); }
  .fall { display: grid; gap: 4px; }
  .step, .scale { display: grid; grid-template-columns: 92px minmax(0, 1fr) 56px 92px; gap: 10px; align-items: center; }
  .name { font-size: 13px; color: #c9d1d9; }
  .track { position: relative; height: 14px; background: #1f2630; border-radius: 3px; }
  .axis { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: #8b96a3; }
  .seg { position: absolute; top: 2px; bottom: 2px; border-radius: 4px; }
  .seg.up { background: #1baf7a; }
  .seg.down { background: #c98500; }
  .seg.flat { background: #8b96a3; }
  .dot { position: absolute; top: 50%; width: 10px; height: 10px; border-radius: 50%; background: var(--text); border: 2px solid var(--panel); transform: translate(-50%, -50%); }
  .num { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; }
  .range { font-size: 12px; color: #8b96a3; font-variant-numeric: tabular-nums; }
  .ticks { display: flex; justify-content: space-between; font-size: 11px; color: #8b96a3; }
  .ticks i { font-style: normal; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: stretch; }
  .chip { display: grid; gap: 1px; min-width: 104px; padding: 6px 10px; background: var(--panel-2, #1f2630); border: 0; border-left: 4px solid #8b96a3;
          border-radius: 4px; color: var(--text); font: inherit; font-size: 13px; text-align: left; }
  button.chip { cursor: pointer; }
  .chip.up { border-left-color: #1baf7a; }
  .chip.down { border-left-color: #c98500; }
  .chip.sub { min-width: 88px; opacity: 0.92; }
  .chip span { color: #c9d1d9; }
  .chip em { font-style: normal; font-weight: 700; font-variant-numeric: tabular-nums; }
  .key { margin: 0; font-size: 12px; color: #8b96a3; }
  .toggle { justify-self: start; background: none; border: 1px solid var(--line); color: var(--text); border-radius: 6px; padding: 4px 10px; cursor: pointer; font: inherit; font-size: 13px; }
  .tables { display: grid; grid-template-columns: minmax(0, 2fr) minmax(220px, 1fr); gap: 16px; align-items: start; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8b96a3; font-weight: 500; padding: 3px 6px; border-bottom: 1px solid var(--line); }
  td { padding: 3px 6px; border-bottom: 1px solid #1f2630; }
  .n { text-align: right; font-variant-numeric: tabular-nums; }
  .group td { padding-top: 8px; font-weight: 700; }
  .group span { font-weight: 400; color: #8b96a3; }
  .basis td { color: #8b96a3; }
  @media (max-width: 1100px) {
    .tables { grid-template-columns: 1fr; }
    .step, .scale { grid-template-columns: 76px minmax(0, 1fr) 48px; }
    .range { display: none; }
  }
</style>
