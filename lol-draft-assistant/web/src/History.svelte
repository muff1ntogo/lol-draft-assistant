<script>
  import Champ from './Champ.svelte';

  const QUEUES = { 420: 'Solo/Duo', 440: 'Flex', 700: 'Clash', 0: 'Custom' };
  let drafts = $state(null);
  let open = $state(null);

  async function load() {
    drafts = await (await fetch('/api/history')).json();
  }
  load();

  const when = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const pct = (p) => (p == null ? '—' : `${Math.round(p * 100)}%`);
  const resultLabel = { win: 'WIN', loss: 'LOSS', remake: 'REMAKE' };
</script>

<main class="page">
  <h1>History</h1>
  {#if drafts === null}
    <p class="muted">Loading…</p>
  {:else if drafts.length === 0}
    <p class="muted">No games logged yet. Drafts are logged automatically once a game starts.</p>
  {:else}
    {#each drafts as d}
      <article class="draft" class:expanded={open === d.draft_id}>
        <button class="row" onclick={() => (open = open === d.draft_id ? null : d.draft_id)}>
          <span class="result {d.result ?? 'unknown'}">{resultLabel[d.result] ?? '?'}</span>
          <span class="side {d.side}">{d.side}</span>
          <span>{when(d.played_at)}</span>
          <span class="muted">{QUEUES[d.queue_id] ?? (d.queue_id ? `queue ${d.queue_id}` : d.format)}</span>
          <span>predicted {pct(d.pred_win_prob)}</span>
          <span class="muted">
            followed our #1 {d.summary.followed_top}/{d.summary.our_turns} ·
            enemy picks foreseen {d.summary.enemy_picks_in_our_top3}/{d.summary.enemy_picks_predicted}
          </span>
          <span class="muted small">{d.match_verified ? '✓ match-v5' : d.result_source ?? ''}</span>
        </button>
        {#if open === d.draft_id}
          <table>
            <thead><tr><th>#</th><th>Team</th><th>Action</th><th>Champion</th><th>Our rank</th><th>We showed</th></tr></thead>
            <tbody>
              {#each d.actions as a}
                <tr class:ours={a.ours}>
                  <td>{a.seq}</td>
                  <td class="side {a.side}">{a.ours ? 'us' : 'them'}</td>
                  <td>{a.type}</td>
                  <td class="champ">
                    {#if a.champ}<Champ champ={a.champ} size={28} banned={a.type === 'ban'} /> {a.champ.name}{:else}<span class="muted">skipped</span>{/if}
                  </td>
                  <td>{a.our_rank ? `#${a.our_rank}` : a.shown.length ? '—' : ''}</td>
                  <td class="shown">
                    {#each a.shown as c}<Champ champ={c} size={24} />{/each}
                    {#if a.shown_kind === 'predicted'}<span class="muted small">predicted</span>{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </article>
    {/each}
  {/if}
</main>

<style>
  .page { max-width: 1100px; margin: 0 auto; padding: 20px 28px 60px; }
  h1 { font-size: 32px; margin: 8px 0 16px; }
  .draft { background: var(--panel); border-radius: 8px; margin: 8px 0; overflow: hidden; }
  .row { width: 100%; display: grid; grid-template-columns: 80px 56px 170px 90px 120px 1fr auto; gap: 12px;
         align-items: center; background: none; border: 0; color: var(--text); font: inherit; text-align: left;
         padding: 12px 16px; cursor: pointer; }
  .row:hover { background: var(--panel-2); }
  .result { font-weight: 700; }
  .result.win { color: var(--good); }
  .result.loss { color: var(--red); }
  .result.remake, .result.unknown { color: var(--muted); }
  .side { text-transform: uppercase; font-size: 13px; font-weight: 600; }
  .side.blue { color: var(--blue); }
  .side.red { color: var(--red); }
  table { width: 100%; border-collapse: collapse; margin: 0 0 8px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 13px; padding: 6px 16px; }
  td { padding: 4px 16px; border-top: 1px solid var(--line); font-size: 15px; }
  tr.ours td { background: #18202b; }
  .champ { display: flex; align-items: center; gap: 8px; }
  .shown { display: flex; gap: 4px; align-items: center; }
  .muted { color: var(--muted); }
  .small { font-size: 12px; }
</style>
