<script>
  // docs/enemy-scouting.md, Stage C: paste the opponents' Riot IDs when the client doesn't give them. The client's names
  // win when it has them; pasted ones are forgotten when the game ends.
  import Champ from './Champ.svelte';

  let { compact = false } = $props();
  let text = $state('');
  let info = $state(null);
  let message = $state('');
  let timer = null;

  async function load() {
    try {
      info = await (await fetch('/api/scout')).json();
    } catch {
      return;
    }
    clearTimeout(timer);
    if (info?.running) timer = setTimeout(load, 2000);  // poll while the pools fill in
  }
  load();
  $effect(() => () => clearTimeout(timer));

  async function scout() {
    message = '';
    const res = await fetch('/api/scout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
    });
    const body = await res.json();
    if (!res.ok) { message = body.detail; return; }
    info = body;
    text = '';
    load();
  }

  async function clear() {
    info = await (await fetch('/api/scout', { method: 'DELETE' })).json();
  }

  const STATUS = { new: 'waiting', mastery: 'mastery only', games: 'reading games', done: '', missing: 'no such account',
                   error: 'failed' };
  const progress = (p) => (p.status === 'games' ? ` ${p.games_read}/${p.games_wanted}` : '');
</script>

<section class="scout" class:compact>
 {#snippet body()}
 <div class="body">
 {#if info?.source === 'client'}
 <p class="note">The client shows their names: scouting them automatically.</p>
 {:else}
 <form onsubmit={(e) => { e.preventDefault(); scout(); }}>
 <textarea bind:value={text} rows={compact ? 3 : 5}
 placeholder={'Name#TAG, one per line (or paste an OP.GG multi-search)'}></textarea>
 <div class="actions">
 <button type="submit" disabled={!text.trim()}>Scout</button>
 {#if info?.source === 'manual'}<button type="button" class="link" onclick={clear}>Clear</button>{/if}
 </div>
 </form>
 {/if}
 {#if message}<p class="error">{message}</p>{/if}
 {#if info && !info.available}<p class="note">Needs a Riot API key (.env) to fetch anything.</p>{/if}
 {#if info?.error}<p class="error">{info.error}</p>{/if}
 {#if info?.players?.length}
 <ul>
 {#each info.players as p}
 <li>
 <span class="who">{p.name}{#if p.role}<small>{p.role}</small>{/if}</span>
 <span class="champs">
 {#each p.top as t}
 <span class="champ" title={t.games ? `${t.games} games, ${Math.round((100 * t.wins) / t.games)}%` : `${t.points?.toLocaleString()} mastery`}>
 <Champ champ={t.champ} size={24} />{#if t.games}<small>{t.games}</small>{/if}
 </span>
 {/each}
 </span>
 <span class="status">{STATUS[p.status] ?? p.status}{progress(p)}</span>
 </li>
 {/each}
 </ul>
 {/if}
 </div>
 {/snippet}
 {#if compact}<details>
 <summary>Scout opponents</summary>
 {@render body()}
 </details>
 {:else}
 <header><h3>Scout opponents</h3></header>
 {@render body()}
 {/if}
</section>

<style>
 .scout { border-top: 1px solid var(--hairline); padding: 12px 0 0; display: grid; gap: 8px; text-align: left; width: min(520px, 100%); }
 .body { display: grid; gap: 8px; margin-top: 8px; }
 details summary { cursor: pointer; color: var(--text-1); font-size: 13px; font-weight: 600; }
 details summary::marker { color: var(--text-3); }
  header { display: flex; justify-content: space-between; align-items: baseline; }
  h3 { margin: 0; font-size: 16px; }
 textarea { width: 100%; box-sizing: border-box; background: var(--surface-2); color: var(--text-1); border: 1px solid var(--hairline);
 padding: 6px 8px; font: inherit; font-size: 13px; resize: vertical; }
  .actions { display: flex; gap: 10px; margin-top: 6px; }
 button { background: var(--signal); color: var(--signal-ink); font-weight: 600; border: 0; padding: 5px 14px; font: inherit; font-size: 13px;
           cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
 button.link { background: none; color: var(--text-3); padding: 0; }
 .note { margin: 0; font-size: 12px; color: var(--text-3); }
  .error { margin: 0; font-size: 13px; color: var(--down-text); }
  ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
  li { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 10px; align-items: center; font-size: 14px; }
  .who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
 .who small { color: var(--text-3); margin-left: 6px; text-transform: uppercase; font-size: 11px; }
  .champs { display: flex; gap: 6px; }
  .champ { display: inline-flex; align-items: center; gap: 2px; }
 .champ small { font-size: 11px; color: var(--text-3); }
 .status { font-size: 11px; color: var(--text-3); min-width: 5em; text-align: right; }
</style>
