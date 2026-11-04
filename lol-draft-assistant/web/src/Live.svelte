<script>
  import Champ from './Champ.svelte';
  import ComponentBar from './ComponentBar.svelte';
  import ComponentLegend from './ComponentLegend.svelte';
  import CompPanel from './CompPanel.svelte';

  let { view, secondsLeft } = $props();
  const center = $derived(view.center);
  const pct = (p) => (p < 0.005 ? '<1%' : `${Math.round(p * 100)}%`);
  const pp = (x) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x).toFixed(1)}pp`;
</script>

<header class="top {view.side}">
  <span class="side">{view.side.toUpperCase()} SIDE</span>
  <span class="phase">{view.phase}</span>
  <span class="turn">
    {#if view.turn === 'us'}OUR {view.action?.toUpperCase()}{:else if view.turn === 'them'}THEIR {view.action?.toUpperCase()}{/if}
  </span>
  <span class="timer">{secondsLeft == null ? '' : Math.ceil(secondsLeft)}</span>
</header>

<main class="board">
  <section class="team ours">
    {#each view.our_slots as slot}
      <div class="slot" class:local={slot.is_local}>
        <span class="role">{slot.role_label}</span>
        {#if slot.champ}
          <Champ champ={slot.champ} size={56} />
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

  <section class="center">
    {#if center.mode === 'our_ban_group'}
      <div class="grouphead"><h2>{center.title}</h2><ComponentLegend /></div>
      <div class="group">
        {#each center.cards as card}
          <article class="gcard {card.status}">
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
        <article class="card" class:highlighted={card.highlighted} class:compact={center.cards.length > 3}>
          <span class="rank">{i + 1}</span>
          <Champ champ={card.champ} size={center.cards.length > 3 ? 60 : 88} />
          <div>
            <div class="title">{card.champ.name}{#if card.role_label} <small>{card.role_label}</small>{/if}
              {#if card.plan}<span class="plantag">plan</span>{/if}
              {#if card.badge?.text}<span class="badge">{card.badge.text}</span>{/if}</div>
            <div class="reason">{card.reason}{#if card.effect?.length}<span class="effect">{` → ${card.effect.join(', ')}`}</span>{/if}</div>
            {#if card.components}<ComponentBar components={card.components} />{/if}
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
            <div class="title">Hover: {center.hover_card.champ.name}</div>
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
      {#each center.cards as card}
        <article class="card enemy">
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
      <div class="winprob">{Math.round(center.win_prob * 100)}%</div>
      <p class="muted">model win probability</p>
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
      <div class="slot">
        {#if slot.champ}
          <Champ champ={slot.champ} size={56} />
          <span class="name">{slot.champ.name}{#if slot.badge?.text}<small class="badge">{slot.badge.text}</small>{/if}</span>
        {:else}
          <span class="empty"></span>
        {/if}
        <span class="role">{slot.role_label}</span>
      </div>
    {/each}
    <div class="bans">
      {#each view.their_bans as ban}<Champ champ={ban} size={36} banned />{/each}
    </div>
    {#if view.comp}<CompPanel comp={view.comp.theirs} title="Their comp" />{/if}
  </section>
</main>

<style>
  .checkstrip { display: flex; gap: 14px; margin: 0 0 8px; font-size: 14px; color: var(--muted); }
  .checkstrip .no { color: #e0a640; font-weight: 600; }
  .plantag { font-size: 13px; font-weight: 600; color: #8ec5ff; border: 1px solid #2f6fb7; border-radius: 4px;
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
  .syn.pos { color: #4fd1a1; }
  .syn.neg { color: #e0a640; }
  .card.highlighted { border-color: var(--gold); background: var(--panel-2); }
  .card.compact { gap: 14px; padding: 8px 14px; }
  .card.compact .title { font-size: 23px; }
  .card.compact .reason { font-size: 17px; margin-top: 2px; }
  .card.compact .rank { font-size: 22px; }
  .badge { font-size: 13px; font-weight: 500; color: #c9d1d9; background: #2d3540; border-radius: 4px;
           padding: 1px 6px; margin-left: 8px; white-space: nowrap; vertical-align: middle; }
  .name .badge { margin: 2px 0 0; align-self: flex-start; font-size: 12px; }
  .effect { color: var(--gold); }
  .hovercard { opacity: 0.9; }
  .rank { font-size: 28px; font-weight: 700; color: var(--muted); width: 1.2em; }
  .title { font-size: 30px; font-weight: 700; }
  .title small { font-size: 18px; color: var(--muted); font-weight: 500; margin-left: 10px; }
  .reason { font-size: 22px; color: #c9d1d9; margin-top: 4px; }
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
  .greason { font-size: 16px; color: #c9d1d9; line-height: 1.3; }
  .status { font-size: 13px; color: var(--muted); text-transform: uppercase; }
  .hovering { font-size: 14px; color: var(--gold); }
  .free { font-size: 16px; color: var(--muted); margin-top: 24px; }
</style>
