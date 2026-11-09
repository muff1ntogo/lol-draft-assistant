<script>
  import { rise, swap } from './motion.js';
  import Champ from './Champ.svelte';
  import ComponentBar from './ComponentBar.svelte';
  import ComponentLegend from './ComponentLegend.svelte';
  import CompPanel from './CompPanel.svelte';
  import ScoutBox from './ScoutBox.svelte';
  import StrategyView from './StrategyView.svelte';

  let { view, secondsLeft } = $props();
  const center = $derived(view.center);
  const pct = (p) => (p < 0.005 ? '<1%' : `${Math.round(p * 100)}%`);
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
  const pp = (x) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(1)}pp`;
</script>

<svelte:window onkeydown={onKey} />
<header class="top {view.side}">
  <span class="side">{view.side.toUpperCase()} SIDE</span>
  <span class="phase">{view.phase}</span>
  <span class="turn">
    {#if view.turn === 'us'}OUR {view.action?.toUpperCase()}{:else if view.turn === 'them'}THEIR {view.action?.toUpperCase()}{/if}
  </span>
  <span class="timer" class:soon={secondsLeft != null && secondsLeft < 10} class:urgent={secondsLeft != null && secondsLeft < 5}>{secondsLeft == null ? '' : Math.ceil(secondsLeft)}</span>
</header>
<div class="tabs" role="tablist">
  <button role="tab" aria-selected={tab === 'draft'} class:on={tab === 'draft'} onclick={() => show('draft')} title="Draft (D)">Draft</button>
  <button role="tab" aria-selected={tab === 'strategy'} class:on={tab === 'strategy'} onclick={() => show('strategy')} title="Strategy (S)">
    Strategy{#if view.strategy?.players?.length} <span class="count">{view.strategy.players.length}</span>{/if}</button>
</div>

{#if tab === 'strategy'}
<StrategyView strategy={view.strategy} />
{:else}

<main class="board">
  <section class="team ours">
    {#each view.our_slots as slot}
      <div class="slot" class:local={slot.is_local}>
        <span class="role">{slot.role_label}</span>
        {#if slot.champ}
          <!-- a lock-in replaces the faded hover in place: crossfade with scale and blur -->
          <span class="lockin" in:swap><Champ champ={slot.champ} size={56} /></span>
          <span class="name">{slot.champ.name}{#if slot.badge?.text}<small class="badge">{slot.badge.text}</small>{/if}</span>
        {:else if slot.hover}
          <Champ champ={slot.hover} size={56} faded />
          <span class="name hover">
            {slot.hover.name}
            {#if slot.hover_score?.warning}<em class="warn">{slot.hover_score.warning}</em>{/if}
          </span>
        {:else}
          <span class="empty"></span>
        {/if}
      </div>
    {/each}
    <div class="bans">
      {#each view.our_bans as ban}<Champ champ={ban} size={36} banned />{/each}
    </div>
    {#if view.comp}<CompPanel comp={view.comp.ours} title="Our comp" />{/if}
  </section>

  <section class="center" class:stale={view.center_stale && !view.center_stale.updating}>
    {#if view.center_stale}
      <div class="stalestrip" class:updating={view.center_stale.updating}>
        {#if view.center_stale.updating}Updating…{:else}Suggestions couldn't update: showing the last good ones{#if !view.center_stale.same_step} (from {view.center_stale.phase}){/if}{/if}
      </div>
    {/if}
    {#if view.panel_errors?.filter((e) => e !== 'suggestions').length}
      <div class="panelnote">Not shown: {view.panel_errors.filter((e) => e !== 'suggestions').join(', ')} (see the log)</div>
    {/if}
    {#if center.mode === 'updating'}
      <h2>{center.title}</h2>
      <p class="muted">Working out suggestions…</p>
    {:else if center.mode === 'unavailable'}
      <h2>{center.title}</h2>
      <p class="muted">Suggestions are unavailable for this step (see cache/launcher.log). The board stays live.</p>
    {:else if center.mode === 'our_ban_group'}
      <div class="grouphead"><h2>{center.title}</h2><ComponentLegend /></div>
      <div class="group">
        {#each center.cards as card, i}
          <article class="gcard {card.status}" in:rise|global={{ i }}>
            <div class="owner">{card.role_label}{#if card.player} <span>{card.player}</span>{/if}</div>
            {#if card.status === 'locked'}
              <div class="status">locked</div>
              <Champ champ={card.status_champ} size={72} banned />
              <div class="gname">{card.status_champ.name}</div>
            {:else if card.suggestion}
              <Champ champ={card.suggestion.champ} size={72} />
              <div class="gname">{card.suggestion.champ.name}{#if card.suggestion.plan} <span class="plantag">plan</span>{/if}</div>
              {#if card.suggestion.badge?.text}<span class="badge">{card.suggestion.badge.text}</span>{/if}
              <div class="greason">{card.suggestion.reason}</div>
              <ComponentBar components={card.suggestion.components} />
              {#if card.status === 'hovering'}
                <div class="hovering">hovering {card.status_champ.name}</div>
              {/if}
            {:else if card.status === 'skipped'}
              <div class="status">skipped</div>
            {:else}
              <div class="free">{card.note}</div>
              {#if card.status === 'hovering'}<div class="hovering">hovering {card.status_champ.name}</div>{/if}
            {/if}
          </article>
        {/each}
      </div>
    {:else if center.mode === 'our_ban' || center.mode === 'our_pick'}
      <div class="grouphead"><h2>{center.title}</h2>{#if center.mode === 'our_ban'}<ComponentLegend />{/if}</div>
      {#if center.check}
        <div class="checkstrip">
          <span>Picking: {center.check.picking}</span>
          <span class:no={!center.check.fight_starter}>Fight starter: {center.check.fight_starter ? 'yes' : 'no'}</span>
          <span class:no={!center.check.frontline}>Frontline: {center.check.frontline ? 'yes' : 'no'}</span>
        </div>
      {/if}
      {#each center.cards as card, i}
        {#if center.scouted && card.group !== center.cards[i - 1]?.group}
          <h3 class="bangroup">{card.group === 'pool' ? 'From their pools' : 'Strongest of the rest'}</h3>
        {/if}
        <article class="card" class:highlighted={card.highlighted} class:compact={center.cards.length > 3} in:rise|global={{ i }}>
          <span class="rank">{i + 1}</span>
          <Champ champ={card.champ} size={center.cards.length > 3 ? 60 : 88} />
          <div>
            <div class="title">{card.champ.name}{#if card.role_label} <small>{card.role_label}</small>{/if}
              {#if card.plan}<span class="plantag">plan</span>{/if}
              {#if card.badge?.text}<span class="badge">{card.badge.text}</span>{/if}
              {#if card.archetype}<span class="badge">{card.archetype.replaceAll('_', ' ')}</span>{/if}</div>
            {#if card.target}<div class="target">{card.target.role_label} {card.target.player}: {#if card.target.games}{card.target.games} games{#if card.target.share != null} ({Math.round(card.target.share * 100)}% of theirs){/if}, {Math.round((100 * card.target.wins) / card.target.games)}% won{:else}#{card.target.mastery_rank} by mastery{/if}</div>{/if}
            <div class="reason">{card.reason}{#if card.effect?.length}<span class="effect">{` → ${card.effect.join(', ')}`}</span>{/if}</div>
            {#if card.components}<ComponentBar components={card.components} />{/if}
            {#if card.contested}<div class="contested" title="How likely their next pick takes it">contested: {pct(card.contested)} they pick it next</div>{/if}
            {#if card.low_data}<div class="lowdata">{card.low_data}</div>{/if}
            {#each card.warnings as w}<div class="warn">{w}</div>{/each}
          </div>
        </article>
      {:else}
        <p class="muted">No useful {center.mode === 'our_ban' ? 'ban' : 'pick'} left</p>
      {/each}
      {#if center.hover_card}
        <article class="card hovercard highlighted">
          <span class="rank">H</span>
          <Champ champ={center.hover_card.champ} size={64} />
          <div>
            <div class="title">Hover: {center.hover_card.champ.name}{#if center.hover_card.delta_pp != null} <small class="delta" class:neg={center.hover_card.delta_pp < 0}>{pp(center.hover_card.delta_pp)} vs {center.hover_card.vs_name}</small>{/if}</div>
            <div class="reason">{center.hover_card.reason}</div>
            {#each center.hover_card.warnings as w}<div class="warn">{w}</div>{/each}
          </div>
        </article>
      {/if}
      {#if center.next_two?.length}
        <h3 class="nexthead">Next 2 picks</h3>
        {#each center.next_two as pair}
          <div class="pairrow">
            {#each pair.picks as p}
              <span class="pairpick"><Champ champ={p.champ} size={44} /><span><strong title={p.badge?.text || null}>{p.champ.name} <small>{p.role_label}</small></strong>
                <small title="Meta: win rate in {p.bracket}. You: the player's games on it in this role, last 60 days.">{p.meta_wr == null ? '—' : Math.round(p.meta_wr * 100) + '%'} meta · {#if p.player_games}{Math.round(p.player_wr * 100)}% you ({p.player_games} g){:else}no games yet{/if}</small></span></span>
            {/each}
            <span class="pairstats">
              <span class="syn" class:pos={pair.synergy_pp > 0.5} class:neg={pair.synergy_pp < -0.5}
                    title={pair.low_data ? 'Few games together: shrunk towards 0' : 'Pair synergy'}>
                {pair.synergy_pp > 0 ? '+' : pair.synergy_pp < 0 ? '−' : ''}{Math.abs(pair.synergy_pp).toFixed(1)}pp together{pair.low_data ? '*' : ''}</span>
            </span>
          </div>
        {/each}
      {/if}
    {:else if center.mode === 'enemy_turn'}
      <h2>{center.title}</h2>
      <p class="muted">Their most likely picks</p>
      {#each center.cards as card, i}
        <article class="card enemy" in:rise|global={{ i }}>
          <Champ champ={card.champ} size={72} />
          <div>
            <div class="title">{card.champ.name} <small>{pct(card.prob)} · {card.role_label}</small>
              {#if card.badge?.text}<span class="badge">{card.badge.text}</span>{/if}</div>
            <div class="reason">
              {#if card.answer}
                Our answer: <strong>{card.answer.champ.name}</strong> ({pp(card.answer.resp_pp)})
              {:else}
                {card.answer_note?.replace(/[()]/g, "")}
              {/if}
            </div>
          </div>
        </article>
      {/each}
    {:else if center.mode === 'locked'}
      <h2>LOCKED IN</h2>
      {#if center.calibrated_win_prob != null}
        <div class="winprob">{Math.round(center.calibrated_win_prob * 100)}%</div>
        <p class="muted" title="The draft and our comfort, scaled as crawled games say draft numbers should be. History starts each game's strip from this figure.">
          win probability, calibrated · draft model's raw figure {Math.round(center.win_prob * 100)}%</p>
      {:else}
        <div class="winprob">{Math.round(center.win_prob * 100)}%</div>
        <p class="muted">model win probability</p>
      {/if}
      <h3>Biggest threats</h3>
      {#each center.threats as t}
        <article class="card enemy">
          <Champ champ={t.champ} size={64} />
          <div>
            <div class="title">{t.champ.name}</div>
            <div class="reason">{t.reason}</div>
          </div>
        </article>
      {/each}
    {/if}
  </section>

  <section class="team theirs">
    {#each view.their_slots as slot}
      <div class="slot" class:open={!slot.champ}>
        {#if slot.champ}
          <!-- a lock-in replaces the faded hover in place: crossfade with scale and blur -->
          <span class="lockin" in:swap><Champ champ={slot.champ} size={56} /></span>
          <span class="name">{slot.champ.name}{#if slot.badge?.text}<small class="badge">{slot.badge.text}</small>{/if}
            {#if slot.role_probs}<small class="roleprobs" title="Their roles are guessed from how often each champion is played in each role; suggestions assume the first">{slot.role_probs.map((r) => `${r.role_label} ${Math.round(r.p * 100)}%`).join(' / ')}</small>{/if}</span>
        {:else if slot.likely?.length}
          <span class="likely" title="Most likely picks for their {slot.open_role_label}: meta in the enemy bracket{slot.scout ? ', blended with the player\'s pool' : ''}">
            {#each slot.likely as l}
              <span class="lk"><Champ champ={l.champ} size={30} faded /><small>{Math.round(l.p * 100)}%</small></span>
            {/each}
          </span>
        {:else}
          <span class="empty"></span>
        {/if}
        <span class="role" class:scouted={slot.scout}>
          {slot.role_label || slot.open_role_label || (slot.scout?.role ?? '')}
          {#if slot.name_only}
            <span class="player" title="The client gave this name without its tag: paste {slot.name_only}#TAG in Scout opponents to scout them">{slot.name_only} <small class="rk">no tag</small></span>
          {/if}
          {#if slot.scout}
            <span class="player" title={slot.scout.riot_id}>{slot.scout.name}{#if slot.scout.rank} <small class="rk">{slot.scout.rank}</small>{/if}</span>
            <span class="pool">
              {#each slot.scout.top as t}
                <span title={t.games ? `${t.champ?.name}: ${t.games} of their last 30 games, ${Math.round((100 * t.wins) / t.games)}% won${t.this_patch ? `, ${t.this_patch} this patch` : ''}` : `${t.champ?.name}: mastery`}>
                  <Champ champ={t.champ} size={22} /></span>
              {/each}
            </span>
            {#if slot.scout.one_trick}<span class="label otp" title="{slot.scout.one_trick.name} is most of their {slot.scout.games_in_role} recent games in this role">one-trick</span>{/if}
            {#if slot.scout.mastery_only}<span class="label" title="No recent ranked games: their pool is from mastery">mastery only</span>{/if}
            {#if slot.scout.role_sure != null && slot.scout.role_sure < 0.85}<span class="label" title="The role is guessed from their games and mastery">role? {Math.round(slot.scout.role_sure * 100)}%</span>{/if}
            {#if !['done', 'missing', 'error'].includes(slot.scout.status)}<span class="scouting">scouting…</span>{/if}
          {/if}
        </span>
      </div>
    {/each}
    <div class="bans">
      {#each view.their_bans as ban}<Champ champ={ban} size={36} banned />{/each}
    </div>
    {#if view.comp}<CompPanel comp={view.comp.theirs} title="Their comp" />{/if}
    <ScoutBox compact />
  </section>
</main>
{/if}

<style>
  .timer.soon { color: var(--gold); }
  .timer.urgent { color: var(--red); }
  .contested { font-size: 14px; color: var(--down-text); margin-top: 2px; }
  .lowdata { font-size: 13px; color: var(--muted); font-style: italic; }
  .delta { font-size: 14px; color: var(--up); font-weight: 600; }
  .delta.neg { color: var(--down); }
  /* compact: the app beside the client on one screen */
  @media (max-width: 1100px) {
    .board { grid-template-columns: 200px 1fr 200px !important; gap: 14px !important; padding: 12px 14px !important; }
    .top { padding: 10px 14px !important; font-size: 20px !important; }
  }
  @media (max-width: 760px) {
    .board { grid-template-columns: 1fr !important; }
  }
  .tabs { display: flex; gap: 4px; padding: 8px 28px 0; border-bottom: 1px solid var(--line); }
  .tabs button { background: none; border: 0; color: var(--muted); font: inherit; font-size: 15px; padding: 6px 14px 8px;
                 cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -1px; }
  .tabs button.on { color: var(--text); font-weight: 600; border-bottom-color: var(--gold); }
  .tabs .count { font-size: 12px; background: var(--panel-2); border-radius: 8px; padding: 0 6px; margin-left: 4px; }
  .rk { font-size: 11px; color: var(--muted); }
  .roleprobs { display: block; font-size: 12px; color: var(--gold); font-weight: 400; }
  .likely { display: flex; gap: 6px; }
  .lk { display: flex; flex-direction: column; align-items: center; font-size: 11px; color: var(--muted); }
  .label { font-size: 11px; color: var(--gold); text-transform: none; border: 1px solid var(--gold-line); border-radius: 4px; padding: 0 4px; }
  .label.otp { color: var(--down-text); }
  .target { font-size: 14px; color: var(--link); margin: 2px 0; }
  .stalestrip { margin: 0 0 10px; padding: 6px 10px; border-radius: 6px; font-size: 14px; background: var(--gold-bg); color: var(--gold); }
  .stalestrip.updating { background: transparent; color: var(--muted); padding: 0; }
  .center.stale :global(article) { opacity: 0.6; }
  .panelnote { margin: 0 0 8px; font-size: 13px; color: var(--muted); }
  .checkstrip { display: flex; gap: 14px; margin: 0 0 8px; font-size: 14px; color: var(--muted); }
  .checkstrip .no { color: var(--down-text); font-weight: 600; }
  .plantag { font-size: 13px; font-weight: 600; color: var(--link); border: 1px solid var(--side-blue); border-radius: 4px;
             padding: 0 5px; margin-left: 6px; vertical-align: middle; }
  .top { display: grid; grid-template-columns: 1fr auto 1fr auto; align-items: center; gap: 24px;
         padding: 14px 28px; font-size: 26px; font-weight: 700; border-bottom: 4px solid var(--line); }
  .top.blue { border-color: var(--blue); }
  .top.red { border-color: var(--red); }
  .side { font-size: 20px; color: var(--muted); }
  .blue .side { color: var(--blue); }
  .red .side { color: var(--red); }
  .phase { font-size: 30px; }
  .turn { text-align: right; color: var(--gold); }
  .timer { min-width: 2.5em; text-align: right; font-variant-numeric: tabular-nums; font-size: 34px; }

  .board { display: grid; grid-template-columns: 260px 1fr 260px; gap: 24px; padding: 20px 28px; }
  .team { display: flex; flex-direction: column; gap: 10px; }
  .slot { display: flex; align-items: center; gap: 12px; background: var(--panel); border-radius: 8px;
          padding: 8px 12px; min-height: 72px; }
  .theirs .slot { justify-content: flex-end; }
  .slot.local { outline: 2px solid var(--gold); }
  .role { width: 64px; font-size: 14px; color: var(--muted); text-transform: uppercase; }
  .theirs .role { text-align: right; }
  /* docs/enemy-scouting.md: the player's name and what they play, under the role */
  .role.scouted { width: 110px; display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
  .player { font-size: 13px; color: var(--text); text-transform: none; max-width: 110px; overflow: hidden;
            text-overflow: ellipsis; white-space: nowrap; }
  .pool { display: flex; gap: 2px; }
  .scouting { font-size: 11px; color: var(--gold); text-transform: none; }
  .bangroup { font-size: 16px; margin: 4px 0 -6px; text-transform: uppercase; letter-spacing: 0.5px; }
  .name { font-size: 20px; display: flex; flex-direction: column; }
  .name.hover { color: var(--muted); font-style: italic; }
  .empty { width: 56px; height: 56px; border: 2px dashed var(--line); border-radius: 6px; }
  .bans { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .theirs .bans { justify-content: flex-end; }

  .center { display: flex; flex-direction: column; gap: 14px; }
  h2 { font-size: 44px; margin: 0 0 4px; letter-spacing: 1px; }
  h3 { font-size: 22px; margin: 12px 0 0; color: var(--muted); }
  .card { display: grid; grid-template-columns: auto auto 1fr; align-items: center; gap: 18px;
          background: var(--panel); border: 2px solid var(--line); border-radius: 10px; padding: 14px 18px; }
  .card.enemy { grid-template-columns: auto 1fr; }
  .nexthead { margin-top: 6px; }
  .pairrow { display: grid; grid-template-columns: 1fr 1fr auto; align-items: center; gap: 14px;
             background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 14px; }
  .pairpick { display: flex; align-items: center; gap: 10px; font-size: 18px; }
  .pairpick > span { display: flex; flex-direction: column; }
  .pairpick small { color: var(--muted); font-size: 13px; }
  .pairstats { display: flex; flex-direction: column; align-items: flex-end; font-size: 16px; font-variant-numeric: tabular-nums; }
  .syn.pos { color: var(--up-text); }
  .syn.neg { color: var(--down-text); }
  .card.highlighted { border-color: var(--gold); background: var(--panel-2); }
  .card.compact { gap: 14px; padding: 8px 14px; }
  .card.compact .title { font-size: 23px; }
  .card.compact .reason { font-size: 17px; margin-top: 2px; }
  .card.compact .rank { font-size: 22px; }
  .badge { font-size: 13px; font-weight: 500; color: var(--text-2); background: var(--line); border-radius: 4px;
           padding: 1px 6px; margin-left: 8px; white-space: nowrap; vertical-align: middle; }
  .name .badge { margin: 2px 0 0; align-self: flex-start; font-size: 12px; }
  .effect { color: var(--gold); }
  .hovercard { opacity: 0.9; }
  .rank { font-size: 28px; font-weight: 700; color: var(--muted); width: 1.2em; }
  .title { font-size: 30px; font-weight: 700; }
  .title small { font-size: 18px; color: var(--muted); font-weight: 500; margin-left: 10px; }
  .reason { font-size: 22px; color: var(--text-2); margin-top: 4px; }
  .warn { color: var(--warn); font-size: 18px; font-style: normal; margin-top: 4px; }
  .muted { color: var(--muted); font-size: 18px; margin: 0; }
  .winprob { font-size: 96px; font-weight: 800; line-height: 1; }
  .grouphead { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
  .group { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
  .gcard { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 6px;
           background: var(--panel); border: 2px solid var(--line); border-radius: 10px; padding: 12px 10px; }
  .gcard.locked { opacity: 0.75; }
  .owner { font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .owner span { text-transform: none; color: var(--text); margin-left: 6px; }
  .gname { font-size: 22px; font-weight: 700; }
  .gcard :global(.bar) { max-width: 180px; }
  .greason { font-size: 16px; color: var(--text-2); line-height: 1.3; }
  .status { font-size: 13px; color: var(--muted); text-transform: uppercase; }
  .hovering { font-size: 14px; color: var(--gold); }
  .free { font-size: 16px; color: var(--muted); margin-top: 24px; }
  .lockin { display: inline-flex; }
</style>
