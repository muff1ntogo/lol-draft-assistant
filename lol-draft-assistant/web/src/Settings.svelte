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
  let message = $state('');
  let syn = $state({ w_stat: 0.5, w_style: 0.25, w_team: 0.25, conf_k: 200, prior_sd_pp: 1, ban_teamfight_d: 1 });
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
    if (s.synergy_v2) syn = { ...s.synergy_v2 };
  });

  async function saveSynergy() {
    synMessage = '';
    const res = await fetch('/api/settings/synergy', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: syn }),
    });
    const body = await res.json();
    synMessage = res.ok ? 'Saved. The Prep page uses the new weights at once.' : `Not saved: ${body.detail}`;
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
                             comp_coherence: compCoherence, comp_weight: compWeight }),
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
      <input type="checkbox" bind:checked={damageBalance} />
      Damage balance: penalise a comp that is more than 85% one damage type
    </label>
    {#if damageBalance}
      <label class="row">Penalty (log-odds)
        <input type="number" min="-1" max="0" step="0.01" bind:value={damagePenalty} /></label>
      <p class="muted">Applies once three or more of our picks have damage data; −0.05 is about −1.25pp win chance.</p>
    {/if}

    <label class="check">
      <input type="checkbox" bind:checked={compCoherence} />
      Comp style: pull picks towards our style (the Team page's, else the one our comp is heading for)
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
    <h2>Synergy (Prep page)</h2>
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
