<script>
  let slider = $state(0.6);
  let weights = $state({ threat: 1.0, matchup: 1.5, comp: 1.0, own: 1.0 });
  let cap = $state(10);
  let declared = $state(false);
  let alpha = $state(1.0);
  let beta = $state(1.0);
  let damageBalance = $state(true);
  let damagePenalty = $state(-0.05);
  let banSynergy = $state(false);
  let compCoherence = $state(true);
  let compWeight = $state(0.15);
  let laneWeight = $state(0.5);
  let roleFitted = $state({});
  let roleOverride = $state({}); // role -> number, blank = use the fitted weight
  const BLIND_LABELS = { comfort: 'Comfort', matchup: 'Vs likely opponents', synergy: 'With our pools', worst: 'Worst counter',
                         behind: 'Win rate 300+ gold behind', kit: 'Range and escape' };
  let blindWeights = $state({ comfort: 1, matchup: 1, synergy: 1, worst: 1, behind: 1, kit: 1 });
  let gateTau = $state(0.75);
  let executionComfort = $state(false);
  // engine/model.py comfort_gate: the share of a counter pick's edge a player keeps
  const gateShare = (comfort, games) => Math.round(100 * (1 - Math.exp(-(Math.max(comfort + 0.3, 0) + games / 20) / gateTau)));
  let message = $state('');
  let syn = $state({ w_stat: 0.5, w_style: 0.25, w_team: 0.25, conf_k: 200, prior_sd_pp: 1, ban_teamfight_d: 1,
                     plan_ban_bonus: 0.3, plan_pick_bonus: 0.3 });
  let synMessage = $state('');

  fetch('/api/settings').then((r) => r.json()).then((s) => {
    weights = { ...s.ban_weights };
    slider = s.slider ?? 0.6;
    declared = s.ban_declared_intents;
    alpha = s.alpha;
    beta = s.beta;
    cap = s.comfort_games_cap ?? 10;
    damageBalance = s.damage_balance;
    damagePenalty = s.damage_penalty;
    banSynergy = s.ban_comp_synergy;
    compCoherence = s.comp_coherence;
    compWeight = s.comp_weight;
    laneWeight = s.lane_weight;
    roleFitted = s.role_weights_fitted;
    roleOverride = { ...s.role_weight_override };
    blindWeights = { ...blindWeights, ...s.blind_weights };
    gateTau = s.gate_tau;
    executionComfort = s.execution_comfort;
    if (s.synergy_v2) syn = { ...s.synergy_v2 };
  });

  async function saveSynergy() {
    synMessage = '';
    const res = await fetch('/api/settings/synergy', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: syn }),
    });
    const body = await res.json();
    synMessage = res.ok ? 'Saved. Prep uses the new weights at once; Live from the next draft.' : `Not saved: ${body.detail}`;
    if (res.ok) syn = { ...body.synergy_v2 };
  }

  function fromSlider() {
    weights.threat = +(2.5 * (1 - slider)).toFixed(3);
    weights.matchup = +(2.5 * slider).toFixed(3);
  }

  async function save() {
    message = '';
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ban_weights: weights, ban_declared_intents: declared, alpha, beta, comfort_games_cap: cap,
                             damage_balance: damageBalance, damage_penalty: damagePenalty, ban_comp_synergy: banSynergy,
                             comp_coherence: compCoherence, comp_weight: compWeight, lane_weight: laneWeight,
                             role_weight_override: Object.fromEntries(Object.entries(roleOverride)
                               .filter(([, v]) => v !== '' && v != null).map(([r, v]) => [r, +v])),
                             blind_weights: blindWeights, gate_tau: gateTau, execution_comfort: executionComfort }),
    });
    const body = await res.json();
    if (res.ok) {
      message = 'Saved. Suggestions use the new weights from the next update.';
      if (body.slider != null) slider = body.slider;
    } else {
      message = `Not saved: ${body.detail}`;
    }
  }
</script>

