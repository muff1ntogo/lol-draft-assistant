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
      <span class="pill {bar.state}">{bar.label}</span>
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
  .planbar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 20; display: flex; align-items: center; gap: 22px;
             padding: 6px 20px; min-height: 64px; background: #0f1a26; border-top: 2px solid #2f6fb7; }
  .planbar.at_risk { border-top-color: #c98500; background: #1d1810; }
  .planbar.broken { border-top-color: #c0423a; background: #1f1312; }
  .spacer { height: 76px; }
  .t { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .head { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; flex: none; }
  .pill { font-size: 13px; font-weight: 700; padding: 0 6px; border-radius: 4px; background: #12304d; color: #8ec5ff; }
  .pill.at_risk { background: #3d2e0a; color: #f0c674; }
  .pill.broken { background: #4a1f1c; color: #ffb4ad; }
  .unpin { font: inherit; font-size: 12px; background: none; color: var(--muted); border: 1px solid var(--line);
           border-radius: 5px; padding: 0 8px; cursor: pointer; }
  .slots { display: flex; gap: 14px; flex: none; }
  .slot { display: flex; align-items: center; gap: 7px; }
  .ic { position: relative; flex: none; }
  .actual { position: absolute; right: -8px; bottom: -6px; border: 2px solid #0f1a26; border-radius: 6px; line-height: 0; }
  .txt { display: flex; flex-direction: column; line-height: 1.15; }
  .name { font-size: 14px; font-weight: 600; }
  .st { font-size: 12px; color: var(--muted); }
  .slot.picked .st { color: #4fd1a1; }
  .slot.replaced .st, .slot.at_risk .st { color: #e0a640; }
  .slot.banned .st, .slot.taken .st { color: #ff9b94; }
  .bans { display: flex; flex-direction: column; gap: 3px; flex: none; }
  .row { display: inline-flex; gap: 3px; align-items: center; }
  .ban { border-radius: 6px; outline: 2px solid transparent; outline-offset: 1px; line-height: 0; }
  .ban.ours { outline-color: #1baf7a; }
  .row.off .t { margin-right: 4px; }
  .summary { display: flex; flex-direction: column; gap: 3px; margin-left: auto; flex: none; }
  .srow { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .srow .t { width: 34px; }
  .dmg { display: flex; width: 110px; height: 8px; border-radius: 3px; overflow: hidden; gap: 1px; }
  .phys { background: #d95926; }
  .magic { background: #3987e5; }
  .true { background: #199e70; }
  .lbl { font-weight: 700; min-width: 44px; }
  .timing.early { color: #e0a640; }
  .timing.late { color: #79b8ff; }
  .small { font-size: 12px; color: #c9d1d9; }
  .muted { color: var(--muted); font-weight: 500; }
</style>
