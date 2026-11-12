<script>
  // Prep page, bottom half (docs/synergy-v2.md, Stage 5): bans and picks for the current plan (the locks above),
  // for our side and the draft format. Ban lists: Win lane (each plan champion is its role's only answer),
  // Win teamfights (enemy champions that hurt our plan's team model), Combined (ban value + teamfights).
  // Pinning the plan is Prep.svelte's (the page header's primary action).
  import Champ from './Champ.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Table from './ui/Table.svelte';
  import Tag from './ui/Tag.svelte';

  let { locks = {} } = $props();

  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const saved = (() => { try { return JSON.parse(localStorage.getItem('prep-bans') || '{}'); } catch { return {}; } })();
  let side = $state(saved.side ?? 'blue');
  let format = $state(saved.format ?? 'tournament');
  let data = $state(null);
  let error = $state(null);
  let loading = $state(false);

  $effect(() => {
    try { localStorage.setItem('prep-bans', JSON.stringify({ side, format })); } catch { /* not remembered */ }
  });

  // While the page opens (its first second) requests go out at once, so it appears with them (loading.js); after that
  // edits are debounced. A request identical to the last one sent is skipped: Prep hands over the same locks again
  // once the synergy view has loaded.
  let timer;
  let lastBody = null;
  const opened = performance.now();
  $effect(() => {
    const body = JSON.stringify({ locks, side, format });
    clearTimeout(timer);
    if (body === lastBody) return;
    if (performance.now() - opened < 1000) { load(body); return; }
    timer = setTimeout(() => load(body), 150);
  });

  let seq = 0;
  async function load(body) {
    lastBody = body;
    const mine = ++seq; // only the newest answer is shown, whichever order they arrive in
    loading = true;
    const r = await fetch('/api/prep/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    const out = await r.json();
    if (mine !== seq) return;
    loading = false;
    if (!r.ok) { error = out.detail; return; }
    error = null;
    data = out;
  }

  const signed = (x, d = 1) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(d)}`);
  const cls = (v) => (v > 0.5 ? 'up' : v < -0.5 ? 'down' : '');
  const pct = (x) => `${Math.round(x * 100)}%`;
  const LISTS = [
    ['lane', 'Win lane', 'Bans that protect each plan champion\'s lane (roles without a plan use the pool).'],
    ['teamfight', 'Win teamfights', 'Enemy champions that most lower our plan\'s team model, × how likely they pick them.'],
    ['combined', 'Combined', 'Ban value (meta threat, our matchups, counters to the plan) plus Win teamfights.'],
  ];
</script>

<section class="half" aria-busy={loading}>
  <div class="head">
    <h2>Bans and picks for the plan</h2>
    <span class="label-caps">Side</span>
    <Segmented label="Our side" bind:value={side} options={[['blue', 'Blue'], ['red', 'Red']]} />
    <span class="label-caps">Format</span>
    <Segmented label="Draft format" bind:value={format} options={[['tournament', 'Ranked 5s'], ['flex', 'Flex']]} />
    {#if loading}<span class="note">Updating…</span>{/if}
  </div>
  {#if error}<p class="error" role="alert">⚠ {typeof error === 'string' ? error : JSON.stringify(error)}</p>{/if}
  {#if data}
    {#if data.empty}<p class="note lead">No plan yet: the lists use each player's whole pool until champions are locked in above.</p>{/if}
    <div class="three">
      {#each LISTS as [k, title, hint]}
        <div class="col">
          <h3>{title}</h3>
          <p class="hint">{hint}</p>
          <Table sticky={false}>
            <thead><tr><th scope="col">Ban</th><th scope="col" class="num" title="Ban value, pp of win chance">Value</th></tr></thead>
            <tbody>
              {#each data.bans[k] as b}
                <tr>
                  <td><div class="who"><Champ champ={b.champ} size={28} />
                    <div class="txt"><strong>{b.champ.name}{#if b.owner} <Tag>{LABELS[b.owner]}</Tag>{/if}</strong>
                      <span>{b.reason}</span>
                      {#each b.warnings as w}<span class="warn">⚠ {w}</span>{/each}</div></div></td>
                  <td class="num mono">{signed(b.value_pp, 2)}</td>
                </tr>
              {:else}<tr><td colspan="2" class="none">No useful ban</td></tr>{/each}
            </tbody>
          </Table>
        </div>
      {/each}
    </div>

    <div class="three picks">
      <div class="col">
        <h3>Pick order <span class="sub">{side} side · {format === 'flex' ? 'Flex' : 'Ranked 5s'}</span></h3>
        <!-- the side is the list's edge; the slot code itself stays neutral (colour rule 2) -->
        <div class="orderlist {side}">
          {#each data.picks.order as o}
            <div class="order">
              <span class="slot mono">{o.slot}</span>
              <Champ champ={o.champ} size={28} />
              <div class="txt"><span class="line">{o.line}</span>
                <span class="figs">contest <span class="mono">{pct(o.contest)}</span> · loss if gone <span class="mono">{signed(o.loss_pp)}pp</span> · counter cost <span class="mono">{signed(o.counter_pp)}pp</span> · priority <span class="mono">{signed(o.priority_pp)}</span></span></div>
            </div>
          {:else}<p class="none">No plan champions to schedule</p>{/each}
        </div>
        <p class="hint">Slots are chosen to minimise the expected loss: being banned or taken before our slot
          (× what the backup costs), the best open counter (× enemy picks left after it), minus the value of hiding a flex pick.</p>
      </div>

      <div class="col">
        <h3>Fill the other roles</h3>
        <Table dense sticky={false}>
          <thead><tr><th scope="col">Champion</th><th scope="col" class="num" title="Overall gain with the plan, pp">Gain</th>
            <th scope="col" class="num">Stat</th><th scope="col" class="num">Style</th><th scope="col" class="num">Team</th></tr></thead>
          <tbody>
            {#each Object.entries(data.picks.fill) as [role, rows]}
              <tr class="grouprow"><th colspan="5" scope="colgroup">{LABELS[role]}</th></tr>
              {#each rows as s}
                <tr>
                  <td><span class="who"><Champ champ={s.champ} size={20} /><strong>{s.champ.name}</strong></span></td>
                  <td class="num mono {cls(s.gain_pp)}">{signed(s.gain_pp)}</td>
                  <td class="num mono">{signed(s.stat_total_pp)}</td><td class="num mono">{signed(s.style_total_pp)}</td>
                  <td class="num mono">{signed(s.team_weighted_pp)}</td>
                </tr>
              {:else}<tr><td colspan="5" class="none">Nobody available in the pool</td></tr>{/each}
            {:else}<tr><td colspan="5" class="none">Every role is in the plan</td></tr>{/each}
          </tbody>
        </Table>
      </div>

      <div class="col">
        <h3>Plan risks</h3>
        <Table dense sticky={false}>
          <thead><tr><th scope="col">Enemy champions that hurt the plan most</th><th scope="col" class="num">Change</th></tr></thead>
          <tbody>
            {#each data.picks.risks.champions as r}
              <tr><td><span class="who"><Champ champ={r.champ} size={20} /><strong>{r.champ.name}</strong><span class="why">{r.rule ?? ''}</span></span></td>
                <td class="num mono down">{signed(r.delta_pp)}</td></tr>
            {:else}<tr><td colspan="2" class="none">Nothing stands out</td></tr>{/each}
          </tbody>
          <thead><tr><th scope="col">Enemy comps</th><th scope="col" class="num">Change</th></tr></thead>
          <tbody>
            {#each data.picks.risks.identities as r}
              <tr><td><span class="who"><strong>{r.name}</strong><span class="icons">{#each r.champs as c}<Champ champ={c} size={20} />{/each}</span></span></td>
                <td class="num mono down">{signed(r.delta_pp)}</td></tr>
            {:else}<tr><td colspan="2" class="none">No comp identity hurts this plan</td></tr>{/each}
          </tbody>
          <thead><tr><th scope="col">Backups</th><th scope="col" class="num" title="Overall change with the backup, pp">Change</th></tr></thead>
          <tbody>
            {#each Object.entries(data.picks.backups) as [role, b]}
              <tr><td><span class="who"><span class="role mono">{LABELS[role]}</span>
                {#if b}<Champ champ={b.champ} size={20} /><strong>{b.champ.name}</strong>{:else}<span class="why">no one left in the pool</span>{/if}</span></td>
                <td class="num mono {b ? cls(b.change_pp) : ''}">{b ? signed(b.change_pp) : '—'}</td></tr>
            {:else}<tr><td colspan="2" class="none">No plan champions to back up</td></tr>{/each}
          </tbody>
        </Table>
      </div>
    </div>
  {:else if !error}
    <p class="note">Loading…</p>
  {/if}
</section>

<style>
  .half { margin-top: 32px; padding-top: 20px; border-top: 1px solid var(--hairline); }
  .head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  h2 { font-size: 16px; font-weight: 650; letter-spacing: -0.01em; margin: 0 16px 0 0; }
  .head .label-caps:not(:first-of-type) { margin-left: 12px; }
  h3 { font-size: 14px; font-weight: 650; margin: 0 0 2px; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 6px; }
  .note { font-size: 13px; color: var(--text-3); margin: 0; }
  .note.lead { margin-bottom: 12px; }
  .hint { font-size: 12px; color: var(--text-3); margin: 0 0 8px; min-height: 32px; }
  .picks .hint { min-height: 0; margin-top: 8px; }
  .error { color: var(--error); font-size: 13px; margin: 0 0 12px; }

  .three { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px 32px; }
  .three.picks { margin-top: 28px; }
  @media (max-width: 1180px) { .three { grid-template-columns: minmax(0, 1fr); } .hint { min-height: 0; } }
  .col { min-width: 0; }

  .who { display: flex; align-items: center; gap: 8px; min-width: 0; }
  td .who { padding: 4px 0; }
  .who strong { font-weight: 600; white-space: nowrap; }
  .txt { display: flex; flex-direction: column; min-width: 0; font-size: 12px; line-height: 16px; color: var(--text-2); }
  .txt strong { font-size: 14px; color: var(--text-1); }
  .warn { color: var(--warn); }
  .why { font-size: 12px; color: var(--text-3); white-space: normal; }
  .icons { display: inline-flex; gap: 2px; }
  .role { width: 56px; font-size: 11px; text-transform: uppercase; color: var(--text-3); }
  .none { color: var(--text-3); }
  p.none { margin: 0; padding: 8px 12px; font-size: 13px; }
  .half :global(tr.grouprow th) { height: 24px; padding-top: 6px; box-shadow: none; background: none; color: var(--text-2); }
  .half :global(tbody + thead th) { padding-top: 12px; height: 40px; vertical-align: bottom; padding-bottom: 6px; }

  .orderlist { border-top: 1px solid var(--hairline); margin-top: 6px; }
  .orderlist.blue { box-shadow: inset 3px 0 0 var(--side-blue); }
  .orderlist.red { box-shadow: inset 3px 0 0 var(--side-red); }
  .order { display: flex; align-items: center; gap: 10px; padding: 6px 0 6px 12px; border-bottom: 1px solid var(--hairline); }
  .slot { flex: none; width: 24px; font-size: 12.5px; font-weight: 600; color: var(--text-1); }
  .line { font-size: 13px; color: var(--text-1); }
  .figs { color: var(--text-3); }
  .figs .mono { color: var(--text-2); font-size: 11.5px; }
</style>
