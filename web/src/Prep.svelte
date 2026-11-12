<script>
  // Prep (docs/ui-redesign.md, step 3): build a plan from our pools (synergy ring + plan), see its bans, pick order and
  // risks, pin it for the next game. The Matchup tree is the page's other mode. The patch overview (no plan needed:
  // priority bans, best 3, blind and flex picks, lane ban tables from ban-score.md) stays folded at the bottom.
  import Champ from './Champ.svelte';
  import PlanSynergy from './PlanSynergy.svelte';
  import PlanBansPicks from './PlanBansPicks.svelte';
  import MatchupTree from './MatchupTree.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Table from './ui/Table.svelte';
  import { signed } from './format.js';

  const savedPlan = (() => { try { return JSON.parse(localStorage.getItem('prep-plan') || '{}'); } catch { return {}; } })();
  let locks = $state(savedPlan);
  let planView = $state(null);
  let tab = $state('plan'); // plan | tree
  // the plan the bottom half works on: the locks, plus champions locked in a live draft ("use current draft")
  const planLocks = $derived(planView ? Object.fromEntries(planView.plan.champs.map((c) => [c.role, c.id])) : locks);
  $effect(() => {
    try { localStorage.setItem('prep-plan', JSON.stringify(locks)); } catch { /* not remembered */ }
  });

  let data = $state(null);
  let sheet = $state(null);
  fetch('/api/prep/lane-bans').then((r) => r.json()).then((d) => (data = d));
  fetch('/api/prep').then((r) => r.json()).then((d) => (sheet = d));
  const pct = (p) => `${Math.round(p * 100)}%`;

  // ---- "Pin for next game": the page's one primary action (Live follows the pinned plan in the next champ select) ----
  let pinned = $state(null);
  let pinMsg = $state('');
  let pinFailed = $state(false);
  fetch('/api/plan/pin').then((r) => r.json()).then((d) => (pinned = d.pinned));
  const sameAsPinned = $derived(pinned && JSON.stringify(Object.entries(pinned.locks).sort())
                                         === JSON.stringify(Object.entries(planLocks).sort()));
  const hasPlan = $derived(Object.keys(planLocks).length > 0);
  async function pin() {
    pinMsg = '';
    pinFailed = false;
    try {
      const r = await fetch('/api/plan/pin', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ locks: planLocks }) });
      const body = await r.json();
      if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'the app refused it');
      pinned = body.pinned;
      pinMsg = 'Pinned: Live follows this plan in the next champ select.';
    } catch (e) {
      pinFailed = true;
      pinMsg = `Couldn't pin the plan: ${e.message}`;
    }
  }
  async function unpin() {
    await fetch('/api/plan/pin', { method: 'DELETE' });
    pinned = null;
    pinMsg = '';
  }

  const LANE_COLUMNS = [
    { key: 'opp', label: 'Opponent', width: '112px' },
    { key: 'pr', label: 'Pick rate', num: true, width: '84px', format: (v) => `${v.toFixed(1)}%`, title: 'How often they pick it' },
    { key: 'resp', label: 'Answer', num: true, width: '72px', format: (v) => signed(v, 1), title: "Our best answer's matchup, pp" },
    { key: 'via', label: 'With', format: (v) => v.replace(/[()]/g, '') },
    { key: 'ban', label: 'Lane ban', num: true, width: '80px', format: (v) => signed(v, 2), tone: (v, r) => (r.band === 'positive' ? 'up' : r.band === 'negative' ? 'down' : null),
      title: "How much banning it improves this player's expected lane matchup, pp" },
    { key: 'team_ban_value', label: 'Team ban value', num: true, width: '124px', format: (v) => signed(v, 4), title: 'Meta threat + our matchups + counters, log-odds' },
  ];
</script>

