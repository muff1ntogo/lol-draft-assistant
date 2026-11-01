<script>
  import Live from './Live.svelte';
  import Pools from './Pools.svelte';
  import History from './History.svelte';
  import Prep from './Prep.svelte';
  import Settings from './Settings.svelte';

  const PAGES = { live: 'Live', prep: 'Prep', pools: 'Pools', history: 'History', settings: 'Settings' };
  const pageFromHash = () => (location.hash.slice(2) in PAGES ? location.hash.slice(2) : 'live');
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
  {#each Object.entries(PAGES) as [key, label]}
    <a href={`#/${key}`} class:active={page === key}>{label}</a>
  {/each}
  {#if view.status === 'champ_select' && page !== 'live'}
    <a href="#/live" class="alert">Champ select live →</a>
  {/if}
</nav>

{#if !connected}
  <div class="banner offline">Reconnecting to the assistant…</div>
{/if}
{#if view.note}
  <div class="banner fixture">{view.note}</div>
{/if}

{#if page === 'pools'}
  <Pools />
{:else if page === 'prep'}
  <Prep />
{:else if page === 'settings'}
  <Settings />
{:else if page === 'history'}
  <History />
{:else if view.status === 'champ_select'}
  <Live {view} {secondsLeft} />
{:else}
  <main class="idle">
    <h1>{view.status === 'error' ? 'Something went wrong' : view.message}</h1>
    {#if view.status === 'error'}<p class="error">{view.message}</p>{/if}
    {#if view.client}<p>League client: {view.client}</p>{/if}
  </main>
{/if}

<style>
  nav { display: flex; gap: 4px; padding: 0 20px; background: #090c10; border-bottom: 1px solid var(--line); }
  nav a { padding: 10px 16px; color: var(--muted); text-decoration: none; font-size: 15px; border-bottom: 2px solid transparent; }
  nav a.active { color: var(--text); border-color: var(--gold); }
  nav a.alert { margin-left: auto; color: var(--gold); }
  .banner { padding: 6px 16px; font-size: 14px; text-align: center; }
  .offline { background: #6e2a2a; }
  .fixture { background: #3d3214; color: var(--gold); }
  .idle { display: grid; place-items: center; align-content: center; min-height: 80vh; gap: 4px; color: var(--muted); }
  .idle h1 { color: var(--text); font-size: 40px; font-weight: 600; margin: 0; }
  .idle p { font-size: 20px; margin: 4px; }
  .error { color: var(--red); }
</style>
