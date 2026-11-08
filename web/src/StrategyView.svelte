<script>
  // Live's Strategy tab (docs/strategy.md): per locked champion of ours, runes and build with why, and the lane plan;
  // once all ten are locked, the team's win conditions and the threats to track.
  import Champ from './Champ.svelte';

  let { strategy } = $props();
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
  const signed = (x) => (x == null ? '' : `${x > 0 ? '+' : x < 0 ? '−' : '±'}${Math.abs(x)}`);
  const stat = (c) => `${pct(c.share)} pick${c.wr != null ? ` · ${pct(c.wr)} win (${c.games} g)` : ''}`;
</script>

<main class="strategy">
  {#if !strategy || !strategy.players.length}
    <p class="muted big">Strategy fills in as we lock champions: runes, build and a lane plan per player, then the
      team's game plan once all ten are locked.</p>
  {:else}
    {#if strategy.complete && strategy.team}
      <section class="team">
        <h2>Game plan <small>{strategy.team.ours.style}{strategy.team.ours.timing ? ` · ${strategy.team.ours.timing}` : ''} vs
          {strategy.team.theirs.style}{strategy.team.theirs.timing ? ` · ${strategy.team.theirs.timing}` : ''}</small></h2>
        <ul>{#each strategy.team.lines as l}<li>{l}</li>{/each}</ul>
        {#if strategy.threats?.length}
          <h3>Track these</h3>
          <div class="threats">
            {#each strategy.threats as t}
              <div class="threat">
                <Champ champ={t.champ} size={44} />
                <div>
                  <strong>{t.champ?.name}</strong> <span class="muted">{t.role_label}{t.player ? ` · ${t.player}` : ''}</span>
                  <div class="muted">{t.reason}</div>
                  {#if t.answer}<div>{t.answer}</div>{/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </section>
    {:else}
      <p class="muted">The game plan appears once all ten champions are locked.</p>
    {/if}

    <div class="players">
      {#each strategy.players as p}
        <article class="player">
          <header>
            <Champ champ={p.champ} size={48} />
            <div>
              <div class="pname">{p.champ?.name} <small>{p.role_label}</small></div>
              <div class="muted">{#if p.opponent}vs {p.opponent.name}{:else}their {p.role_label} not locked yet{/if}</div>
            </div>
            {#if p.opponent}<span class="vs"><Champ champ={p.opponent} size={36} /></span>{/if}
          </header>

          <h4>Lane</h4>
          <p class="summary">{p.lane.summary}</p>
          {#if p.lane.lines.length}<ul>{#each p.lane.lines as l}<li>{l}</li>{/each}</ul>{/if}
          {#if p.lane.matchup_pp != null && p.opponent}
            <div class="muted small">Game matchup {signed(p.lane.matchup_pp)}pp{#if p.lane.archetypes?.[0] && p.lane.archetypes?.[1]} · {p.lane.archetypes[0].replaceAll('_', ' ')} vs {p.lane.archetypes[1].replaceAll('_', ' ')}{/if}</div>
          {/if}

          <h4>Build {#if p.build}<small>{p.build.vs_opponent ? `vs ${p.build.opponent_name}` : 'overall'} · what: {p.build.ref_label} ({p.build.ref_games} g) · how well: your bracket ({p.build.bracket_games} g{p.build.bracket_wr != null ? `, ${pct(p.build.bracket_wr)}` : ''})</small>{/if}</h4>
          {#if !p.build}
            <p class="muted small">No runes or items crawled for {p.champ?.name} {p.role_label} yet. Run the crawl, or
              <code>data.backfill</code> for matches crawled before builds were stored.</p>
          {:else}
            {#if p.build.names_missing}<p class="muted small">Item and rune names aren't downloaded yet: run <code>data.ddragon</code>.</p>{/if}
            <div class="rows">
              {#if p.build.keystones.length}
                <div class="row"><span class="k">Keystone</span>
                  <span>{#each p.build.keystones as k, i}<span class:first={i === 0} class="opt">{k.name} <small>{stat(k)}</small></span>{/each}</span></div>
              {/if}
              {#if p.build.page}
                <div class="row"><span class="k">Runes</span><span>{p.build.page.runes.join(' · ')} <small>{stat(p.build.page)}</small></span></div>
              {/if}
              {#if p.build.spells}
                <div class="row"><span class="k">Spells</span><span>{p.build.spells.names.join(' + ')} <small>{stat(p.build.spells)}</small></span></div>
              {/if}
              {#if p.build.core.length}
                <div class="row"><span class="k">Core</span>
                  <ol class="core">{#each p.build.core as it}<li>{it.name} <small>{stat(it)}</small></li>{/each}</ol></div>
              {/if}
              {#if p.build.boots}
                <div class="row"><span class="k">Boots</span><span>{p.build.boots.name} <small>{stat(p.build.boots)}</small>{#if p.build.boots.why} <em>({p.build.boots.why})</em>{/if}</span></div>
              {/if}
              {#if p.build.situational.length}
                <div class="row"><span class="k">This game</span>
                  <span>{#each p.build.situational as it}<span class="opt">{it.name} <em>({it.why})</em></span>{/each}</span></div>
              {/if}
              {#if p.build.others.length}
                <div class="row"><span class="k">Also built</span><span class="muted">{p.build.others.map((o) => o.name).join(', ')}</span></div>
              {/if}
            </div>
            {#if p.build.reasons.length}<ul class="why">{#each p.build.reasons as r}<li>{r}</li>{/each}</ul>{/if}
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</main>

<style>
  .strategy { padding: 18px 28px 80px; }
  .muted { color: var(--muted); }
  .big { font-size: 18px; text-align: center; margin-top: 60px; }
  .small { font-size: 13px; }
  h2 { font-size: 22px; margin: 0 0 8px; }
  h2 small, h4 small, .pname small { font-size: 13px; font-weight: 400; color: var(--muted); margin-left: 6px; }
  h3 { font-size: 16px; margin: 14px 0 8px; }
  h4 { font-size: 14px; margin: 14px 0 4px; color: var(--gold); }
  h4 small { display: block; margin: 2px 0 0; }
  ol.core { margin: 0; padding-left: 18px; }
  ul { margin: 4px 0; padding-left: 20px; line-height: 1.5; }
  .team { background: var(--panel); border-radius: 10px; padding: 16px 20px; margin-bottom: 18px; }
  .threats { display: flex; gap: 24px; flex-wrap: wrap; }
  .threat { display: flex; gap: 10px; align-items: flex-start; max-width: 520px; }
  .players { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 16px; }
  @media (max-width: 520px) { .players { grid-template-columns: 1fr; } }
  .player { background: var(--panel); border-radius: 10px; padding: 14px 18px; }
  header { display: flex; align-items: center; gap: 12px; }
  .pname { font-size: 18px; font-weight: 600; }
  .vs { margin-left: auto; }
  .summary { margin: 2px 0; }
  .rows { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
  .row { display: grid; grid-template-columns: 80px 1fr; gap: 8px; }
  .k { color: var(--muted); }
  .opt { margin-right: 10px; }
  .opt.first { font-weight: 600; }
  small { color: var(--muted); }
  em { color: #8ec5ff; font-style: normal; }
  .why { color: var(--muted); font-size: 13px; }
  code { font-size: 12px; }
</style>
