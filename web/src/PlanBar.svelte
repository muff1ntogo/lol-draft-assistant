<script>
  // The pinned plan, fixed at the bottom of the Live screen (docs/synergy-v2.md, Stage 6). Each planned champion
  // with its status against the live draft (picked / replaced / banned / taken / at risk), the plan's bans and our
  // off-plan bans, and the comp of the plan vs the comp now (our picks + the plan's remaining champions or backups):
  // damage split, timing (Early / Mix / Late) and fight style. Status is always written out, never colour alone.
  import Champ from './Champ.svelte';

  let { bar } = $props();
  let hidden = $state(false);
  const STATUS = { planned: 'planned', picked: 'picked ✓', replaced: 'replaced', banned: 'banned', taken: 'taken by them',
                   at_risk: 'at risk' };
  const BAN = { ours: 'banned by us', theirs: 'banned by them', picked: 'picked', open: 'not banned yet' };
  const pct = (x) => `${Math.round(x * 100)}%`;
  async function unpin() {
    await fetch('/api/plan/pin', { method: 'DELETE' });
    hidden = true; // at once here; the server re-publishes the Live screen without the plan
  }
</script>

{#if !hidden}
  <footer class="planbar {bar.state}" aria-label="Pinned plan">
    <div class="head">
      <span class="t">Plan</span>
 <span class="state {bar.state}">{bar.label}</span>
      <button class="unpin" onclick={unpin} title="Stop using this plan">Unpin</button>
    </div>

    <div class="slots">
      {#each bar.slots as s}
        <div class="slot {s.status}" title="{s.role_label}: {s.planned.name} — {STATUS[s.status]}{s.backup ? ` · backup ${s.backup.name}` : ''}">
          <span class="ic">
            <Champ champ={s.planned} size={38} faded={s.status === 'replaced'} banned={s.status === 'banned' || s.status === 'taken'} />
            {#if s.actual}<span class="actual"><Champ champ={s.actual} size={22} /></span>{/if}
          </span>
          <span class="txt">
            <span class="name">{s.planned.name}</span>
            <span class="st">{`${s.role_label} · ${STATUS[s.status]}`}{#if s.actual}{` → ${s.actual.name}`}{/if}{#if (s.status === 'banned' || s.status === 'taken') && s.backup}{` → backup ${s.backup.name}`}{/if}</span>
          </span>
        </div>
      {/each}
    </div>

    {#if bar.bans}
      <div class="bans">
        <span class="t">Bans</span>
        <span class="row">
          {#each bar.bans.plan as b}
            <span class="ban {b.status}" title="{b.champ.name}: {BAN[b.status]}"><Champ champ={b.champ} size={26} banned={b.status !== 'open'} /></span>
          {/each}
        </span>
        {#if bar.bans.off_plan.length}
          <span class="row off" title="Our bans that weren't in the plan">
            <span class="t">off plan</span>
            {#each bar.bans.off_plan as c}<span title="{c.name}: banned by us, not in the plan"><Champ champ={c} size={22} banned /></span>{/each}
          </span>
        {/if}
      </div>
    {/if}

    <div class="summary">
      {#each [['Plan', bar.summary.plan], ['Now', bar.summary.now]] as [label, c]}
        {#if c}
          <div class="srow">
            <span class="t">{label}</span>
            {#if c.damage}
              <span class="dmg" title="Physical {pct(c.damage.physical)} · Magic {pct(c.damage.magic)} · True {pct(c.damage.true)}">
                <span class="phys" style="flex-grow:{c.damage.physical}"></span><span class="magic" style="flex-grow:{c.damage.magic}"></span><span class="true" style="flex-grow:{c.damage.true}"></span>
              </span>
              <span class="small">{pct(c.damage.physical)} AD · {pct(c.damage.magic)} AP</span>
            {:else}<span class="small muted">no damage data</span>{/if}
            <span class="lbl timing {c.timing ?? ''}" title="Comp timing from power curves">{c.timing ? c.timing_label : '—'}</span>
            <span class="lbl" class:muted={c.style === 'mixed'} title="Fight style">{c.style_label}</span>
          </div>
        {/if}
      {/each}
    </div>
  </footer>
  <div class="spacer"></div>
{/if}

<style>
 .planbar { position: fixed; left: var(--rail-w-collapsed); right: 0; bottom: 0; z-index: 20; display: flex; align-items: center; gap: 18px;
 padding: 4px 20px; min-height: 56px; background: var(--rail); border-top: 1px solid var(--hairline-strong); overflow-x: auto; }
 /* one 56px bar under a hairline; the plan's state is written in the bar, not painted on its edge */
 .spacer { height: 60px; }
 .t { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; }
  .head { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; flex: none; }
 .state { font-size: 12px; font-weight: 600; color: var(--text-2); white-space: nowrap; }
 /* a status is a word, so it takes the status colours (never the favourability or side ones) */
 .state.at_risk { color: var(--warn); }
 .state.broken { color: var(--error); }
 .unpin { font: inherit; font-size: 12px; background: none; color: var(--text-3); border: 0; padding: 0; cursor: pointer; }
  .slots { display: flex; gap: 14px; flex: none; }
  .slot { display: flex; align-items: center; gap: 7px; }
  .ic { position: relative; flex: none; }
 .actual { position: absolute; right: -8px; bottom: -6px; border: 2px solid var(--rail); border-radius: 6px; line-height: 0; }
  .txt { display: flex; flex-direction: column; line-height: 1.15; }
  .name { font-size: 14px; font-weight: 600; }
 .st { font-size: 11px; color: var(--text-3); white-space: nowrap; }
  .slot.picked .st { color: var(--text-1); }
  .slot.replaced .st, .slot.at_risk .st { color: var(--warn); }
  .slot.banned .st, .slot.taken .st { color: var(--error); }
  .bans { display: flex; align-items: center; gap: 8px; flex: none; }
  .row { display: inline-flex; gap: 3px; align-items: center; }
  .ban { border-radius: 6px; outline: 2px solid transparent; outline-offset: 1px; line-height: 0; }
  /* a planned ban we made: marked in light ink (a state), and said in its title */
  .ban.ours { outline-color: var(--text-2); }
  .row.off { margin-left: 8px; }
  .row.off .t { margin-right: 4px; }
  .summary { display: flex; flex-direction: column; gap: 3px; margin-left: auto; flex: none; }
  .srow { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .srow .t { width: 34px; }
  .dmg { display: flex; width: 110px; height: 8px; border-radius: 3px; overflow: hidden; gap: 1px; }
 .phys { background: var(--viz-matchup); }
 .magic { background: var(--viz-threat); }
 .true { background: var(--viz-comp); }
  .lbl { font-weight: 700; min-width: 44px; }
  .small { font-size: 12px; color: var(--text-2); }
 .muted { color: var(--text-3); font-weight: 500; }
</style>
