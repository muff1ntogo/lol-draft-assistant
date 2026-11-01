<script>
  let slider = $state(0.6);
  let weights = $state({ threat: 1.0, matchup: 1.5, comp: 1.0, own: 1.0 });
  let cap = $state(10);
  let declared = $state(false);
  let alpha = $state(1.0);
  let beta = $state(1.0);
  let message = $state('');

  fetch('/api/settings').then((r) => r.json()).then((s) => {
    weights = { ...s.ban_weights };
    slider = s.slider ?? 0.6;
    declared = s.ban_declared_intents;
    alpha = s.alpha;
    beta = s.beta;
    cap = s.comfort_games_cap ?? 10;
  });

  function fromSlider() {
    weights.threat = +(2.5 * (1 - slider)).toFixed(3);
    weights.matchup = +(2.5 * slider).toFixed(3);
  }

  async function save() {
    message = '';
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ban_weights: weights, ban_declared_intents: declared, alpha, beta, comfort_games_cap: cap }),
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
      <input type="checkbox" bind:checked={declared} />
      Flex / Solo: allow suggesting a ban on a champion a teammate has declared as their pick
    </label>

    <button onclick={save}>Save</button>
    {#if message}<p class="msg">{message}</p>{/if}
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
