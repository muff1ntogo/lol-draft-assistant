<script>
  // Live: the broadcast desk (docs/ui-redesign.md). One question answered in one second (what do we do now), depth one
  // look away. The scorebar says whose turn it is; the two team rails carry the side colours; the centre is a ranked
  // shortlist. Only events animate (lock-in, ban, turn change, timer); nothing moves while you read.
  import { banStamp, lockIn, rise, swap } from './motion.js';
  import Champ from './Champ.svelte';
  import ComponentBar from './ComponentBar.svelte';
  import ComponentLegend from './ComponentLegend.svelte';
  import CompPanel from './CompPanel.svelte';
  import ScoutBox from './ScoutBox.svelte';
  import StrategyView from './StrategyView.svelte';
  import Notice from './ui/Notice.svelte';

  let { view, secondsLeft, notices = [] } = $props();
  const center = $derived(view.center);
  const pct = (p) => (p < 0.005 ? '<1%' : `${Math.round(p * 100)}%`);
  const pp = (x) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(1)}pp`;
  const theirSide = $derived(view.side === 'blue' ? 'red' : 'blue');
  const ourTurn = $derived(view.turn === 'us');

  // Draft (suggestions) or Strategy (runes, builds, game plan; docs/strategy.md); remembered per browser
  let tab = $state('draft');
  try { tab = localStorage.getItem('live.tab') === 'strategy' ? 'strategy' : 'draft'; } catch {}
  function show(t) {
    tab = t;
    try { localStorage.setItem('live.tab', t); } catch {}
  }
  // D / S switch views; ignored while typing (the Scout box)
  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target?.tagName ?? '')) return;
    if (e.key === 'd' || e.key === 'D') show('draft');
    else if (e.key === 's' || e.key === 'S') show('strategy');
  }

  // The timer bar drains from the longest time seen this step: the client doesn't say how long a turn is, so the
  // bar never claims a length it wasn't given.
  let turnKey = $state(null);
  let turnTotal = $state(30);
  $effect(() => {
    const key = `${view.game_id}:${view.step_index}`;
    if (key !== turnKey) { turnKey = key; turnTotal = Math.max(1, view.seconds_left ?? 30); }
    else if ((view.seconds_left ?? 0) > turnTotal) turnTotal = view.seconds_left;
  });
  const timeShare = $derived(secondsLeft == null ? 0 : Math.max(0, Math.min(1, secondsLeft / turnTotal)));
  const turnText = $derived(
    view.turn === 'us' ? `Your ${view.action ?? 'turn'}` : view.turn === 'them' ? `Their ${view.action ?? 'turn'}` : '',
  );
</script>

<svelte:window onkeydown={onKey} />

<!-- ======== scorebar: our side tag · phase and whose turn · the timer · their side tag ======== -->
<header class="scorebar" class:ours={ourTurn} class:theirs={view.turn === 'them'}>
  <div class="tag {view.side}"><span class="sidename">{view.side}</span><span class="you">Us</span></div>
  <div class="mid">
    <span class="phase">{view.phase}</span>
    {#key turnText}<span class="turn" in:swap>{turnText}</span>{/key}
  </div>
  <div class="clock" class:soon={secondsLeft != null && secondsLeft < 10} class:urgent={secondsLeft != null && secondsLeft < 5}>
    <span class="secs mono">{secondsLeft == null ? '' : Math.ceil(secondsLeft)}</span>
  </div>
  <div class="tag {theirSide} right"><span class="you">Them</span><span class="sidename">{theirSide}</span></div>
  <span class="timebar" style="transform: scaleX({timeShare})" aria-hidden="true"></span>
</header>

<div class="views" role="tablist" aria-label="Live view">
  <button role="tab" aria-selected={tab === 'draft'} class:on={tab === 'draft'} onclick={() => show('draft')} title="Draft (D)">Draft <kbd>D</kbd></button>
  <button role="tab" aria-selected={tab === 'strategy'} class:on={tab === 'strategy'} onclick={() => show('strategy')} title="Strategy (S)">
    Strategy{#if view.strategy?.players?.length} <span class="count mono">{view.strategy.players.length}</span>{/if} <kbd>S</kbd></button>
</div>
<!-- the app's messages, one line: the scorebar stays flush to the top during a draft -->
{#if notices.length}<div class="appnotice"><Notice compact items={notices} /></div>{/if}

{#if tab === 'strategy'}
<StrategyView strategy={view.strategy} />
{:else}

<main class="desk">
  <!-- ======== our rail ======== -->
  <section class="team ours {view.side}" aria-label="Our team">
    {#each view.our_slots as slot}
      <div class="slot" class:local={slot.is_local} class:filled={slot.champ}>
        <span class="role mono">{slot.role_label}</span>
        {#if slot.champ}
          {#key slot.champ.name}
            <span class="pick" in:lockIn={{ side: 'left' }}>
              <Champ champ={slot.champ} size={52} />
              <span class="name">{slot.champ.name}{#if slot.badge?.text}<small class="tagword">{slot.badge.text}</small>{/if}</span>
            </span>
          {/key}
        {:else if slot.hover}
          <span class="pick hovering">
            <Champ champ={slot.hover} size={52} faded />
            <span class="name">{slot.hover.name}<small class="tagword">hovering</small>
              {#if slot.hover_score?.warning}<em class="warn">⚠ {slot.hover_score.warning}</em>{/if}</span>
          </span>
        {:else}
          <span class="pick"><span class="empty"></span></span>
        {/if}
      </div>
    {/each}
    <div class="bans" aria-label="Our bans">
      {#each view.our_bans as ban (ban.name)}<span in:banStamp><Champ champ={ban} size={30} banned /></span>{/each}
    </div>
    {#if view.comp}<CompPanel comp={view.comp.ours} title="Our comp" />{/if}
  </section>

  <!-- ======== the centre: the ranked shortlist for this step ======== -->
  <section class="center" class:stale={view.center_stale && !view.center_stale.updating} aria-live="polite">
    {#if view.center_stale}
      <div class="notice" class:quiet={view.center_stale.updating}>
        {#if view.center_stale.updating}Updating…{:else}⚠ Suggestions couldn't update: showing the last good ones{#if !view.center_stale.same_step} (from {view.center_stale.phase}){/if}{/if}
      </div>
    {/if}
    {#if view.panel_errors?.filter((e) => e !== 'suggestions').length}
      <div class="notice quiet">Not shown: {view.panel_errors.filter((e) => e !== 'suggestions').join(', ')} (see the log)</div>
    {/if}

    {#if center.mode === 'updating'}
      <h2 class="headline">{center.title}</h2>
      <p class="muted">Working out suggestions…</p>
    {:else if center.mode === 'unavailable'}
      <h2 class="headline">{center.title}</h2>
      <p class="muted">Suggestions are unavailable for this step (see cache/launcher.log). The board stays live.</p>

    {:else if center.mode === 'our_ban_group'}
      <div class="head"><h2 class="headline">{center.title}</h2><ComponentLegend /></div>
      <div class="group">
        {#each center.cards as card, i}
          <article class="gcard {card.status}" in:rise|global={{ i }}>
            <div class="owner mono">{card.role_label}{#if card.player} <span>{card.player}</span>{/if}</div>
            {#if card.status === 'locked'}
              <div class="status">locked</div>
              <Champ champ={card.status_champ} size={64} banned />
              <div class="gname">{card.status_champ.name}</div>
            {:else if card.suggestion}
              <Champ champ={card.suggestion.champ} size={64} />
              <div class="gname">{card.suggestion.champ.name}{#if card.suggestion.plan} <span class="plantag">plan</span>{/if}</div>
              {#if card.suggestion.badge?.text}<span class="tagword">{card.suggestion.badge.text}</span>{/if}
              <div class="greason">{card.suggestion.reason}</div>
              <ComponentBar components={card.suggestion.components} />
              {#if card.status === 'hovering'}<div class="hovernote">hovering {card.status_champ.name}</div>{/if}
            {:else if card.status === 'skipped'}
              <div class="status">skipped</div>
            {:else}
              <div class="free">{card.note}</div>
              {#if card.status === 'hovering'}<div class="hovernote">hovering {card.status_champ.name}</div>{/if}
            {/if}
          </article>
        {/each}
      </div>

    {:else if center.mode === 'our_ban' || center.mode === 'our_pick'}
      <div class="head"><h2 class="headline">{center.title}</h2>{#if center.mode === 'our_ban'}<ComponentLegend />{/if}</div>
      {#if center.check}
        <div class="checks">
          <span>Picking <strong>{center.check.picking}</strong></span>
          <span class:no={!center.check.fight_starter}>{center.check.fight_starter ? '✓' : '✕'} Fight starter</span>
          <span class:no={!center.check.frontline}>{center.check.frontline ? '✓' : '✕'} Frontline</span>
        </div>
      {/if}
      <ol class="shortlist">
        {#each center.cards as card, i}
          {#if center.scouted && card.group !== center.cards[i - 1]?.group}
            <li class="groupname label-caps">{card.group === 'pool' ? 'From their pools' : 'Strongest of the rest'}</li>
          {/if}
          <li class="row" class:top={i === 0} class:highlighted={card.highlighted} in:rise|global={{ i }}>
            <span class="rank mono">{i + 1}</span>
            <Champ champ={card.champ} size={i === 0 ? 64 : 52} />
            <div class="body">
              <div class="title">
                <span class="cname">{card.champ.name}</span>
                {#if card.role_label}<span class="meta">{card.role_label}</span>{/if}
                {#if card.plan}<span class="plantag">plan</span>{/if}
                {#if card.badge?.text}<span class="tagword">{card.badge.text}</span>{/if}
                {#if card.archetype}<span class="tagword">{card.archetype.replaceAll('_', ' ')}</span>{/if}
              </div>
              {#if card.target}<div class="target">{card.target.role_label} {card.target.player}: {#if card.target.games}{card.target.games} games{#if card.target.share != null} ({Math.round(card.target.share * 100)}% of theirs){/if}, {Math.round((100 * card.target.wins) / card.target.games)}% won{:else}#{card.target.mastery_rank} by mastery{/if}</div>{/if}
              <div class="reason">{card.reason}{#if card.effect?.length}<span class="effect">{` → ${card.effect.join(', ')}`}</span>{/if}</div>
              {#if card.contested || card.low_data || card.warnings.length}
                <div class="flags">
                  {#if card.contested}<span class="contested" title="How likely their next pick takes it">contested · {pct(card.contested)} they pick it next</span>{/if}
                  {#if card.low_data}<span class="lowdata">{card.low_data}</span>{/if}
                  {#each card.warnings as w}<span class="warn">⚠ {w}</span>{/each}
                </div>
              {/if}
            </div>
            {#if card.components}<div class="figure"><ComponentBar components={card.components} /></div>{:else}<span></span>{/if}
          </li>
        {:else}
          <li class="muted">No useful {center.mode === 'our_ban' ? 'ban' : 'pick'} left</li>
        {/each}
      </ol>
      {#if center.hover_card}
        <div class="hovercard">
          <span class="label-caps">Your hover</span>
          <Champ champ={center.hover_card.champ} size={44} />
          <div class="body">
            <div class="title"><span class="cname small">{center.hover_card.champ.name}</span>
              {#if center.hover_card.delta_pp != null}<span class="delta mono" class:neg={center.hover_card.delta_pp < 0}>{pp(center.hover_card.delta_pp)}</span><span class="meta">vs {center.hover_card.vs_name}</span>{/if}</div>
            <div class="reason small">{center.hover_card.reason}</div>
            {#each center.hover_card.warnings as w}<div class="warn">⚠ {w}</div>{/each}
          </div>
        </div>
      {/if}
      {#if center.next_two?.length}
        <h3 class="label-caps nexthead">Next 2 picks</h3>
        <div class="pairs">
          {#each center.next_two as pair}
            <div class="pair">
              {#each pair.picks as p}
                <span class="pairpick"><Champ champ={p.champ} size={36} /><span><strong title={p.badge?.text || null}>{p.champ.name}</strong> <span class="meta">{p.role_label}</span>
                  <small title="Meta: win rate in {p.bracket}. You: the player's games on it in this role, last 60 days."><span class="mono">{p.meta_wr == null ? '—' : Math.round(p.meta_wr * 100) + '%'}</span> meta · {#if p.player_games}<span class="mono">{Math.round(p.player_wr * 100)}%</span> you ({p.player_games} g){:else}no games yet{/if}</small></span></span>
              {/each}
              <span class="syn mono" class:pos={pair.synergy_pp > 0.5} class:neg={pair.synergy_pp < -0.5}
                    title={pair.low_data ? 'Few games together: shrunk towards 0' : 'Pair synergy'}>
                {pair.synergy_pp > 0 ? '+' : pair.synergy_pp < 0 ? '−' : ''}{Math.abs(pair.synergy_pp).toFixed(1)}pp{pair.low_data ? '*' : ''}</span>
            </div>
          {/each}
        </div>
      {/if}

    {:else if center.mode === 'enemy_turn'}
      <div class="head"><h2 class="headline dim">{center.title}</h2><span class="meta">their most likely picks · our answer</span></div>
      <ol class="shortlist enemy">
        {#each center.cards as card, i}
          <li class="row" in:rise|global={{ i }}>
            <span class="prob mono">{pct(card.prob)}</span>
            <Champ champ={card.champ} size={52} />
            <div class="body">
              <div class="title"><span class="cname">{card.champ.name}</span><span class="meta">{card.role_label}</span>
                {#if card.badge?.text}<span class="tagword">{card.badge.text}</span>{/if}</div>
            </div>
            <div class="answer">
              {#if card.answer}
                <span class="label-caps">Our answer</span>
                <span class="ans"><Champ champ={card.answer.champ} size={32} /><strong>{card.answer.champ.name}</strong>
                  <span class="mono" class:pos={card.answer.resp_pp > 0} class:neg={card.answer.resp_pp < 0}>{pp(card.answer.resp_pp)}</span></span>
              {:else}
                <span class="meta">{card.answer_note?.replace(/[()]/g, '')}</span>
              {/if}
            </div>
          </li>
        {/each}
      </ol>

    {:else if center.mode === 'locked'}
      <div class="final">
        {#if center.calibrated_win_prob != null}
          <div class="winprob mono">{Math.round(center.calibrated_win_prob * 100)}<small>%</small></div>
          <p class="muted" title="The draft and our comfort, scaled as crawled games say draft numbers should be. History starts each game's strip from this figure.">
            win probability, calibrated · the draft model's raw figure is <span class="mono">{Math.round(center.win_prob * 100)}%</span></p>
        {:else}
          <div class="winprob mono">{Math.round(center.win_prob * 100)}<small>%</small></div>
          <p class="muted">model win probability</p>
        {/if}
      </div>
      <h3 class="label-caps nexthead">Biggest threats</h3>
      <ol class="shortlist enemy">
        {#each center.threats as t}
          <li class="row"><span class="prob"></span><Champ champ={t.champ} size={44} />
            <div class="body"><div class="title"><span class="cname small">{t.champ.name}</span></div><div class="reason small">{t.reason}</div></div></li>
        {/each}
      </ol>
    {/if}
  </section>

  <!-- ======== their rail ======== -->
  <section class="team theirs {theirSide}" aria-label="Their team">
    {#each view.their_slots as slot}
      <div class="slot" class:open={!slot.champ} class:filled={slot.champ}>
        {#if slot.champ}
          {#key slot.champ.name}
            <span class="pick" in:lockIn={{ side: 'right' }}>
              <span class="name">{slot.champ.name}{#if slot.badge?.text}<small class="tagword">{slot.badge.text}</small>{/if}
                {#if slot.role_probs}<small class="roleprobs mono" title="Their roles are guessed from how often each champion is played in each role; suggestions assume the first">{slot.role_probs.map((r) => `${r.role_label} ${Math.round(r.p * 100)}%`).join(' / ')}</small>{/if}</span>
              <Champ champ={slot.champ} size={52} />
            </span>
          {/key}
        {:else if slot.likely?.length}
          <span class="likely" title="Most likely picks for their {slot.open_role_label}: meta in the enemy bracket{slot.scout ? ', blended with the player\'s pool' : ''}">
            {#each slot.likely as l}
              <span class="lk"><Champ champ={l.champ} size={28} faded /><small class="mono">{Math.round(l.p * 100)}%</small></span>
            {/each}
          </span>
        {:else}
          <span class="pick"><span class="empty"></span></span>
        {/if}
        <span class="role" class:scouted={slot.scout}>
          <span class="mono">{slot.role_label || slot.open_role_label || (slot.scout?.role ?? '')}</span>
          {#if slot.name_only}
            <span class="player" title="The client gave this name without its tag: paste {slot.name_only}#TAG in Scout opponents to scout them">{slot.name_only} <small class="rk">no tag</small></span>
          {/if}
          {#if slot.scout}
            <span class="player" title={slot.scout.riot_id}>{slot.scout.name}{#if slot.scout.rank} <small class="rk">{slot.scout.rank}</small>{/if}</span>
            <span class="pool">
              {#each slot.scout.top as t}
                <span title={t.games ? `${t.champ?.name}: ${t.games} of their last 30 games, ${Math.round((100 * t.wins) / t.games)}% won${t.this_patch ? `, ${t.this_patch} this patch` : ''}` : `${t.champ?.name}: mastery`}>
                  <Champ champ={t.champ} size={20} /></span>
              {/each}
            </span>
            {#if slot.scout.one_trick}<span class="flag" title="{slot.scout.one_trick.name} is most of their {slot.scout.games_in_role} recent games in this role">one-trick</span>{/if}
            {#if slot.scout.mastery_only}<span class="flag" title="No recent ranked games: their pool is from mastery">mastery only</span>{/if}
            {#if slot.scout.role_sure != null && slot.scout.role_sure < 0.85}<span class="flag" title="The role is guessed from their games and mastery">role? {Math.round(slot.scout.role_sure * 100)}%</span>{/if}
            {#if !['done', 'missing', 'error'].includes(slot.scout.status)}<span class="flag">scouting…</span>{/if}
          {/if}
        </span>
      </div>
    {/each}
    <div class="bans" aria-label="Their bans">
      {#each view.their_bans as ban (ban.name)}<span in:banStamp><Champ champ={ban} size={30} banned /></span>{/each}
    </div>
    {#if view.comp}<CompPanel comp={view.comp.theirs} title="Their comp" />{/if}
    <ScoutBox compact />
  </section>
</main>
{/if}

<style>
  /* ================= scorebar ================= */
  .scorebar { position: relative; display: grid; grid-template-columns: 1fr auto auto 1fr; align-items: stretch;
              height: 72px; background: var(--surface-1); border-bottom: 1px solid var(--hairline); overflow: hidden; }
  .tag { display: flex; align-items: center; gap: 12px; padding: 0 24px; font-weight: 750; letter-spacing: 0.08em;
         text-transform: uppercase; font-size: 13px; }
  .tag.right { justify-content: flex-end; }
  .tag.blue { box-shadow: inset 4px 0 0 var(--side-blue); background: linear-gradient(90deg, var(--side-blue-soft), transparent 70%); }
  .tag.red { box-shadow: inset 4px 0 0 var(--side-red); background: linear-gradient(90deg, var(--side-red-soft), transparent 70%); }
  .tag.right.blue { box-shadow: inset -4px 0 0 var(--side-blue); background: linear-gradient(270deg, var(--side-blue-soft), transparent 70%); }
  .tag.right.red { box-shadow: inset -4px 0 0 var(--side-red); background: linear-gradient(270deg, var(--side-red-soft), transparent 70%); }
  .tag.blue .sidename { color: var(--side-blue); }
  .tag.red .sidename { color: var(--side-red); }
  .you { color: var(--text-3); font-weight: 600; }
  .mid { display: flex; flex-direction: column; justify-content: center; align-items: center; padding: 0 28px; min-width: 260px; }
  .phase { font-size: 13px; color: var(--text-3); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .turn { font-size: 26px; font-weight: 750; letter-spacing: -0.02em; text-transform: capitalize; line-height: 1.1; }
  /* the turn change: our turn lights the centre in the signal; theirs stays neutral */
  .scorebar.ours .turn { color: var(--signal); }
  .scorebar .mid { transition: background-color 240ms var(--ease-out); }
  .clock { display: grid; place-items: center; width: 96px; border-left: 1px solid var(--hairline); border-right: 1px solid var(--hairline); }
  .secs { font-size: 40px; font-weight: 600; letter-spacing: -0.04em; }
  .clock.soon .secs { color: var(--warn); }
  .clock.urgent .secs { color: var(--error); }
  .timebar { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: var(--text-3); transform-origin: left;
             transition: transform 200ms linear; }
  .scorebar.ours .timebar { background: var(--signal); }

  /* ================= view switch ================= */
  .views { display: flex; gap: 2px; padding: 10px 24px 0; }
  .views button { display: inline-flex; align-items: center; gap: 8px; background: none; border: 0; color: var(--text-3);
                  font-size: 13px; font-weight: 600; padding: 6px 10px; border-radius: var(--radius-sm); }
  .views button.on { color: var(--text-1); background: var(--surface-2); }
  .appnotice { padding: 4px 24px 0; }
  @media (max-width: 1180px) { .appnotice { padding: 4px 16px 0; } }
  kbd { font-family: var(--font-mono); font-size: 10px; color: var(--text-3); border: 1px solid var(--hairline-strong);
        border-radius: 3px; padding: 0 4px; line-height: 15px; }
  .count { font-size: 11px; color: var(--text-2); }

  /* ================= the desk ================= */
  .desk { display: grid; grid-template-columns: 272px minmax(0, 1fr) 272px; gap: 28px; padding: 16px 24px 32px; }
  @media (max-width: 1180px) { .desk { grid-template-columns: 220px minmax(0, 1fr) 220px; gap: 18px; padding: 12px 16px 24px; } }
  @media (max-width: 820px) { .desk { grid-template-columns: 1fr; } .scorebar { grid-template-columns: 1fr auto auto; } .tag.right { display: none; } }

  /* ---- team rails: the side colour is the rail's edge, nothing else ---- */
  .team { display: flex; flex-direction: column; gap: 4px; }
  .slot { position: relative; display: flex; align-items: center; gap: 12px; min-height: 68px; padding: 8px 12px;
          background: var(--surface-1); }
  .ours .slot { box-shadow: inset 3px 0 0 var(--hairline-strong); }
  .theirs .slot { box-shadow: inset -3px 0 0 var(--hairline-strong); justify-content: flex-end; }
  .ours.blue .slot.filled { box-shadow: inset 3px 0 0 var(--side-blue); }
  .ours.red .slot.filled { box-shadow: inset 3px 0 0 var(--side-red); }
  .theirs.blue .slot.filled { box-shadow: inset -3px 0 0 var(--side-blue); }
  .theirs.red .slot.filled { box-shadow: inset -3px 0 0 var(--side-red); }
  .slot:first-child { border-radius: var(--radius) var(--radius) 0 0; }
  .slot:nth-child(5) { border-radius: 0 0 var(--radius) var(--radius); }
  .slot.local { background: var(--surface-2); }
  .slot.local::after { content: "you"; position: absolute; top: 6px; right: 8px; font-size: 10px; font-weight: 700;
                       letter-spacing: 0.06em; text-transform: uppercase; color: var(--signal); }
  .role { flex: none; width: 54px; font-size: 11px; color: var(--text-3); text-transform: uppercase; }
  .theirs .role { width: auto; min-width: 54px; text-align: right; }
  .role.scouted { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; max-width: 108px; }
  .pick { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .name { display: flex; flex-direction: column; gap: 2px; font-size: 17px; font-weight: 650; letter-spacing: -0.01em; min-width: 0; }
  .theirs .name { align-items: flex-end; text-align: right; }
  .hovering .name { color: var(--text-3); font-weight: 500; }
  .empty { width: 52px; height: 52px; border-radius: var(--radius-sm); border: 1px dashed var(--hairline-strong); }
  .player { font-size: 12px; color: var(--text-1); max-width: 108px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rk { font-size: 10px; color: var(--text-3); }
  .pool { display: flex; gap: 2px; }
  .flag { font-size: 10px; color: var(--text-2); border: 1px solid var(--hairline-strong); border-radius: 3px; padding: 0 4px; }
  .roleprobs { font-size: 11px; color: var(--text-3); font-weight: 500; }
  .likely { display: flex; gap: 6px; }
  .lk { display: flex; flex-direction: column; align-items: center; gap: 2px; font-size: 10px; color: var(--text-3); }
  .bans { display: flex; gap: 4px; flex-wrap: wrap; margin: 8px 0 4px; min-height: 30px; }
  .theirs .bans { justify-content: flex-end; }

  /* ---- the centre ---- */
  .center { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .center.stale .shortlist, .center.stale .group { opacity: 0.55; }
  .notice { padding: 8px 12px; border-radius: var(--radius-sm); font-size: 13px; color: var(--warn); background: rgba(240, 180, 41, 0.1); }
  .notice.quiet { color: var(--text-3); background: none; padding: 0; }
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .headline { margin: 0; font-size: 34px; font-weight: 750; letter-spacing: -0.025em; line-height: 1.1; }
  .headline.dim { color: var(--text-2); }
  .muted { color: var(--text-3); font-size: 15px; margin: 0; }
  .meta { color: var(--text-3); font-size: 14px; font-weight: 500; }
  .checks { display: flex; gap: 18px; font-size: 13px; color: var(--text-2); }
  .checks .no { color: var(--warn); }

  .shortlist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; border-top: 1px solid var(--hairline); }
  .groupname { padding: 14px 0 6px; }
  .row { display: grid; grid-template-columns: 28px auto minmax(0, 1fr) auto; align-items: center; gap: 16px;
         padding: 12px 12px 12px 8px; border-bottom: 1px solid var(--hairline); }
  .row.top { background: var(--surface-1); box-shadow: inset 3px 0 0 var(--signal); padding-top: 16px; padding-bottom: 16px; }
  .row.highlighted:not(.top) { background: var(--surface-1); }
  .rank { font-size: 18px; font-weight: 600; color: var(--text-3); text-align: center; }
  .row.top .rank { color: var(--signal); font-size: 22px; }
  .body { min-width: 0; }
  .title { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
  .cname { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
  .row:not(.top) .cname { font-size: 20px; }
  .cname.small { font-size: 17px; }
  .tagword { font-size: 11px; font-weight: 600; color: var(--text-2); letter-spacing: 0.02em; }
  .tagword::before { content: "· "; color: var(--text-3); }
  .name .tagword::before { content: none; }
  .name .tagword { color: var(--text-3); font-weight: 500; }
  .plantag { font-size: 11px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--signal-ink);
             background: var(--text-2); border-radius: 3px; padding: 1px 5px; }
  .target { font-size: 13px; color: var(--text-2); margin-top: 2px; }
  .reason { font-size: 16px; color: var(--text-2); margin-top: 3px; line-height: 1.35; }
  .reason.small { font-size: 14px; }
  .row:not(.top) .reason { font-size: 15px; }
  .effect { color: var(--text-1); }
  .flags { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 5px; font-size: 12px; }
  .contested { color: var(--down-text); }
  .lowdata { color: var(--text-3); font-style: italic; }
  .warn { color: var(--warn); font-style: normal; font-size: 12px; }
  .figure :global(.bar) { margin-top: 0; }
  .figure { justify-self: end; width: 180px; }
  @media (max-width: 1180px) { .figure { width: 110px; } }

  .hovercard { display: grid; grid-template-columns: auto auto 1fr; align-items: center; gap: 14px; padding: 10px 12px;
               border: 1px dashed var(--hairline-strong); border-radius: var(--radius); }
  .delta { font-size: 14px; font-weight: 600; color: var(--up-text); }
  .delta.neg { color: var(--down-text); }
  .nexthead { margin: 10px 0 0; }
  .pairs { display: flex; flex-direction: column; border-top: 1px solid var(--hairline); }
  .pair { display: grid; grid-template-columns: 1fr 1fr 72px; align-items: center; gap: 14px; padding: 8px 4px;
          border-bottom: 1px solid var(--hairline); }
  .pairpick { display: flex; align-items: center; gap: 10px; font-size: 15px; min-width: 0; }
  .pairpick > span { display: flex; flex-direction: column; min-width: 0; }
  .pairpick small { color: var(--text-3); font-size: 12px; }
  .syn { text-align: right; font-size: 14px; color: var(--text-2); }
  .pos { color: var(--up-text); }
  .neg { color: var(--down-text); }

  /* enemy turn: their probability where our rank would be, our answer on the right */
  .shortlist.enemy .row { grid-template-columns: 44px auto minmax(0, 1fr) auto; }
  .prob { font-size: 15px; color: var(--text-2); text-align: right; }
  .answer { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
  .ans { display: flex; align-items: center; gap: 8px; font-size: 15px; }

  /* ban groups (Flex: five simultaneous bans) */
  .group { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
  .gcard { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 6px;
           background: var(--surface-1); border-radius: var(--radius); padding: 12px 10px; border-top: 2px solid var(--hairline-strong); }
  .gcard.locked { opacity: 0.7; }
  .owner { font-size: 11px; color: var(--text-3); text-transform: uppercase; }
  .owner span { text-transform: none; color: var(--text-1); font-family: var(--font-ui); margin-left: 4px; }
  .gname { font-size: 17px; font-weight: 700; }
  .gcard :global(.bar) { max-width: 160px; }
  .greason { font-size: 13px; color: var(--text-2); line-height: 1.3; }
  .status { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; }
  .hovernote { font-size: 12px; color: var(--text-2); }
  .free { font-size: 13px; color: var(--text-3); margin-top: 20px; }

  /* locked in */
  .final { display: flex; flex-direction: column; gap: 4px; padding: 8px 0 4px; }
  .winprob { font-size: 88px; font-weight: 600; line-height: 1; letter-spacing: -0.05em; }
  .winprob small { font-size: 40px; color: var(--text-3); }
</style>
