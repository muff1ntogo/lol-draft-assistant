<script>
  import Champ from './Champ.svelte';

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
          <span class="name">{slot.champ.name}</span>
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
  </section>

  <section class="center">
    {#if center.mode === 'our_ban' || center.mode === 'our_pick'}
      <h2>{center.title}</h2>
      {#each center.cards as card, i}
        <article class="card" class:highlighted={card.highlighted}>
          <span class="rank">{i + 1}</span>
          <Champ champ={card.champ} size={88} />
          <div>
            <div class="title">{card.champ.name}{#if card.role_label} <small>{card.role_label}</small>{/if}</div>
            <div class="reason">{card.reason}</div>
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
    {:else if center.mode === 'enemy_turn'}
      <h2>{center.title}</h2>
      <p class="muted">Their most likely picks</p>
      {#each center.cards as card}
        <article class="card enemy">
          <Champ champ={card.champ} size={72} />
          <div>
            <div class="title">{card.champ.name} <small>{pct(card.prob)} · {card.role_label}</small></div>
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
          <span class="name">{slot.champ.name}</span>
        {:else}
          <span class="empty"></span>
        {/if}
        <span class="role">{slot.role_label}</span>
      </div>
    {/each}
    <div class="bans">
      {#each view.their_bans as ban}<Champ champ={ban} size={36} banned />{/each}
    </div>
  </section>
</main>

<style>
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
  .card.highlighted { border-color: var(--gold); background: var(--panel-2); }
  .hovercard { opacity: 0.9; }
  .rank { font-size: 28px; font-weight: 700; color: var(--muted); width: 1.2em; }
  .title { font-size: 30px; font-weight: 700; }
  .title small { font-size: 18px; color: var(--muted); font-weight: 500; margin-left: 10px; }
  .reason { font-size: 22px; color: #c9d1d9; margin-top: 4px; }
  .warn { color: var(--warn); font-size: 18px; font-style: normal; margin-top: 4px; }
  .muted { color: var(--muted); font-size: 18px; margin: 0; }
  .winprob { font-size: 96px; font-weight: 800; line-height: 1; }
</style>