<main class="page">
  <h1>Settings</h1>

  <section>
    <h2>Ban suggestions</h2>
    <label class="slider">
      <span>Meta threat</span>
      <input type="range" min="0" max="1" step="0.05" bind:value={slider} oninput={fromSlider} />
      <span>Our matchups</span>
    </label>
    <p class="muted">
      Meta threat {weights.threat.toFixed(2)} · Our matchups {weights.matchup.toFixed(2)} (the two always add up to 2.5)
    </p>

    <label class="row">
      Self-ban penalty
      <input type="number" min="0" max="10" step="0.1" bind:value={weights.own} />
    </label>
    <p class="muted">
      Banning one of our own players' best picks costs us how much better it is than their next option; this
      weight scales that cost (0 = ignore it, 1 = count it fully). Such bans are flagged "our … best pick".
    </p>

    <details>
      <summary>Advanced</summary>
      <div class="advanced">
        <label>Threat weight (a) <input type="number" min="0" max="10" step="0.1" bind:value={weights.threat} /></label>
        <label>Matchup weight (b) <input type="number" min="0" max="10" step="0.1" bind:value={weights.matchup} /></label>
        <label>Comp weight (c) <input type="number" min="0" max="10" step="0.1" bind:value={weights.comp} /></label>
      </div>
      <p class="muted">ban value = relevance × enemy-ban discount × (a × Threat + b × Matchup + c × Comp) − self-ban penalty × our loss</p>
    </details>

    <h2 class="sub">Pick suggestions</h2>
    <div class="advanced">
      <label>Meta weight (α) <input type="number" min="0" max="5" step="0.1" bind:value={alpha} /></label>
      <label>Comfort weight (β) <input type="number" min="0" max="5" step="0.1" bind:value={beta} /></label>
      <label>Comfort games cap <input type="number" min="1" max="200" step="1" bind:value={cap} /></label>
    </div>
    <p class="muted">
      Each pick scores α × the champion's strength in that player's bracket + β × the player's comfort on it.
      Comfort grows with recent games on the champion, up to the cap: games beyond it add nothing more
      (at {cap} games the games part tops out at {(0.4 * Math.log(1 + cap)).toFixed(2)} log-odds, × β = {(beta * 0.4 * Math.log(1 + cap)).toFixed(2)}).
      Tiers still count every game.
    </p>
    <label class="check">
      <input type="checkbox" bind:checked={executionComfort} />
      Lane hint on the Pools page: flag a champion we win on but are behind expectation in lane with, or lose on but are ahead with
    </label>
    <p class="muted">
      From each logged game's lane gold against expectation (History's strip), shrunk towards 0 by games, so one game can't
      show a hint. It's a hint only: tiers and comfort don't change.
    </p>

    <p class="muted">
      Synergy and damage balance are scored by the synergy model (Stat + Style + Team, weights below): the team model's
      damage rule replaces the old "more than 85% one damage type" penalty.
    </p>

    <label class="check">
      <input type="checkbox" bind:checked={compCoherence} />
      Comp style: pull picks towards our identity (the Team page's, else the one our comp is heading for)
    </label>
    {#if compCoherence}
      <label class="row">Strength (log-odds)
        <input type="number" min="0" max="1" step="0.05" bind:value={compWeight} /></label>
      <p class="muted">
        Off at our first pick, a tenth at the second, a quarter at the third, full at picks 4–5, so it can't take over the early
        calls. At full strength a perfect fit adds about {(25 * compWeight * 2 / 3).toFixed(1)}pp to a pick's score.
        It ranks picks but isn't counted in the win %.
      </p>
    {/if}

    <label class="row">Lane vs game matchup (0 = game only, 1 = lane only)
      <input type="number" min="0" max="1" step="0.1" bind:value={laneWeight} /></label>
    <p class="muted">Used for ban "best answers", counter risk and History's lane bars. The win % always uses the game matchup.</p>
    <div class="row roles">How much each role's matchups count
      {#each Object.keys(roleFitted) as r}
        <label>{r} <input type="number" min="0" max="3" step="0.1" placeholder={roleFitted[r] ?? '1'} bind:value={roleOverride[r]} /></label>
      {/each}
    </div>
    <p class="muted">The greyed number is fitted from crawled games; these average 1. Type a number to override it, or clear it to go back.</p>

    <div class="row roles">Blind pick safety weights
      {#each Object.keys(BLIND_LABELS) as k}
        <label>{BLIND_LABELS[k]} <input type="number" min="0" max="5" step="0.1" bind:value={blindWeights[k]} /></label>
      {/each}
    </div>
    <label class="row">Comfort curve (lower = a counter pick pays off sooner)
      <input type="number" min="0.05" max="5" step="0.05" bind:value={gateTau} /></label>
    <p class="muted">Counter picks keep a share of their edge that grows with comfort and games, quickly at first and then levelling off:
      a new learning champion keeps {gateShare(-0.24, 0)}%, a new comfortable one {gateShare(0, 0)}%, 5 games {gateShare(0.6, 5)}%, 20 games {gateShare(1, 20)}%.
      A much less comfortable champion also can't pass your most comfortable one on an edge of 3pp or less. Early picks lean on blind safety.</p>

    <label class="check">
      <input type="checkbox" bind:checked={banSynergy} />
      Ban phase 2: Comp also counts how much a ban target would complete strong synergy with the enemy's picks
    </label>

    <label class="check">
      <input type="checkbox" bind:checked={declared} />
      Flex / Solo: allow suggesting a ban on a champion a teammate has declared as their pick
    </label>

    <button onclick={save}>Save</button>
    {#if message}<p class="msg">{message}</p>{/if}
  </section>

  <section class="gap">
    <h2>Synergy (Live and Prep)</h2>
    <div class="advanced">
      <label>Stat weight <input type="number" min="0" max="5" step="0.05" bind:value={syn.w_stat} /></label>
      <label>Style weight <input type="number" min="0" max="5" step="0.05" bind:value={syn.w_style} /></label>
      <label>Team weight <input type="number" min="0" max="5" step="0.05" bind:value={syn.w_team} /></label>
      <label>Confidence games (k) <input type="number" min="1" max="5000" step="10" bind:value={syn.conf_k} /></label>
    </div>
    <p class="muted">
      Overall = Stat × win-rate synergy of every pair + Style × the core pairs' style rules + Team × the whole-comp
      model. A core pair with few games together moves its Stat weight to Style: confidence = games / (games + k).
    </p>
    <div class="advanced">
      <label>Rule prior SD (pp) <input type="number" min="0.1" max="10" step="0.1" bind:value={syn.prior_sd_pp} /></label>
      <label>Win teamfights in Combined bans (d) <input type="number" min="0" max="10" step="0.1" bind:value={syn.ban_teamfight_d} /></label>
    </div>
    <p class="muted">
      Prior SD: how far crawled data may move a written rule (smaller = trust the rules more); changing it refits
      every rule.
    </p>
    <div class="advanced">
      <label>Pinned plan: ban bonus <input type="number" min="0" max="2" step="0.05" bind:value={syn.plan_ban_bonus} /></label>
      <label>Pinned plan: pick bonus <input type="number" min="0" max="2" step="0.05" bind:value={syn.plan_pick_bonus} /></label>
    </div>
    <p class="muted">
      Log-odds added on the Live screen to a pinned plan's bans (our ban turns) and to plan champions due at our
      current pick (0.3 ≈ 7.5pp). Applied when a plan is pinned; plan suggestions still pass every guardrail.
    </p>
    <button onclick={saveSynergy}>Save synergy settings</button>
    {#if synMessage}<p class="msg">{synMessage}</p>{/if}
  </section>
</main>

<style>
  .page { max-width: 800px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 32px; margin: 8px 0 16px; }
  h2 { font-size: 20px; margin: 0 0 12px; }
  h2.sub { font-size: 17px; margin: 18px 0 6px; }
  .row { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
  .row input { width: 80px; background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
               border-radius: 6px; padding: 4px 6px; font: inherit; }
  section { background: var(--panel); border-radius: 8px; padding: 18px 20px; }
  section.gap { margin-top: 18px; }
  .slider { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center; font-size: 16px; }
  input[type='range'] { width: 100%; }
  .advanced { display: flex; gap: 16px; flex-wrap: wrap; margin: 10px 0; }
  .advanced input { width: 80px; background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
                    border-radius: 6px; padding: 4px 6px; font: inherit; }
  details { margin: 12px 0; }
  summary { cursor: pointer; color: var(--muted); }
  .check { display: flex; gap: 8px; align-items: center; margin: 14px 0; }
  button { background: var(--blue); color: white; border: 0; border-radius: 6px; padding: 8px 16px; font: inherit; cursor: pointer; }
  .muted { color: var(--muted); font-size: 14px; }
  .msg { color: var(--good); }
</style>