<main class="console">
  <PageHeader title="Prep" description="Build a plan from our pools, see its bans, pick order and risks, and pin it for the next game.">
    {#snippet actions()}
      <Segmented label="Prep view" bind:value={tab} options={[['plan', 'Plan'], ['tree', 'Matchup tree']]} />
      {#if tab === 'plan'}
        {#if pinned}<Button variant="ghost" onclick={unpin} title="Live stops following the pinned plan">Unpin</Button>{/if}
        {#if pinned && sameAsPinned}
          <Button variant="primary" size="md" disabled title="This plan is the one Live follows next game">Pinned for next game</Button>
        {:else}
          <Button variant="primary" size="md" disabled={!hasPlan} onclick={pin}
                  title={hasPlan ? 'Live follows this plan in the next champ select' : 'Lock at least one champion into the plan first'}>
            {pinned ? 'Pin this plan instead' : 'Pin for next game'}</Button>
        {/if}
      {/if}
    {/snippet}
  </PageHeader>
  {#if pinMsg && tab === 'plan'}<p class="pinmsg" class:failed={pinFailed} role={pinFailed ? 'alert' : 'status'}>{pinFailed ? '⚠ ' : ''}{pinMsg}</p>{/if}

  {#if tab === 'tree'}
    <MatchupTree />
  {:else}
  <PlanSynergy bind:locks onplan={(d) => (planView = d)} />
  <PlanBansPicks locks={planLocks} />

  <details class="overview">
    <summary><span class="sumtitle">Patch overview</span> <span class="sumnote">without a plan: priority bans, best 3, blind and flex picks, lane ban tables</span></summary>
    {#if sheet}
      <div class="sheet">
        {#each ['blue', 'red'] as side}
          <section class="bans {side}">
            <h3>Priority bans <span class="sub">{side} side</span></h3>
            <ol class="rows">
              {#each sheet.bans[side].bans as b, i}
                <li><span class="n mono">{i + 1}</span><Champ champ={b.champ} size={28} /><div><strong>{b.champ.name}</strong><span>{b.reason}</span></div></li>
              {:else}<li class="none">No useful ban</li>{/each}
            </ol>
          </section>
        {/each}
        <p class="cap wide">{sheet.notes.bans}</p>

        <section class="wide">
          <h3>Best 3 this patch</h3>
          <div class="best">
            {#each sheet.best as r}
              <div>
                <h4 class="label-caps">{r.role_label} <span class="sub">{r.bracket}</span></h4>
                <div class="rows">
                  {#each r.best as b}
                    <div class="pickrow">
                      <Champ champ={b.champ} size={28} />
                      <div><strong>{b.champ.name}</strong><span>{b.tier} · <span class="mono">{b.win_rate == null ? '—' : pct(b.win_rate)}</span> WR · comfort <span class="mono">{b.comfort}</span></span></div>
                    </div>
                  {:else}<p class="none">Empty pool</p>{/each}
                </div>
              </div>
            {/each}
          </div>
        </section>

        <section>
          <h3>Safe blind first picks</h3>
          <div class="rows">
            {#each sheet.blind_safe as b}
              <div class="pickrow">
                <Champ champ={b.champ} size={28} />
                <div><strong>{b.champ.name}</strong>
                  <span>{b.role_label} · <span class="mono">{pct(b.win_prob)}</span> as blue's first pick{#if b.worst_counter} · worst counter {b.worst_counter.name}{/if}</span></div>
              </div>
            {:else}<p class="none">None of our pool champions is safe to blind pick right now.</p>{/each}
          </div>
          <p class="cap">{sheet.notes.blind_safe}</p>
        </section>

        <section>
          <h3>Flex picks</h3>
          <div class="rows">
            {#each sheet.flex as f}
              <div class="pickrow">
                <Champ champ={f.champ} size={28} />
                <div><strong>{f.champ.name}</strong><span>{Object.entries(f.tiers).map(([r, t]) => `${r} ${t}`).join(' · ')}</span></div>
              </div>
            {:else}<p class="none">No champion is in two of our pools.</p>{/each}
          </div>
        </section>
      </div>
    {/if}

    <h3 class="lanehead">Lane ban tables</h3>
    <p class="cap">Per role: how much banning each opponent improves that player's expected lane matchup (pp), and its
      final team ban value (meta threat + our matchups + counters, log-odds).</p>
    <div class="roles">
      {#each data?.roles ?? [] as role}
        <section>
          <h4 class="label-caps">{role.role_label} <span class="sub">{role.player_bracket} · μ <span class="mono">{signed(role.mu, 2)}pp</span></span></h4>
          <Table dense sticky={false} columns={LANE_COLUMNS} rows={role.rows.slice(0, 15)} empty="No lane data for this role yet" />
        </section>
      {:else}
        <p class="none">{data ? 'No lane ban tables yet' : 'Loading…'}</p>
      {/each}
    </div>
  </details>
  {/if}
</main>

<style>
  .pinmsg { margin: -8px 0 12px; font-size: 13px; color: var(--text-2); text-align: right; }
  .pinmsg.failed { color: var(--error); }

  .overview { margin-top: 32px; border-top: 1px solid var(--hairline); }
  .overview > summary { cursor: pointer; padding: 12px 0; list-style-position: inside; color: var(--text-3); }
  .overview > summary:hover .sumtitle { color: var(--text-1); }
  .sumtitle { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; color: var(--text-2); }
  .overview[open] .sumtitle { color: var(--text-1); }
  .sumnote { font-size: 13px; margin-left: 8px; }

  h3 { margin: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  h4 { margin: 0 0 6px; }
  .sub { font-size: 12px; font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--text-3); margin-left: 6px; }
  .cap { margin: 8px 0 0; font-size: 12px; color: var(--text-3); max-width: 96ch; }
  .none { margin: 0; padding: 8px 0; font-size: 13px; color: var(--text-3); }

  .sheet { display: grid; grid-template-columns: 1fr 1fr; gap: 24px 32px; margin: 8px 0 32px; }
  .wide { grid-column: 1 / -1; }
  /* the side is the list's edge (colour rule 2: structure only) */
  .bans { padding-left: 12px; }
  .bans.blue { box-shadow: inset 3px 0 0 var(--side-blue); }
  .bans.red { box-shadow: inset 3px 0 0 var(--side-red); }
  .rows { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--hairline); }
  .rows li, .pickrow { display: flex; align-items: center; gap: 10px; min-height: 40px; padding: 4px 0; border-bottom: 1px solid var(--hairline); }
  .rows li div, .pickrow div { display: flex; flex-direction: column; min-width: 0; }
  .rows strong { font-size: 14px; font-weight: 600; }
  .rows li span, .pickrow div > span { font-size: 12px; color: var(--text-2); }
  .rows li .n { width: 16px; text-align: right; color: var(--text-3); }
  .best { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 24px; }
  .lanehead { margin-top: 8px; }
  .roles { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 24px 32px; margin-top: 16px; }
  @media (max-width: 1180px) { .sheet { grid-template-columns: 1fr; } .best { grid-template-columns: 1fr 1fr; } .roles { grid-template-columns: minmax(0, 1fr); } }
</style>
