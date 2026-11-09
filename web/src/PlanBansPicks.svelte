<script>
  // Prep page, bottom half (docs/synergy-v2.md, Stage 5): bans and picks for the current plan (the locks above),
  // for our side and the draft format. Ban lists: Win lane (each plan champion is its role's only answer),
  // Win teamfights (enemy champions that hurt our plan's team model), Combined (ban value + teamfights).
  import Champ from './Champ.svelte';

  let { locks = {} } = $props();

  const LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
  const saved = (() => { try { return JSON.parse(localStorage.getItem('prep-bans') || '{}'); } catch { return {}; } })();
  let side = $state(saved.side ?? 'blue');
  let format = $state(saved.format ?? 'tournament');
  let data = $state(null);
  let error = $state(null);
  let loading = $state(false);
  let pinned = $state(null);
  let pinMsg = $state('');
  fetch('/api/plan/pin').then((r) => r.json()).then((d) => (pinned = d.pinned));
  const sameAsPinned = $derived(pinned && JSON.stringify(Object.entries(pinned.locks).sort())
                                         === JSON.stringify(Object.entries(locks).map(([r, c]) => [r, c]).sort()));
  async function pin() {
    pinMsg = '';
    const r = await fetch('/api/plan/pin', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                              body: JSON.stringify({ locks }) });
    const body = await r.json();
    if (!r.ok) { pinMsg = typeof body.detail === 'string' ? body.detail : 'Could not pin'; return; }
    pinned = body.pinned;
    pinMsg = 'Pinned: the Live screen follows this plan in the next champ select.';
  }
  async function unpin() {
    await fetch('/api/plan/pin', { method: 'DELETE' });
    pinned = null;
    pinMsg = '';
  }

  $effect(() => {
    try { localStorage.setItem('prep-bans', JSON.stringify({ side, format })); } catch { /* not remembered */ }
  });

  let timer;
  $effect(() => {
    const body = JSON.stringify({ locks, side, format });
    clearTimeout(timer);
    timer = setTimeout(() => load(body), 150);
  });

  async function load(body) {
    loading = true;
    const r = await fetch('/api/prep/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    loading = false;
    if (!r.ok) { error = (await r.json()).detail; return; }
    error = null;
    data = await r.json();
  }

  const signed = (x, d = 1) => (x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(d)}`);
  const cls = (v) => (v > 0.5 ? 'pos' : v < -0.5 ? 'neg' : '');
  const pct = (x) => `${Math.round(x * 100)}%`;
  const LISTS = [
    ['lane', 'Win lane', 'Bans that protect each plan champion\'s lane (roles without a plan use the pool).'],
    ['teamfight', 'Win teamfights', 'Enemy champions that most lower our plan\'s team model, × how likely they pick them.'],
    ['combined', 'Combined', 'Ban value (meta threat, our matchups, counters to the plan) plus Win teamfights.'],
  ];
</script>

<section class="half">
  <div class="head">
    <h2>Plan: bans and picks</h2>
    <div class="group" role="group" aria-label="Side">
      <button class="blue" class:on={side === 'blue'} onclick={() => (side = 'blue')}>Blue</button>
      <button class="red" class:on={side === 'red'} onclick={() => (side = 'red')}>Red</button>
    </div>
    <div class="group" role="group" aria-label="Format">
      <button class:on={format === 'tournament'} onclick={() => (format = 'tournament')}>Ranked 5s (tournament)</button>
      <button class:on={format === 'flex'} onclick={() => (format = 'flex')}>Flex</button>
    </div>
    {#if loading}<span class="muted small">updating…</span>{/if}
    <span class="pin">
      {#if pinned && sameAsPinned}<span class="pinned">📌 Pinned for next game</span>
      {:else}<button class="pinbtn" disabled={!Object.keys(locks).length} onclick={pin}>
        📌 {pinned ? 'Pin this plan instead' : 'Pin for next game'}</button>{/if}
      {#if pinned}<button onclick={unpin}>Unpin</button>{/if}
    </span>
  </div>
  {#if pinMsg}<p class="muted small">{pinMsg}</p>{/if}
  {#if error}<p class="error">{typeof error === 'string' ? error : JSON.stringify(error)}</p>{/if}
  {#if data}
    {#if data.empty}<p class="muted">Lock champions above to build a plan. Until then the lists use each player's whole pool.</p>{/if}
    <div class="bans">
      {#each LISTS as [k, title, hint]}
        <div class="col">
          <h3>{title}</h3>
          <p class="muted small">{hint}</p>
          <ol>
            {#each data.bans[k] as b}
              <li>
                <Champ champ={b.champ} size={34} />
                <div class="txt">
                  <strong>{b.champ.name}{#if b.owner}<span class="owner">{LABELS[b.owner]}</span>{/if}</strong>
                  <span>{b.reason}</span>
                  {#each b.warnings as w}<span class="warn">{w}</span>{/each}
                </div>
                <span class="val" title="Ban value, pp of win chance">{signed(b.value_pp, 2)}</span>
              </li>
            {:else}<li class="muted small">No useful ban.</li>{/each}
          </ol>
        </div>
      {/each}
    </div>

    <div class="picks">
      <div class="col">
        <h3>Pick order <small>{side} side · {format === 'flex' ? 'Flex' : 'tournament'}</small></h3>
        {#each data.picks.order as o}
          <div class="order">
            <span class="slot {side}">{o.slot}</span>
            <Champ champ={o.champ} size={30} />
            <div class="txt"><span>{o.line}</span>
              <span class="muted small">contest {pct(o.contest)} · loss if gone {signed(o.loss_pp)}pp · counter cost {signed(o.counter_pp)}pp · priority {signed(o.priority_pp)}</span></div>
          </div>
        {:else}<p class="muted small">No plan champions to schedule.</p>{/each}
        <p class="muted small">Slots are chosen to minimise the expected loss: being banned or taken before our slot
          (× what the backup costs), the best open counter (× enemy picks left after it), minus the value of hiding a flex pick.</p>
      </div>

      <div class="col">
        <h3>Fill the other roles</h3>
        {#each Object.entries(data.picks.fill) as [role, rows]}
          <h4>{LABELS[role]}</h4>
          {#each rows as s}
            <div class="fill">
              <Champ champ={s.champ} size={26} />
              <span class="name">{s.champ.name}</span>
              <span class="num {cls(s.gain_pp)}" title="Overall gain with the plan">{signed(s.gain_pp)}</span>
              <span class="muted small">Stat {signed(s.stat_total_pp)} · Style {signed(s.style_total_pp)} · Team {signed(s.team_weighted_pp)}</span>
            </div>
          {:else}<p class="muted small">Nobody available in the pool.</p>{/each}
        {:else}<p class="muted small">Every role is in the plan.</p>{/each}
      </div>

      <div class="col">
        <h3>Plan risks</h3>
        <h4>Enemy champions that hurt the plan most</h4>
        {#each data.picks.risks.champions as r}
          <div class="fill"><Champ champ={r.champ} size={26} /><span class="name">{r.champ.name}</span>
            <span class="num neg">{signed(r.delta_pp)}</span><span class="muted small">{r.rule ?? ''}</span></div>
        {:else}<p class="muted small">Nothing stands out.</p>{/each}
        <h4>Enemy comps</h4>
        {#each data.picks.risks.identities as r}
          <div class="fill"><span class="name">{r.name}</span><span class="num neg">{signed(r.delta_pp)}</span>
            <span class="icons">{#each r.champs as c}<Champ champ={c} size={20} />{/each}</span></div>
        {:else}<p class="muted small">No comp identity hurts this plan.</p>{/each}
        <h4>Backups</h4>
        {#each Object.entries(data.picks.backups) as [role, b]}
          <div class="fill"><span class="role">{LABELS[role]}</span>
            {#if b}<Champ champ={b.champ} size={22} /><span class="name">{b.champ.name}</span>
              <span class="num {cls(b.change_pp)}" title="Overall change with the backup">{signed(b.change_pp)}</span>
            {:else}<span class="muted small">no one left in the pool</span>{/if}</div>
        {/each}
      </div>
    </div>
  {:else if !error}
    <p class="muted">Loading…</p>
  {/if}
</section>

<style>
  .half { margin-top: 16px; background: var(--panel); border-radius: 8px; padding: 12px 14px; }
  .head { display: flex; align-items: center; gap: 16px; margin-bottom: 6px; }
  h2 { font-size: 19px; margin: 0; }
  h3 { font-size: 16px; margin: 0 0 2px; }
  h3 small { color: var(--muted); font-weight: 400; font-size: 13px; }
  h4 { font-size: 13px; margin: 10px 0 4px; color: var(--text-2); }
  .group { display: inline-flex; gap: 4px; }
  .pin { margin-left: auto; display: inline-flex; gap: 6px; align-items: center; }
  .pinbtn { background: var(--gold); border-color: var(--gold); color: var(--on-gold); font-weight: 600; }
  .pinbtn:disabled { opacity: 0.4; cursor: not-allowed; }
  .pinned { color: var(--link); font-size: 13px; }
  button { font: inherit; font-size: 13px; color: var(--text); background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px; cursor: pointer; }
  button.on { background: var(--gold-bg); border-color: var(--gold); color: var(--gold); }
  button.blue.on { background: var(--side-blue-bg); border-color: var(--side-blue); color: var(--link); }
  button.red.on { background: var(--side-red-bg); border-color: var(--side-red); color: var(--side-red-text); }
  .bans, .picks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 8px; }
  .picks { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 12px; }
  .col { min-width: 0; }
  ol { margin: 6px 0 0; padding-left: 18px; }
  li { display: flex; align-items: center; gap: 8px; margin: 5px 0; }
  .txt { display: flex; flex-direction: column; min-width: 0; flex: 1; font-size: 13px; }
  .txt span { color: var(--text-2); }
  .val, .num { font-variant-numeric: tabular-nums; font-size: 13px; }
  .owner { font-size: 11px; font-weight: 400; color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; margin-left: 6px; }
  .warn { color: var(--warn); font-size: 12px; }
  .order { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
  .slot { flex: none; width: 30px; text-align: center; font-weight: 700; font-size: 13px; border-radius: 4px; padding: 2px 0; }
  .slot.blue { background: var(--side-blue-bg); color: var(--link); }
  .slot.red { background: var(--side-red-bg); color: var(--side-red-text); }
  .fill { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 13px; }
  .fill .name { min-width: 92px; }
  .fill .role { min-width: 56px; color: var(--muted); }
  .icons { display: inline-flex; gap: 2px; }
  .pos { color: var(--up-text); }
  .neg { color: var(--down-text); }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
  .error { color: var(--red); }
</style>
