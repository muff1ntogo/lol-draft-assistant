<script>
  // Settings (docs/ui-redesign.md, step 7): a sub-navigation on the left, one section at a time. Weights, Bans and
  // Scouting are one saved set (Save in the header); Synergy saves on its own.
  import { pulse, reveal } from './motion.js';
  import PageHeader from './ui/PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Field from './ui/Field.svelte';

  const SECTIONS = [['weights', 'Weights'], ['bans', 'Bans'], ['synergy', 'Synergy'], ['scouting', 'Scouting'], ['data', 'Data'],
                    ['key', 'Riot key'], ['account', 'Account'], ['champions', 'Champion data'], ['about', 'About']];
  const SAVED_TOGETHER = ['weights', 'bans', 'scouting'];
  let section = $state('weights');
  try { const kept = localStorage.getItem('settings.section'); if (SECTIONS.some(([k]) => k === kept)) section = kept; } catch {}
  function go(key) {
    section = key;
    try { localStorage.setItem('settings.section', key); } catch {}
  }
  function navKey(e) {
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const at = SECTIONS.findIndex(([k]) => k === section);
    go(SECTIONS[(at + step + SECTIONS.length) % SECTIONS.length][0]);
    queueMicrotask(() => document.querySelector('.subnav [aria-current="page"]')?.focus());
  }
  const failed = (m) => /^(Not|No) |fail|error|couldn/i.test(m);
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
  let scoutEnabled = $state(true);
  // App: the Riot key and updates (first_run.py, updates.py)
  let riotKey = $state('');
  let keyMessage = $state('');
  let appSetup = $state(null);
  let appUpdate = $state(null);
  (async () => {
    appSetup = await (await fetch('/api/setup')).json();
    appUpdate = await (await fetch('/api/update')).json();
  })();
  // Account (docs/shared-data.md, Stage 3): join with an invite, the admin's own tokens, sign out. Tokens are sent once and
  // never come back from the server.
  let account = $state(null);
  let invite = $state('');
  let showAdmin = $state(false);
  let admin = $state({ url: '', org: '', database: '', name: '', db_token: '', platform_token: '' });
  let accountMessage = $state('');
  (async () => { account = await (await fetch('/api/account')).json(); })();
  async function accountCall(url, body) {
    accountMessage = '';
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
    const out = await res.json();
    if (res.ok) { account = out; return true; }
    accountMessage = out.detail;
    return false;
  }
  async function join() { if (await accountCall('/api/account/join', { invite })) invite = ''; }
  async function saveAdmin() {
    if (await accountCall('/api/account/admin', admin)) { admin = { url: '', org: '', database: '', name: '', db_token: '', platform_token: '' }; showAdmin = false; }
  }
  const signOut = () => accountCall('/api/account/sign-out');
  async function saveKey() {
    const res = await fetch('/api/setup/riot-key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: riotKey }),
    });
    const body = await res.json();
    if (res.ok) { appSetup = body; riotKey = ''; keyMessage = 'Key saved: used from now on.'; }
    else keyMessage = `Not saved: ${body.detail}`;
  }
  let checking = $state(false);
  let installing = $state(false);
  let updateMessage = $state('');
  const tellBanner = (status) => window.dispatchEvent(new CustomEvent('draftassist:update', { detail: status }));

  function describe(s) {
    if (s.error) return s.error;
    if (s.available) return `Draft Assistant ${s.latest} is available (you have ${s.current}).`;
    if (s.latest) return `You have the latest version (${s.current}). Checked just now.`;
    return 'No release found on GitHub yet.';
  }

  async function checkUpdate() {
    checking = true;
    updateMessage = '';
    try {
      const res = await fetch('/api/update/check', { method: 'POST' });
      appUpdate = await res.json();
      updateMessage = res.ok ? describe(appUpdate) : `Couldn't check: ${appUpdate.detail ?? res.status}`;
      if (res.ok) tellBanner(appUpdate);
    } catch {
      updateMessage = "Couldn't reach the app's server.";
    } finally {
      checking = false;
    }
  }

  async function installUpdate() {
    installing = true;
    const res = await fetch('/api/update/install', { method: 'POST' });
    if (res.ok) {
      updateMessage = 'Installing: the app closes and opens again in a moment.';
    } else {
      updateMessage = (await res.json()).detail;
      installing = false;
    }
  }
  let scoutWeight = $state(0.6);
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
    // every role gets an entry ('' = use the fitted weight), so each field has something to bind to
    roleOverride = Object.fromEntries(Object.keys(s.role_weights_fitted).map((r) => [r, s.role_weight_override[r] ?? '']));
    blindWeights = { ...blindWeights, ...s.blind_weights };
    gateTau = s.gate_tau;
    executionComfort = s.execution_comfort;
    scoutEnabled = s.scout_enabled;
    scoutWeight = s.scout_weight;
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
                             blind_weights: blindWeights, gate_tau: gateTau, execution_comfort: executionComfort,
                             scout_enabled: scoutEnabled, scout_weight: +scoutWeight }),
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

