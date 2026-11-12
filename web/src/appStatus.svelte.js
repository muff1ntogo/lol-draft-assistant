// What the app has to say about itself, as items for the Notice strip (web/src/ui/Notice.svelte): an update to install
// (updates.py), the first-run setup (first_run.py) and a missing Riot key. App.svelte adds "offline" and the
// demo-data note, and decides where the strip goes (the top of the page, or one line inside Live during a draft).
export function appStatus() {
  let update = $state(null);
  let setup = $state(null);
  let installing = $state(false);
  let failure = $state('');
  let hideKey = $state(false);

  async function load() {
    try {
      [update, setup] = await Promise.all([(await fetch('/api/update')).json(), (await fetch('/api/setup')).json()]);
    } catch {
      /* the app is restarting */
    }
  }
  // every 3 s while the first-run setup is working, otherwise every 10 minutes
  async function loop() {
    await load();
    setTimeout(loop, setup?.bootstrap?.state === 'running' ? 3000 : 10 * 60 * 1000);
  }
  loop();
  // Settings' "Check for updates" tells the strip straight away, instead of at the next 10-minute look
  window.addEventListener('draftassist:update', (e) => (update = e.detail));

  async function install() {
    installing = true;
    failure = '';
    try {
      const res = await fetch('/api/update/install', { method: 'POST' });
      if (!res.ok) throw new Error((await res.json()).detail);
    } catch (e) {
      failure = e.message || 'the app did not answer';
      installing = false;
    }
  }

  // inDraft: from champ select to the end of the game an update can be seen but not started (updates.py refuses too)
  function items(inDraft) {
    const out = [];
    const boot = setup?.bootstrap;
    if (failure) {
      out.push({ id: 'update-failed', kind: 'error', text: "The update didn't install", detail: failure, ondismiss: () => (failure = '') });
    }
    if (boot?.state === 'running' && boot.pull) {
      const { done, total } = boot.pull;
      out.push({ id: 'first-run', kind: 'setup', progress: total ? done / total : 0,
                 text: `Pulling the team's stats: ${done.toLocaleString()} of ${total ? total.toLocaleString() : '…'} blocks`,
                 detail: "Live uses OP.GG's stats until it's done" });
    } else if (boot?.state === 'running') {
      out.push({ id: 'first-run', kind: 'setup', text: 'Setting up: fetching champions and starting stats', detail: 'about a minute' });
    } else if (boot?.state === 'done' && Object.values(boot.jobs).some((s) => s !== 'done')) {
      out.push({ id: 'first-run', kind: 'setup', text: "First-run setup couldn't fetch everything (no internet?)", detail: 'it tries again at the next start' });
    }
    if (setup && !setup.riot_key_set && !hideKey) {
      out.push({ id: 'riot-key', kind: 'setup', text: 'Add your Riot API key to scout opponents and read match history',
                 actions: [{ label: 'Open Settings', href: '#/settings' }], ondismiss: () => (hideKey = true) });
    }
    if (update?.available && installing) {
      out.push({ id: 'update', kind: 'update', text: `Installing Draft Assistant ${update.latest}`, detail: 'the app closes and opens again in a moment' });
    } else if (update?.available) {
      out.push({ id: 'update', kind: 'update', text: `Draft Assistant ${update.latest} is available`,
                 detail: inDraft ? `you have ${update.current}; it can install after the game` : `you have ${update.current}`,
                 actions: [{ label: 'Update', onclick: install, disabled: inDraft, title: inDraft ? 'After the game' : update.notes }] });
    }
    return out;
  }
  return { items };
}
