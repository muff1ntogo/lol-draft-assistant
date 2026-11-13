<script>
  // Live (and what it shows) is in the main bundle so champ select never waits for a download; every other page is its
  // own chunk, fetched when first opened, when its tab is hovered, or once the app is idle.
  import Live from './Live.svelte';
  import PlanBar from './PlanBar.svelte';
  import ScoutBox from './ScoutBox.svelte';
  import Notice from './ui/Notice.svelte';
  import { appStatus } from './appStatus.svelte.js';
  import Toast from './ui/Toast.svelte';
  import { DUR, CSS_EASE, receiveShared, reducedMotion, sendShared } from './motion.js';
  import { settled } from './loading.js';

  // The top bar (docs/ui-engine-review.md, decision 3): Live · Prep · Review (History + Team) · Roster (Pools), then
  // Crawl and Settings as icons. Every page keeps its own address; a tab stays lit on the pages it groups.
  const TABS = [['live', 'Live'], ['prep', 'Prep'], ['history', 'Review'], ['pools', 'Roster']];
  const ICONS = [['crawl', 'Crawl: data jobs'], ['settings', 'Settings']];
const PAGES = ['live', 'prep', 'pools', 'player', 'history', 'team', 'crawl', 'settings', 'tags', 'champions', 'rules'];
  const GROUP = { team: 'history', tags: 'settings', champions: 'settings', rules: 'settings', player: 'pools' };
  const navKey = (p) => GROUP[p] ?? p;
  const LOADERS = {
    pools: () => import('./Roster.svelte'), player: () => import('./Player.svelte'), prep: () => import('./Prep.svelte'), history: () => import('./History.svelte'),
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
  // "#/player/<riot id>": a page with one parameter (the rest of the address, still URL-encoded)
  const paramFromHash = () => location.hash.slice(2).split('/').slice(1).join('/');
  const pageFromHash = () => {
    const key = location.hash.slice(2).split('/')[0];
    if (key === 'synergy') return 'prep'; // the Synergy page is part of Prep now (docs/synergy-v2.md)
    if (key === 'roster') return 'pools';
    if (key === 'review') return 'history';
    return PAGES.includes(key) ? key : 'live';
  };
  let page = $state(pageFromHash());
  let param = $state(paramFromHash());

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
    const nextParam = paramFromHash();
    if (next === page && nextParam === param) return;
    const samePage = next === page;
    page = next; // the nav answers at once; the page follows when it's ready
    param = nextParam;
    if (!samePage) show(next); // the same page with another parameter (one profile to another) updates in place
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

  // Live gets the full width while a draft is on; the rail opens again on hover (CSS)
  const railFolded = $derived(page === 'live' && view.status === 'champ_select');

  // Everything the app has to tell the user, as one strip (docs/ui-redesign.md, step 2): the most urgent shows, the
  // rest wait behind a count. During a draft the scorebar stays flush to the top, so Live shows the strip itself, as
  // one line under its Draft / Strategy switch.
  const status = appStatus();
  const inDraft = $derived(view.status === 'champ_select');
  const notices = $derived([
    ...(connected ? [] : [{ id: 'offline', kind: 'offline', text: 'Reconnecting to the assistant…' }]),
    ...status.items(inDraft),
    ...(view.note ? [{ id: 'fixture', kind: 'fixture', text: view.note }] : []),
  ]);

  const secondsLeft = $derived(
    view.seconds_left == null ? null : Math.max(0, view.seconds_left - (now - receivedAt) / 1000),
  );
</script>

<!-- The left rail (docs/ui-redesign.md): the four pages people use, then Crawl and Settings at the foot. It folds to
     icons during champ select so Live gets the width, and opens again on hover. -->
<nav class="rail" class:folded={railFolded} aria-label="Pages">
  <div class="brand" aria-hidden="true"><span class="mark"></span><span class="word">Draft</span></div>
  {#each TABS as [key, label]}
    <a href={`#/${key}`} class:active={navKey(page) === key} aria-current={navKey(page) === key ? 'page' : undefined}
       title={label} onpointerenter={() => loadPage(key).catch(() => {})}>
      {@render icon(key)}<span class="lbl">{label}</span>
      {#if key === 'live' && view.status === 'champ_select'}<span class="live-dot" title="Champ select is on"></span>{/if}
      {#if navKey(page) === key}{@render marker()}{/if}
    </a>
  {/each}
  <span class="grow"></span>
  {#each ICONS as [key, label]}
    <a href={`#/${key}`} class:active={navKey(page) === key} aria-current={navKey(page) === key ? 'page' : undefined}
       title={label} onpointerenter={() => loadPage(key).catch(() => {})}>
      {@render icon(key)}<span class="lbl">{key === 'crawl' ? 'Crawl' : 'Settings'}</span>
      {#if navKey(page) === key}{@render marker()}{/if}
    </a>
  {/each}
</nav>
<!-- One marker, one key: it slides from the old item to the new one instead of blinking out and in -->
{#snippet marker()}<span class="marker" in:receiveShared={{ key: 'nav' }} out:sendShared={{ key: 'nav' }}></span>{/snippet}
{#snippet icon(key)}
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
    {#if key === 'live'}<circle cx="12" cy="12" r="3"/><path d="M6.3 6.3a8 8 0 0 0 0 11.4M17.7 6.3a8 8 0 0 1 0 11.4"/>
    {:else if key === 'prep'}<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>
    {:else if key === 'history'}<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>
    {:else if key === 'pools'}<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 4.5a3.2 3.2 0 0 1 0 6.3M18 14.2c1.9.8 3 2.8 3 5.8"/>
    {:else if key === 'crawl'}<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>
    {:else}<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.6M12 18.6v2.6M21.2 12h-2.6M5.4 12H2.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8M18.5 18.5l-1.8-1.8M7.3 7.3 5.5 5.5"/>{/if}
  </svg>
{/snippet}

<div class="content" class:folded={railFolded}>
{#if !railFolded}<Notice items={notices} />{/if}
<!-- Pages share one grid cell, so the new page comes in while the old one leaves -->
{#snippet pageView(entry)}
{#if entry.Comp}
  <entry.Comp {param} />
{:else if LOADERS[entry.key]}
  <main class="idle"><h1>Couldn't load this page</h1><p class="error">Check the connection to the app, then reload.</p></main>
{:else if view.status === 'champ_select'}
  <Live {view} {secondsLeft} {notices} />
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

<Toast />
{#if page === 'live' && view.plan}
  {#key view.plan.text}<PlanBar bar={view.plan} />{/key}
{/if}
</div>

<style>
  /* ---------- the left rail ---------- */
  .rail { position: fixed; inset: 0 auto 0 0; z-index: 20; width: var(--rail-w); display: flex; flex-direction: column;
          gap: 2px; padding: 12px 10px; background: var(--rail); border-right: 1px solid var(--hairline);
          transition: width var(--dur-enter) var(--ease-in-out); overflow: hidden; }
  .rail.folded { width: var(--rail-w-collapsed); }
  .rail.folded:hover, .rail.folded:focus-within { width: var(--rail-w); box-shadow: 8px 0 24px rgba(0, 0, 0, 0.45); }
  .brand { display: flex; align-items: center; gap: 10px; height: 32px; padding: 0 8px; margin-bottom: 14px; }
  .mark { flex: none; width: 18px; height: 18px; border-radius: 4px; background: var(--signal);
          clip-path: polygon(0 0, 100% 0, 100% 62%, 62% 100%, 0 100%); }
  .word { font-weight: 700; font-size: 15px; letter-spacing: -0.01em; white-space: nowrap; }
  .rail a { position: relative; display: flex; align-items: center; gap: 12px; height: 36px; padding: 0 10px;
            border-radius: var(--radius-sm); color: var(--text-3); text-decoration: none; font-size: 14px; white-space: nowrap; }
  .rail a svg { flex: none; }
  .rail a:hover { color: var(--text-1); background: var(--surface-1); }
  .rail a.active { color: var(--text-1); font-weight: 600; }
  .marker { position: absolute; inset: 0; border-radius: var(--radius-sm); background: var(--surface-2);
            box-shadow: inset 2px 0 0 var(--signal); z-index: -1; }
  .rail a { isolation: isolate; }
  .lbl { transition: opacity var(--dur-enter) var(--ease-out); }
  .rail.folded:not(:hover):not(:focus-within) .lbl, .rail.folded:not(:hover):not(:focus-within) .word { opacity: 0; }
  .live-dot { position: absolute; left: 24px; top: 8px; width: 7px; height: 7px; border-radius: 50%; background: var(--signal);
              box-shadow: 0 0 0 2px var(--rail); animation: pulse 1.6s var(--ease-in-out) infinite; }
  @keyframes pulse { 50% { opacity: 0.35; } }
  .grow { flex: 1; }
  .content { margin-left: var(--rail-w); min-height: 100vh; transition: margin-left var(--dur-enter) var(--ease-in-out); }
  .content.folded { margin-left: var(--rail-w-collapsed); }
  @media (max-width: 900px) {
    .rail:not(:hover):not(:focus-within) { width: var(--rail-w-collapsed); }
    .rail:not(:hover):not(:focus-within) .lbl, .rail:not(:hover):not(:focus-within) .word { opacity: 0; }
    .content { margin-left: var(--rail-w-collapsed); }
  }
  .stage { display: grid; }
  .stage > .page { grid-area: 1 / 1; min-width: 0; }
  .stage > .page.waiting { opacity: 0; pointer-events: none; }
  .stage > .page.leaving { opacity: 0; pointer-events: none; transition: opacity var(--dur-page-exit) var(--ease-in); }
  .idle { display: grid; place-items: center; align-content: center; min-height: 80vh; gap: 4px; color: var(--muted); }
  .idle h1 { color: var(--text); font-size: 32px; font-weight: 650; letter-spacing: -0.02em; margin: 0; }
  .idle p { font-size: 16px; margin: 4px; }
  .scoutwrap { margin-top: 28px; display: flex; justify-content: center; width: 100%; }
  .error { color: var(--red); }
</style>
