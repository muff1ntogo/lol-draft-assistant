<script>
  import Live from './Live.svelte';

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

{#if !connected}
  <div class="banner offline">Reconnecting to the assistant…</div>
{/if}
{#if view.data_source === 'fixture'}
  <div class="banner fixture">{view.note ?? 'Synthetic data'}</div>
{/if}

{#if view.status === 'champ_select'}
  <Live {view} {secondsLeft} />
{:else}
  <main class="idle">
    <h1>{view.status === 'error' ? 'Something went wrong' : view.message}</h1>
    {#if view.status === 'error'}<p class="error">{view.message}</p>{/if}
    {#if view.client}<p>League client: {view.client}</p>{/if}
  </main>
{/if}

<style>
  .banner { padding: 6px 16px; font-size: 14px; text-align: center; }
  .offline { background: #6e2a2a; }
  .fixture { background: #3d3214; color: var(--gold); }
  .idle { display: grid; place-items: center; align-content: center; min-height: 80vh; gap: 4px; color: var(--muted); }
  .idle h1 { color: var(--text); font-size: 40px; font-weight: 600; margin: 0; }
  .idle p { font-size: 20px; margin: 4px; }
  .error { color: var(--red); }
</style>
