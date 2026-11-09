<script>
  // Live (and what it shows) is in the main bundle so champ select never waits for a download; every other page is its
  // own chunk, fetched when first opened, when its tab is hovered, or once the app is idle.
  import Live from './Live.svelte';
  import PlanBar from './PlanBar.svelte';
  import ScoutBox from './ScoutBox.svelte';
  import AppStatus from './AppStatus.svelte';
  import { DUR, CSS_EASE, receiveShared, reducedMotion, reveal, rise, sendShared } from './motion.js';
  import { settled } from './loading.js';

  // The top bar (docs/ui-engine-review.md, decision 3): Live · Prep · Review (History + Team) · Roster (Pools), then
  // Crawl and Settings as icons. Every page keeps its own address; a tab stays lit on the pages it groups.
  const TABS = [['live', 'Live'], ['prep', 'Prep'], ['history', 'Review'], ['pools', 'Roster']];
  const ICONS = [['crawl', 'Crawl: data jobs'], ['settings', 'Settings']];
  const PAGES = ['live', 'prep', 'pools', 'history', 'team', 'crawl', 'settings', 'tags', 'champions', 'rules'];
  const GROUP = { team: 'history', tags: 'settings', champions: 'settings', rules: 'settings' };
  const navKey = (p) => GROUP[p] ?? p;
  const LOADERS = {
    pools: () => import('./Pools.svelte'), prep: () => import('./Prep.svelte'), history: () => import('./History.svelte'),
    team: () => import('./Team.svelte'), crawl: () => import('./Crawl.svelte'), settings: () => import('./Settings.svelte'),
    tags: () => import('./Tags.svelte'), champions: () => import('./Tags.svelte'), rules: () => import('./Rules.svelte'),
  };
  const loaded = {}; // page -> promise of its component (each chunk is fetched once)
  function loadPage(key) {
    if (!LOADERS[key]) return Promise.resolve(null);
    loaded[key] ??= LOADERS[key]().then((m) => m.default).catch((err) => { delete loaded[key]; throw err; });
    return loaded[key];
  }
  // warm the rest once the first page is up, so later switches only wait for their data
  function preloadAll() {
    const idle = window.requestIdleCallback ?? ((fn) => setTimeout(fn, 300));
    idle(() => Object.keys(LOADERS).forEach((k) => loadPage(k).catch(() => {})));
  }
  const pageFromHash = () => {
    const key = location.hash.slice(2);
    if (key === 'synergy') return 'prep'; // the Synergy page is part of Prep now (docs/synergy-v2.md)
    if (key === 'roster') return 'pools';
    if (key === 'review') return 'history';
    return PAGES.includes(key) ? key : 'live';
  };
  let page = $state(pageFromHash());

  // The pages on stage (loading.js): the one showing, and a new one mounted out of sight until its first requests
  // have answered. Then the new page rises in as the old one leaves, so it appears whole, in one movement.
  let nextId = 0;
  let stack = $state([]);
  // One page at a time: the old page fades to blank first (.leaving), while the new one loads out of sight; the new
  // page rises in once both are done. Loading time hides behind the fade instead of the two pages overlapping.
  async function show(key) {
    for (const e of stack) e.leaving = true;
    const faded = new Promise((r) => setTimeout(r, stack.length && !reducedMotion() ? DUR.exitPage : 0));
    let Comp = null;
    try { Comp = await loadPage(key); } catch { /* a failed chunk: the page shows its error below */ }
    if (key !== page) return; // the user moved on while it downloaded
    const entry = { id: nextId++, key, Comp, ready: false, leaving: false };
    const since = performance.now();
    stack.push(entry);
    await Promise.all([settled(since, () => document.querySelector(`.stage > .page[data-entry="${entry.id}"]`)), faded]);
    const i = stack.findIndex((e) => e.id === entry.id);
    if (i < 0 || entry.id !== stack[stack.length - 1].id) return; // a later page already replaced it
    stack = stack.slice(i); // the old pages are blank by now: drop them
    stack[0].ready = true;
    window.scrollTo(0, 0);
  }
  show(pageFromHash()).then(preloadAll);
  window.addEventListener('hashchange', () => {
    const next = pageFromHash();
    if (next === page) return;
    page = next; // the nav answers at once; the page follows when it's ready
    show(next);
  });
  // the entrance, run when a page waiting out of sight is ready (a WAAPI animation, as the element already exists)
  function enter(node, ready) {
    let was = ready;
    return {
      update(now) {
        if (now && !was && !reducedMotion() && node.animate) {
          node.animate([{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }],
                       { duration: DUR.enter, easing: CSS_EASE.out });
        }
        was = now;
      },
    };
  }

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
    <a href={`#/${key}`} class:active={navKey(page) === key} onpointerenter={() => loadPage(key).catch(() => {})}>{label}{#if navKey(page) === key}{@render underline()}{/if}</a>
  {/each}
  {#if view.status === 'champ_select' && page !== 'live'}
    <a href="#/live" class="alert" in:rise={{ y: -4 }}>Champ select live →</a>
  {/if}
  <span class="icons" class:pushed={!(view.status === 'champ_select' && page !== 'live')}>
    {#each ICONS as [key, label]}
      <a href={`#/${key}`} class="icon" class:active={navKey(page) === key} title={label} aria-label={label}
         onpointerenter={() => loadPage(key).catch(() => {})}>
        {#if key === 'crawl'}
          <svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true"><ellipse cx="12" cy="5.5" rx="7.5" ry="2.8" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>
        {:else}
          <svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true"><circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 2.8v2.6M12 18.6v2.6M21.2 12h-2.6M5.4 12H2.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8M18.5 18.5l-1.8-1.8M7.3 7.3 5.5 5.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="12" r="6.3" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>
        {/if}
        {#if navKey(page) === key}{@render underline()}{/if}
      </a>
    {/each}
  </span>
</nav>
<!-- One underline, one key: it slides from the old tab to the new one instead of blinking out and in -->
{#snippet underline()}<span class="underline" in:receiveShared={{ key: 'nav' }} out:sendShared={{ key: 'nav' }}></span>{/snippet}
{#if page === 'history' || page === 'team'}
  <div class="subnav" transition:reveal>
    {#each [['history', 'History'], ['team', 'Team trends']] as [key, label]}
      <a href={`#/${key}`} class:active={page === key}>
        {#if page === key}<span class="pill" in:receiveShared={{ key: 'subnav' }} out:sendShared={{ key: 'subnav' }}></span>{/if}
        <span class="label">{label}</span>
      </a>
    {/each}
  </div>
{/if}

{#if !connected}
  <div class="banner offline" transition:reveal>Reconnecting to the assistant…</div>
{/if}
<AppStatus inDraft={view.status === 'champ_select'} />
{#if view.note}
  <div class="banner fixture">{view.note}</div>
{/if}

<!-- Pages share one grid cell, so the new page comes in while the old one leaves -->
{#snippet pageView(entry)}
{#if entry.Comp}
  <entry.Comp />
{:else if LOADERS[entry.key]}
  <main class="idle"><h1>Couldn't load this page</h1><p class="error">Check the connection to the app, then reload.</p></main>
{:else if view.status === 'champ_select'}
  <Live {view} {secondsLeft} />
{:else}
  <main class="idle">
    <h1>{view.status === 'error' ? 'Something went wrong' : view.message}</h1>
    {#if view.status === 'error'}<p class="error">{view.message}</p>{/if}
    {#if view.client}<p>League client: {view.client}</p>{/if}
    {#if entry.key === 'live' && view.status === 'waiting'}<div class="scoutwrap"><ScoutBox /></div>{/if}
  </main>
{/if}
{/snippet}
<div class="stage">
  {#each stack as entry (entry.id)}
    <div class="page" data-page={entry.key} data-entry={entry.id} class:waiting={!entry.ready} class:leaving={entry.leaving}
         inert={!entry.ready || entry.leaving} aria-hidden={!entry.ready || entry.leaving} use:enter={entry.ready}>
      {@render pageView(entry)}
    </div>
  {/each}
</div>

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
  .underline { position: absolute; left: 14px; right: 14px; bottom: -1px; height: 3px;
               border-radius: 3px 3px 0 0; background: var(--gold); }
  nav a.alert { margin-left: auto; color: var(--gold); }
  .icons { display: flex; gap: 2px; }
  .icons.pushed { margin-left: auto; }
  nav a.icon { display: flex; align-items: center; padding: 9px 11px 11px; }
  nav a.icon .underline { left: 8px; right: 8px; }
  .subnav { display: flex; gap: 4px; padding: 8px 24px 0; }
  .subnav a { position: relative; color: var(--muted); text-decoration: none; font-size: 14px; padding: 5px 12px; border-radius: 6px; }
  .subnav a:hover { color: var(--text); }
  .subnav a.active { color: var(--text); font-weight: 600; }
  .subnav .pill { position: absolute; inset: 0; border-radius: 6px; background: var(--panel-2); }
  .subnav .label { position: relative; }
  .stage { display: grid; }
  .stage > .page { grid-area: 1 / 1; min-width: 0; }
  .stage > .page.waiting { opacity: 0; pointer-events: none; }
  .stage > .page.leaving { opacity: 0; pointer-events: none; transition: opacity var(--dur-page-exit) var(--ease-in); }
  .banner { padding: 6px 16px; font-size: 14px; text-align: center; }
  .offline { background: #6e2a2a; }
  .fixture { background: var(--gold-bg); color: var(--gold); }
  .idle { display: grid; place-items: center; align-content: center; min-height: 80vh; gap: 4px; color: var(--muted); }
  .idle h1 { color: var(--text); font-size: 40px; font-weight: 600; margin: 0; }
  .idle p { font-size: 20px; margin: 4px; }
  .scoutwrap { margin-top: 28px; display: flex; justify-content: center; width: 100%; }
  .error { color: var(--red); }
</style>
