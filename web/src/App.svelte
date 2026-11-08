<script>
  import Live from './Live.svelte';
  import Pools from './Pools.svelte';
  import History from './History.svelte';
  import Crawl from './Crawl.svelte';
  import Prep from './Prep.svelte';
  import Settings from './Settings.svelte';
  import Team from './Team.svelte';
  import Tags from './Tags.svelte';
  import Rules from './Rules.svelte';
  import PlanBar from './PlanBar.svelte';
  import ScoutBox from './ScoutBox.svelte';
  import AppStatus from './AppStatus.svelte';

  // The top bar (docs/ui-engine-review.md, decision 3): Live · Prep · Review (History + Team) · Roster (Pools), then
  // Crawl and Settings as icons. Every page keeps its own address; a tab stays lit on the pages it groups.
  const TABS = [['live', 'Live'], ['prep', 'Prep'], ['history', 'Review'], ['pools', 'Roster']];
  const ICONS = [['crawl', 'Crawl: data jobs'], ['settings', 'Settings']];
  const PAGES = ['live', 'prep', 'pools', 'history', 'team', 'crawl', 'settings', 'tags', 'champions', 'rules'];
  const GROUP = { team: 'history', tags: 'settings', champions: 'settings', rules: 'settings' };
  const navKey = (p) => GROUP[p] ?? p;
  const pageFromHash = () => {
    const key = location.hash.slice(2);
    if (key === 'synergy') return 'prep'; // the Synergy page is part of Prep now (docs/synergy-v2.md)
    if (key === 'roster') return 'pools';
    if (key === 'review') return 'history';
    return PAGES.includes(key) ? key : 'live';
  };
  let page = $state(pageFromHash());
  window.addEventListener('hashchange', () => (page = pageFromHash()));

  let view = $state({ status: 'waiting', message: 'Connecting to the assistant…' });
  let connected = $state(false);
  let receivedAt = $state(performance.now());
  let now = $state(performance.now());

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
    ws.onopen = () => (connected = true);
    ws.onmessage = (e) => {
      view = JSON.parse(e.data);
      receivedAt = performance.now();
    };
    ws.onclose = () => {
      connected = false;
      setTimeout(connect, 1000);
    };
  }

  connect();
  setInterval(() => (now = performance.now()), 200);

  const secondsLeft = $derived(
    view.seconds_left == null ? null : Math.max(0, view.seconds_left - (now - receivedAt) / 1000),
  );
</script>

<nav>
  {#each TABS as [key, label]}
    <a href={`#/${key}`} class:active={navKey(page) === key}>{label}</a>
  {/each}
  {#if view.status === 'champ_select' && page !== 'live'}
    <a href="#/live" class="alert">Champ select live →</a>
  {/if}
  <span class="icons" class:pushed={!(view.status === 'champ_select' && page !== 'live')}>
    {#each ICONS as [key, label]}
      <a href={`#/${key}`} class="icon" class:active={navKey(page) === key} title={label} aria-label={label}>
        {#if key === 'crawl'}
          <svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true"><ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>
        {:else}
          <svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true"><circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2.8v2.6M12 18.6v2.6M21.2 12h-2.6M5.4 12H2.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8M18.5 18.5l-1.8-1.8M7.3 7.3 5.5 5.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="12" r="6.3" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>
        {/if}
      </a>
    {/each}
  </span>
</nav>
{#if page === 'history' || page === 'team'}
  <div class="subnav">
    <a href="#/history" class:active={page === 'history'}>History</a>
    <a href="#/team" class:active={page === 'team'}>Team trends</a>
  </div>
{/if}

{#if !connected}
  <div class="banner offline">Reconnecting to the assistant…</div>
{/if}
<AppStatus inDraft={view.status === 'champ_select'} />
{#if view.note}
  <div class="banner fixture">{view.note}</div>
{/if}

{#if page === 'pools'}
  <Pools />
{:else if page === 'prep'}
  <Prep />
{:else if page === 'tags' || page === 'champions'}
  <Tags />
{:else if page === 'rules'}
  <Rules />
{:else if page === 'settings'}
  <Settings />
{:else if page === 'team'}
  <Team />
{:else if page === 'crawl'}
  <Crawl />
{:else if page === 'history'}
  <History />
{:else if view.status === 'champ_select'}
  <Live {view} {secondsLeft} />
{:else}
  <main class="idle">
    <h1>{view.status === 'error' ? 'Something went wrong' : view.message}</h1>
    {#if view.status === 'error'}<p class="error">{view.message}</p>{/if}
    {#if view.client}<p>League client: {view.client}</p>{/if}
    {#if page === 'live' && view.status === 'waiting'}<div class="scoutwrap"><ScoutBox /></div>{/if}
  </main>
{/if}

{#if page === 'live' && view.plan}
  {#key view.plan.text}<PlanBar bar={view.plan} />{/key}
{/if}

<style>
  /* the nav runs on from the window's title bar (same colour), so it gets no top border of its own */
  nav { display: flex; gap: 2px; padding: 4px 16px 0; background: var(--nav); border-bottom: 1px solid var(--line);
        position: sticky; top: 0; z-index: 10; }
  nav a { position: relative; padding: 9px 14px 11px; color: var(--muted); text-decoration: none; font-size: 14.5px;
          border-radius: 6px 6px 0 0; }
  nav a:hover { color: var(--text); background: rgba(255, 255, 255, 0.04); }
  nav a.active { color: var(--text); font-weight: 600; }
  nav a.active::after { content: ""; position: absolute; left: 14px; right: 14px; bottom: -1px; height: 3px;
                        border-radius: 3px 3px 0 0; background: var(--gold); }
  nav a.alert { margin-left: auto; color: var(--gold); }
  .icons { display: flex; gap: 2px; }
  .icons.pushed { margin-left: auto; }
  nav a.icon { display: flex; align-items: center; padding: 9px 11px 11px; }
  nav a.icon.active::after { left: 8px; right: 8px; }
  .subnav { display: flex; gap: 4px; padding: 8px 24px 0; }
  .subnav a { color: var(--muted); text-decoration: none; font-size: 14px; padding: 5px 12px; border-radius: 6px; }
  .subnav a:hover { color: var(--text); }
  .subnav a.active { color: var(--text); background: var(--panel-2); font-weight: 600; }
  .banner { padding: 6px 16px; font-size: 14px; text-align: center; }
  .offline { background: #6e2a2a; }
  .fixture { background: #3d3214; color: var(--gold); }
  .idle { display: grid; place-items: center; align-content: center; min-height: 80vh; gap: 4px; color: var(--muted); }
  .idle h1 { color: var(--text); font-size: 40px; font-weight: 600; margin: 0; }
  .idle p { font-size: 20px; margin: 4px; }
  .scoutwrap { margin-top: 28px; display: flex; justify-content: center; width: 100%; }
  .error { color: var(--red); }
</style>