<main class="console">
  <PageHeader title="Settings" description="How picks and bans are weighed, scouting, the Riot key, the team account and the app itself.">
    {#snippet actions()}
      {#if SAVED_TOGETHER.includes(section)}
        <Button variant="primary" size="md" onclick={save} title="Saves Weights, Bans and Scouting together">Save</Button>
      {:else if section === 'synergy'}
        <Button variant="primary" size="md" onclick={saveSynergy}>Save synergy settings</Button>
      {/if}
    {/snippet}
  </PageHeader>

  <div class="layout">
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <nav class="subnav" aria-label="Settings sections" onkeydown={navKey}>
      {#each SECTIONS as [key, label]}
        <button type="button" class:on={section === key} aria-current={section === key ? 'page' : undefined} tabindex={section === key ? 0 : -1}
                onclick={() => go(key)}>{label}</button>
      {/each}
    </nav>

    <div class="pane">
      {#if SAVED_TOGETHER.includes(section) && message}<p class="msg" class:bad={failed(message)} role="status" use:pulse={{ value: message, ok: !failed(message) }}>{failed(message) ? '⚠ ' : ''}{message}</p>{/if}

      {#if section === 'weights'}
        <h2>Pick suggestions</h2>
        <div class="fields">
          <Field label="Meta weight (α)" type="number" min="0" max="5" step="0.1" bind:value={alpha} />
          <Field label="Comfort weight (β)" type="number" min="0" max="5" step="0.1" bind:value={beta} />
          <Field label="Comfort games cap" type="number" min="1" max="200" step="1" bind:value={cap} />
        </div>
        <p class="help">Each pick scores α × the champion's strength in that player's bracket + β × the player's comfort on it.
          Comfort grows with recent games on the champion, up to the cap: games beyond it add nothing more
          (at <span class="mono">{cap}</span> games the games part tops out at <span class="mono">{(0.4 * Math.log(1 + cap)).toFixed(2)}</span> log-odds, × β = <span class="mono">{(beta * 0.4 * Math.log(1 + cap)).toFixed(2)}</span>).
          Tiers still count every game.</p>
        <p class="help">Synergy and damage balance are scored by the synergy model (Stat + Style + Team, under Synergy): the team model's
          damage rule replaces the old "more than 85% one damage type" penalty.</p>

        <h3>Comp style</h3>
        <Field type="checkbox" label="Pull picks towards our identity" hint="The Team page's style, else the one our comp is heading for" bind:checked={compCoherence} />
        {#if compCoherence}
          <div class="fields"><Field label="Strength (log-odds)" type="number" min="0" max="1" step="0.05" bind:value={compWeight} /></div>
          <p class="help">Off at our first pick, a tenth at the second, a quarter at the third, full at picks 4–5, so it can't take over the early
            calls. At full strength a perfect fit adds about <span class="mono">{(25 * compWeight * 2 / 3).toFixed(1)}pp</span> to a pick's score.
            It ranks picks but isn't counted in the win %.</p>
        {/if}

        <h3>Matchups</h3>
        <div class="fields"><Field label="Lane vs game matchup" type="number" min="0" max="1" step="0.1" bind:value={laneWeight} hint="0 = game only, 1 = lane only" /></div>
        <p class="help">Used for ban "best answers", counter risk and History's lane bars. The win % always uses the game matchup.</p>
        <p class="label-caps group">How much each role's matchups count</p>
        <div class="fields">
          {#each Object.keys(roleFitted) as r}
            <Field label={r === 'adc' ? 'ADC' : r[0].toUpperCase() + r.slice(1)} type="number" min="0" max="3" step="0.1" placeholder={String(roleFitted[r] ?? '1')} bind:value={roleOverride[r]} />
          {/each}
        </div>
        <p class="help">The greyed number is fitted from crawled games; these average 1. Type a number to override it, or clear it to go back.</p>

        <h3>Blind picks and counter picks</h3>
        <p class="label-caps group">Blind pick safety weights</p>
        <div class="fields">
          {#each Object.keys(BLIND_LABELS) as k}
            <Field label={BLIND_LABELS[k]} type="number" min="0" max="5" step="0.1" bind:value={blindWeights[k]} />
          {/each}
        </div>
        <div class="fields"><Field label="Comfort curve" type="number" min="0.05" max="5" step="0.05" bind:value={gateTau} hint="Lower = a counter pick pays off sooner" /></div>
        <p class="help">Counter picks keep a share of their edge that grows with comfort and games, quickly at first and then levelling off:
          a new learning champion keeps <span class="mono">{gateShare(-0.24, 0)}%</span>, a new comfortable one <span class="mono">{gateShare(0, 0)}%</span>,
          5 games <span class="mono">{gateShare(0.6, 5)}%</span>, 20 games <span class="mono">{gateShare(1, 20)}%</span>.
          A much less comfortable champion also can't pass your most comfortable one on an edge of 3pp or less. Early picks lean on blind safety.</p>

        <h3>Roster</h3>
        <Field type="checkbox" label="Lane hint on a player's pool" bind:checked={executionComfort}
               hint="Flags a champion we win on but are behind expectation in lane with, or lose on but are ahead with" />
        <p class="help">From each logged game's lane gold against expectation (History's strip), shrunk towards 0 by games, so one game can't
          show a hint. It's a hint only: tiers and comfort don't change.</p>

      {:else if section === 'bans'}
        <h2>Ban suggestions</h2>
        <div class="sliderrow">
          <span>Meta threat <span class="mono">{weights.threat.toFixed(2)}</span></span>
          <input type="range" min="0" max="1" step="0.05" bind:value={slider} oninput={fromSlider} aria-label="Balance between meta threat and our matchups" />
          <span>Our matchups <span class="mono">{weights.matchup.toFixed(2)}</span></span>
        </div>
        <p class="help">The two always add up to 2.5.</p>
        <div class="fields"><Field label="Self-ban penalty" type="number" min="0" max="10" step="0.1" bind:value={weights.own} /></div>
        <p class="help">Banning one of our own players' best picks costs us how much better it is than their next option; this
          weight scales that cost (0 = ignore it, 1 = count it fully). Such bans are flagged "our … best pick".</p>
        <Field type="checkbox" label="Ban phase 2: Comp counts synergy with the enemy's picks" bind:checked={banSynergy}
               hint="How much a ban target would complete strong synergy with what they already have" />
        <Field type="checkbox" label="Flex / Solo: allow banning a teammate's declared pick" bind:checked={declared} />
        <details>
          <summary>Advanced: the three ban weights</summary>
          <div class="fields">
            <Field label="Threat weight (a)" type="number" min="0" max="10" step="0.1" bind:value={weights.threat} />
            <Field label="Matchup weight (b)" type="number" min="0" max="10" step="0.1" bind:value={weights.matchup} />
            <Field label="Comp weight (c)" type="number" min="0" max="10" step="0.1" bind:value={weights.comp} />
          </div>
          <p class="help mono">ban value = relevance × enemy-ban discount × (a × Threat + b × Matchup + c × Comp) − self-ban penalty × our loss</p>
        </details>

      {:else if section === 'synergy'}
        <h2>Synergy <span class="sub">Live and Prep</span></h2>
        {#if synMessage}<p class="msg" class:bad={failed(synMessage)} role="status" use:pulse={{ value: synMessage, ok: !failed(synMessage) }}>{failed(synMessage) ? '⚠ ' : ''}{synMessage}</p>{/if}
        <div class="fields">
          <Field label="Stat weight" type="number" min="0" max="5" step="0.05" bind:value={syn.w_stat} />
          <Field label="Style weight" type="number" min="0" max="5" step="0.05" bind:value={syn.w_style} />
          <Field label="Team weight" type="number" min="0" max="5" step="0.05" bind:value={syn.w_team} />
          <Field label="Confidence games (k)" type="number" min="1" max="5000" step="10" bind:value={syn.conf_k} />
        </div>
        <p class="help">Overall = Stat × win-rate synergy of every pair + Style × the core pairs' style rules + Team × the whole-comp
          model. A core pair with few games together moves its Stat weight to Style: confidence = games / (games + k).</p>
        <div class="fields">
          <Field label="Rule prior SD (pp)" type="number" min="0.1" max="10" step="0.1" bind:value={syn.prior_sd_pp} />
          <Field label="Win teamfights in Combined bans (d)" type="number" min="0" max="10" step="0.1" bind:value={syn.ban_teamfight_d} />
        </div>
        <p class="help">Prior SD: how far crawled data may move a written rule (smaller = trust the rules more); changing it refits every rule.</p>
        <h3>Pinned plan</h3>
        <div class="fields">
          <Field label="Ban bonus" type="number" min="0" max="2" step="0.05" bind:value={syn.plan_ban_bonus} />
          <Field label="Pick bonus" type="number" min="0" max="2" step="0.05" bind:value={syn.plan_pick_bonus} />
        </div>
        <p class="help">Log-odds added on Live to a pinned plan's bans (our ban turns) and to plan champions due at our
          current pick (0.3 ≈ 7.5pp). Applied when a plan is pinned; plan suggestions still pass every guardrail.</p>

      {:else if section === 'scouting'}
        <h2>Scouting <span class="sub">Ranked 5s</span></h2>
        <Field type="checkbox" label="Scout the enemy players" bind:checked={scoutEnabled} hint="Their own champion pools in bans, and their likely picks" />
        {#if scoutEnabled}
          <div class="fields"><Field label="Weight of their pools" type="number" min="0" max="1" step="0.05" bind:value={scoutWeight} /></div>
          <p class="help">At full confidence their own games count <span class="mono">{Math.round(scoutWeight * 100)}%</span> and the bracket's meta the rest; with
            5 games in their role it's half that. Ban cards then show the best bans from their pools (5, then 3) and the
            strongest of the rest (2, then 1).</p>
        {/if}
        <p class="help">Scouting needs a Riot key and the enemy's Riot IDs: the client gives them in Ranked 5s, or paste them into Scout opponents on Live.</p>

      {:else if section === 'data'}
        <h2>Data</h2>
        <dl class="facts">
          <dt>Data folder</dt><dd class="mono">{appSetup?.data_folder ?? '—'}</dd>
          <dt>Stats</dt><dd>{account?.role === 'user' ? "Pulled from the team's shared database" : 'Crawled on this machine'}</dd>
        </dl>
        <p class="help">The databases, the Riot cache, the roster file and the tags and rules live in the data folder; updates never touch it.</p>
        <div class="row"><Button href="#/crawl">Open Crawl: jobs and storage</Button></div>

      {:else if section === 'key'}
        <h2>Riot API key</h2>
        <form class="row" onsubmit={(e) => { e.preventDefault(); saveKey(); }}>
          <Field label="Riot API key" type="password" autocomplete="off" bind:value={riotKey} width="320px" mono
                 placeholder={appSetup?.riot_key_set ? 'Set (paste a new one to replace it)' : 'RGAPI-…'} />
          <Button type="submit" disabled={!riotKey.trim()}>Save key</Button>
        </form>
        {#if keyMessage}<p class="msg" class:bad={failed(keyMessage)} role="status" use:pulse={{ value: keyMessage, ok: !failed(keyMessage) }}>{failed(keyMessage) ? '⚠ ' : ''}{keyMessage}</p>{/if}
        <p class="help">{appSetup?.riot_key_set ? 'A key is set.' : 'No key set yet.'} Stored in {appSetup?.data_folder ?? 'the data folder'}\.env, never shown again.
          Needed for match history, scouting and the crawl; get one at developer.riotgames.com.</p>

      {:else if section === 'account'}
        <h2>Account</h2>
        {#if account}
          <p class="line">{account.label}{#if account.url}<span class="quiet url">{account.url}</span>{/if}</p>
          {#if account.warning}<p class="msg bad" role="status">⚠ {account.warning}</p>{/if}
          {#if account.role === 'solo'}
            <form class="row" onsubmit={(e) => { e.preventDefault(); join(); }}>
              <Field label="Invite code" type="password" autocomplete="off" bind:value={invite} width="360px" mono placeholder="dav1.…" />
              <Button type="submit" disabled={!invite.trim()}>Join my team</Button>
            </form>
            <p class="help">A teammate pastes the code the team's admin sent. The app then pulls the shared stats instead of
              crawling; your own games, History and scouting stay on this machine. Without a code it works on its own, as before.</p>
            <div class="row"><Button variant="ghost" aria-expanded={showAdmin} onclick={() => (showAdmin = !showAdmin)}>{showAdmin ? 'Cancel' : 'I am the team admin'}</Button></div>
            {#if showAdmin}
              <form class="admin" transition:reveal onsubmit={(e) => { e.preventDefault(); saveAdmin(); }}>
                <Field label="Database URL" bind:value={admin.url} placeholder="libsql://team-stats-you.turso.io" mono />
                <Field label="Organisation" bind:value={admin.org} placeholder="Your Turso organisation (slug)" />
                <Field label="Database name" bind:value={admin.database} placeholder="team-stats" />
                <Field label="Your display name" bind:value={admin.name} />
                <Field label="Full-access database token" type="password" autocomplete="off" bind:value={admin.db_token} />
                <Field label="Platform API token" type="password" autocomplete="off" bind:value={admin.platform_token} />
                <div><Button type="submit">Save admin account</Button></div>
              </form>
              <p class="help">Both tokens go to Windows Credential Manager, never into the data folder's .env, and are never
                shown again. They stay on this machine: invites carry only a read-only token.</p>
            {/if}
          {:else}
            <div class="row"><Button onclick={signOut}>Sign out</Button></div>
            <p class="help">Removes the stored tokens; this machine's data stays. Then join again with a new code, or use it on its own.</p>
          {/if}
          {#if accountMessage}<p class="msg bad" role="alert">⚠ {accountMessage}</p>{/if}
        {:else}<p class="quiet">Loading…</p>{/if}

      {:else if section === 'champions'}
        <h2>Champion data</h2>
        <div class="row"><Button href="#/tags">Champion tags</Button><Button href="#/rules">Synergy rules</Button></div>
        <p class="help">Tags: each champion's fight identity, archetype, kit tags and timing per role, which feed badges, comp coherence,
          matchup smoothing and the style rules. Rules: the style and team rules, their written priors and what the crawl
          made of them.</p>

      {:else}
        <h2>About</h2>
        <dl class="facts">
          <dt>Version</dt><dd class="mono">{appUpdate?.current ?? 'source checkout'}</dd>
          {#if appSetup?.window?.build}
            <dt>Window</dt><dd>{appSetup.window.dark ? 'Dark title bar on' : appSetup.window.handle === false ? 'Not found to style' : appSetup.window.when ? 'Windows refused the dark title bar' : 'Not styled yet'}
              <span class="quiet">(Windows {appSetup.window.build >= 22000 ? '11' : '10'}, build {appSetup.window.build}{appSetup.window.colours ? '' : ': the title bar colour and rounded corners are Windows 11 only'})</span></dd>
          {/if}
        </dl>
        {#if appUpdate?.unsupported}<p class="help">{appUpdate.unsupported}</p>
        {:else}
          <div class="row">
            <Button onclick={checkUpdate} disabled={installing} busy={checking}>{checking ? 'Checking…' : 'Check for updates'}</Button>
            {#if appUpdate?.available}
              <Button onclick={installUpdate} busy={installing}>{installing ? 'Installing…' : `Update to ${appUpdate.latest}`}</Button>
            {/if}
          </div>
        {/if}
        {#if updateMessage}<p class="msg" class:bad={failed(updateMessage)} role="status">{updateMessage}</p>{/if}
      {/if}
    </div>
  </div>
</main>

<style>
  .layout { display: grid; grid-template-columns: 184px minmax(0, 720px); gap: 48px; align-items: start; }
  @media (max-width: 900px) { .layout { grid-template-columns: minmax(0, 1fr); gap: 16px; } .subnav { flex-direction: row !important; flex-wrap: wrap; position: static !important; } }
  .subnav { display: flex; flex-direction: column; gap: 2px; position: sticky; top: 16px; }
  .subnav button { height: 30px; padding: 0 10px; border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-3);
                   font-size: 13px; text-align: left; }
  .subnav button:hover { color: var(--text-1); background: var(--surface-1); filter: none; }
  /* the open section is a state, not a call to act: light ink on a raised fill */
  .subnav button.on { background: var(--surface-2); color: var(--text-1); font-weight: 600; }
  .pane { min-width: 0; padding-bottom: 24px; }
  h2 { margin: 0 0 16px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  h3 { margin: 28px 0 12px; padding-top: 20px; border-top: 1px solid var(--hairline); font-size: 14px; font-weight: 650; }
  .sub { font-size: 12px; font-weight: 400; color: var(--text-3); margin-left: 8px; letter-spacing: 0; }
  .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px 16px; margin: 12px 0 8px; align-items: start; }
  .group { margin: 16px 0 0; }
  .help { margin: 8px 0 12px; font-size: 12px; line-height: 1.55; color: var(--text-3); max-width: 78ch; }
  .help .mono { color: var(--text-2); }
  p.help.mono { font-size: 11.5px; }
  .quiet { color: var(--text-3); font-size: 13px; }
  .line { margin: 0 0 8px; font-size: 14px; }
  .url { margin-left: 10px; }
  .msg { margin: 0 0 12px; font-size: 13px; color: var(--text-2); }
  .msg.bad { color: var(--warn); }
  .row { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 8px 12px; margin: 12px 0 8px; }
  .sliderrow { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 16px; align-items: center; font-size: 13px; color: var(--text-2); }
  .sliderrow .mono { color: var(--text-1); margin-left: 4px; }
  .sliderrow input { width: 100%; }
  details { margin: 16px 0; padding-top: 12px; border-top: 1px solid var(--hairline); }
  summary { cursor: pointer; font-size: 13px; color: var(--text-2); }
  summary:hover { color: var(--text-1); }
  .admin { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 16px; margin: 12px 0; }
  .facts { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 8px 16px; margin: 0 0 12px; font-size: 13px; }
  .facts dt { color: var(--text-3); }
  .facts dd { margin: 0; color: var(--text-1); overflow-wrap: anywhere; }
</style>
