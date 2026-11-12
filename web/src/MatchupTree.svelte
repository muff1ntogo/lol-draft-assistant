<script>
  // Prep: the Matchup tree (docs/matchup-archetypes.md, Stage 7). Per role, each enemy archetype, up to 3 answers (the pool's
  // best if it covers it, then the best outside the pool), what the archetypes alone would suggest, and a flag where the
  // pool's best answer is below -1.5pp.
  import Champ from './Champ.svelte';
  import { patchLabel } from './patch.js';
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Table from './ui/Table.svelte';
  import Tag from './ui/Tag.svelte';

  let data = $state(null);
  let failed = $state('');
  let open = $state({}); // "role:archetype" -> the gap's detail is showing
  let stop = $state(null); // index into HALF_LIVES
  let saving = $state(false);

  // Small half-lives get more stops: that's where the newest patch starts to outweigh a much bigger older one.
  // The last stop, 0, is no decay: every game counts the same.
  const HALF_LIVES = [0.1, 0.15, 0.25, 0.5, 1, 2, 3, 6, 12, 24, 0];
  const nearest = (h) => (h === 0 ? HALF_LIVES.length - 1
    : HALF_LIVES.reduce((best, x, i) => (x && Math.abs(x - h) < Math.abs(HALF_LIVES[best] - h) ? i : best), 0));
  const counterTitle = (a) => `${a.name} averages ${signed(a.own_pp)}pp into this role's archetypes, and gains ${signed(a.counter_pp)}pp here beyond what the role's other champions gain`;

  async function load() {
    try {
      const r = await fetch('/api/prep/matchup-tree');
      if (!r.ok) throw new Error(`the app answered ${r.status}`);
      data = await r.json();
      stop = nearest(data.half_life);
      failed = '';
    } catch (e) {
      failed = `Couldn't read the matchup tree: ${e.message}`;
    }
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
  const tone = (x) => (x > 0.5 ? 'up' : x < -0.5 ? 'down' : '');
  const words = (a) => a.replaceAll('_', ' ');
  const measured = (c) => (c >= 0.5 ? 'measured' : c >= 0.15 ? 'partly measured' : 'archetype estimate');
  const toggle = (k) => (open[k] = !open[k]);
  // how well the pool covers an archetype, in words (the chip's colour used to carry this)
  const COVER = { green: 'covered', amber: 'thin', red: 'gap', grey: 'no data' };
</script>

{#if failed}
  <p class="error" role="alert">⚠ {failed}</p>
{:else if data}
  <p class="source">{#if data.matches.length}From {data.matches.map((m) => `${m.matches.toLocaleString()} matches on ${patchLabel(m.patch)}`).join(', ')}:{:else}No crawled matches yet:{/if}
    {data.master ? 'Master, Grandmaster and Challenger Solo/Duo' : 'every tier from Iron to Emerald pooled'},
    {data.half_life === 0 ? 'every game counting the same' : 'newer patches counting more'}.
    {data.master ? 'The Blind pick, Setup and Counters line under each role still reads the main data.' : '"Most picked" is each player\'s tier.'}
    {#if !data.master}Master+ crawl: <span class="mono">{data.master_matches.toLocaleString()}</span> of <span class="mono">{data.master_needed.toLocaleString()}</span> matches; the tree moves to Master+ once it has them.{/if}</p>
  <Toolbar label="Matchup tree options">
    <ToolGroup label="Patch weighting">
      <input class="range" type="range" min="0" max={HALF_LIVES.length - 1} step="1" bind:value={stop} onchange={save} disabled={saving}
             aria-label="Patch half-life: left favours new patches" />
      <span class="val">{#if HALF_LIVES[stop] === 0}Every game counts the same{:else}Older patches halve every <span class="mono">{HALF_LIVES[stop]}</span> {HALF_LIVES[stop] === 1 ? 'patch' : 'patches'}{/if}</span>
    </ToolGroup>
    {#if shares.length}
      <ToolGroup label="Share of the games used">
        <span class="shares">{#each shares as m, i}{i ? ' · ' : ''}{patchLabel(m.patch)} <span class="mono">{percent(m.share)}</span>
          <span class="dim">({m.matches.toLocaleString()} × {percent(m.weight)})</span>{/each}</span>
      </ToolGroup>
    {/if}
  </Toolbar>
  {#each data.roles as role}
    {@const rows = role.rows.filter((r) => r.champs.length)}
    <section class="role">
      <div class="rolehead">
        <h2>{role.role_label} <span class="sub">{data.master ? 'Master+' : role.bracket}</span></h2>
        <div class="health">
          <span class:missing={!role.health.blind.ok}>{role.health.blind.ok ? '' : '⚠ '}Blind pick: {role.health.blind.ok ? role.health.blind.name : 'none safe'}</span>
          {#if role.health.setup.applies}<span class:missing={!role.health.setup.ok}>{role.health.setup.ok ? '' : '⚠ '}Setup: {role.health.setup.ok ? role.health.setup.name : 'none'}</span>{/if}
          {#if role.health.counter.main}<span class:missing={!role.health.counter.ok}>{role.health.counter.ok ? '' : '⚠ '}Counters to {role.health.counter.main}'s worst: <span class="mono">{role.health.counter.worst.filter((w) => w.answered).length}</span> of <span class="mono">{role.health.counter.worst.length}</span></span>{/if}
        </div>
      </div>
      {#if role.rows.some((r) => r.chip !== 'none')}
        <div class="cover">
          <span class="label-caps">Pool cover</span>
          {#each role.rows.filter((r) => r.chip !== 'none') as r}
            {@const k = `${role.role}:${r.archetype}`}
            {#if r.chip === 'red'}
              <Tag warn onclick={() => toggle(k)} pressed={!!open[k]} title="Show ban candidates and who answers it">{words(r.archetype)}: gap</Tag>
            {:else}
              <Tag bare title={r.chip === 'grey' ? 'Too few games behind this answer to call it covered or thin' : null}>{words(r.archetype)}: {COVER[r.chip]}</Tag>
            {/if}
          {/each}
        </div>
      {/if}
      {#each role.rows.filter((r) => r.chip === 'red' && open[`${role.role}:${r.archetype}`]) as r}
        <p class="detail">
          <strong>{words(r.archetype)}:</strong>
          ban {r.ban_candidates.map((b) => `${b.name} (${signed(b.pp)}pp)`).join(', ') || '—'}
          {#if r.answering.length}· answered by {r.answering.map(words).join(', ')} champions{/if}
        </p>
      {/each}
      <Table sticky={false}>
        <thead><tr><th scope="col">Enemy archetype</th><th scope="col">Most picked</th><th scope="col">Best answers</th>
          <th scope="col" class="num" title="Lane and game matchup blended, pp">Blend</th><th scope="col" class="num">Lane</th>
          <th scope="col" class="num">Gold at 15</th><th scope="col" class="num">Game</th>
          <th scope="col" class="num" title="What the two archetypes alone suggest">Expected</th><th scope="col">Plan</th></tr></thead>
        <tbody>
          {#each rows as r}
            {@const span = Math.max(1, r.answers.length)}
            {#each r.answers.length ? r.answers : [null] as a, i}
              <tr class:more={i > 0} class:last={i === span - 1}>
                {#if i === 0}
                  <td rowspan={span} class="arch"><span class="archname"><strong>{words(r.archetype)}</strong>{#if r.gap}<Tag bare warn>gap</Tag>{/if}</span>
                    {#if !r.answers.some((x) => x.in_pool)}<small>{r.best ? `Pool's best: ${r.best.name} ${signed(r.best.pp)}` : 'Nothing in the pool'}</small>{/if}</td>
                  <td rowspan={span}><span class="icons">{#each r.champs as c}<span title={c.name}><Champ champ={c.card} size={24} /></span>{/each}</span></td>
                {/if}
                {#if a}
                  <td class="answer"><span class="ans"><Champ champ={a.card} size={i === 0 ? 24 : 20} /><strong>{a.name}</strong>
                    <span class="how">{measured(a.confidence)}</span>
                    {#if a.in_pool}<Tag title="From {role.role_label}'s pool">in pool</Tag>{/if}
                    {#if a.counter}<Tag title={counterTitle(a)}>counter <span class="mono {tone(a.counter_pp)}">{signed(a.counter_pp)}pp</span>{a.own_pp < 0 ? ', weak overall' : ''}</Tag>{/if}</span></td>
                  <td class="num mono {tone(a.pp)}">{signed(a.pp)}</td>
                  <td class="num mono">{signed(a.lane_pp)}</td>
                  <td class="num mono">{signed(a.gold, 0)}</td>
                  <td class="num mono">{signed(a.game_pp)}</td>
                  <td class="num mono">{signed(a.expectation, 2)}</td>
                {:else}
                  <td class="none" colspan="6">No answers</td>
                {/if}
                {#if i === 0}<td rowspan={span} class="plan">{r.plan ?? ''}</td>{/if}
              </tr>
            {/each}
          {:else}
            <tr><td colspan="9" class="none">No matchups for this role yet: they appear once the crawl has games on this patch</td></tr>
          {/each}
        </tbody>
      </Table>
    </section>
  {/each}
{:else}
  <p class="source">Loading…</p>
{/if}

<style>
  .error { color: var(--error); font-size: 13px; }
  .source { font-size: 13px; color: var(--text-3); margin: 0 0 12px; max-width: 120ch; }
  .source .mono { color: var(--text-2); }
  .range { width: 160px; }
  .val, .shares { font-size: 13px; color: var(--text-2); }
  .dim { color: var(--text-3); }
  .role { padding: 20px 0 8px; border-top: 1px solid var(--hairline); }
  .rolehead { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 24px; margin-bottom: 8px; }
  h2 { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 6px; }
  .health { display: flex; flex-wrap: wrap; gap: 4px 20px; font-size: 12px; color: var(--text-3); }
  .health .missing { color: var(--warn); }
  .cover { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 8px; }
  .detail { font-size: 13px; margin: 4px 0 8px; color: var(--text-2); }
  .detail strong { color: var(--text-1); }

  .archname { display: inline-flex; align-items: baseline; gap: 8px; }
  .arch strong { font-weight: 600; text-transform: capitalize; }
  .arch small { display: block; font-size: 11px; color: var(--text-3); }
  .icons { display: inline-flex; gap: 2px; }
  .ans { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  .ans strong { font-weight: 600; }
  .how { font-size: 12px; color: var(--text-3); }
  /* an archetype's answers are one group: the rule comes after the last of them */
  .role :global(tbody tr:not(.last) > td:not([rowspan])) { border-bottom-color: transparent; }
  .role :global(tr.more td) { color: var(--text-2); }
  .plan { font-size: 12px; color: var(--text-3); }
  .none { color: var(--text-3); }
</style>
