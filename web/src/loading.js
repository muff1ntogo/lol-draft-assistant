// Pages appear all at once: a new page is mounted out of sight, and shown only when what it started has arrived: its
// requests, any code it imports on the way (track()), and its images. MAX_WAIT_MS caps the wait, so a slow endpoint
// never blocks the app. Only requests started after the page was mounted count, so another page's polling never
// holds it back.
export const MAX_WAIT_MS = 900;
const QUIET_MS = 50; // after the last answer, how long to watch for a follow-up request before calling it done
const inflight = new Set(); // { started, done: Promise }

if (typeof window !== 'undefined' && window.fetch && !window.fetch.__tracked) {
  const original = window.fetch.bind(window);
  const tracked = (input, init) => {
    let finish;
    const entry = { started: performance.now(), done: new Promise((r) => (finish = r)) };
    inflight.add(entry);
    const end = () => { inflight.delete(entry); finish(); };
    return original(input, init).then(
      (res) => {
        // the page reads the body next: count the request as done once the body is in, not at the headers
        res.clone().arrayBuffer().then(end, end);
        return res;
      },
      (err) => { end(); throw err; },
    );
  };
  tracked.__tracked = true;
  window.fetch = tracked;
}

// Count any other promise a page waits on, e.g. a chart's code: track(import('./Chart.svelte'))
export function track(promise) {
  let finish;
  const entry = { started: performance.now(), done: new Promise((r) => (finish = r)) };
  inflight.add(entry);
  Promise.resolve(promise).finally(() => { inflight.delete(entry); finish(); });
  return promise;
}

const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Images still loading inside the page (champion icons come from Riot's CDN), as promises that settle either way
function pendingImages(root) {
  if (!root) return [];
  return [...root.querySelectorAll('img')].filter((img) => !img.complete)
    .map((img) => new Promise((r) => { img.addEventListener('load', r, { once: true }); img.addEventListener('error', r, { once: true }); }));
}

// Resolves once everything the page started at or after `since` has arrived, then gives it two frames to render.
// A request that starts another (a list, then its first item) is waited for too. `root()` returns the page's element,
// whose images are waited for as well.
export async function settled(since, root = () => null, maxMs = MAX_WAIT_MS) {
  const deadline = since + maxMs;
  const timeout = new Promise((r) => setTimeout(r, Math.max(0, deadline - performance.now()))).then(() => 'late');
  await frame(); // let the page mount and start its requests
  for (;;) {
    const mine = [...inflight].filter((e) => e.started >= since).map((e) => e.done);
    const waits = [...mine, ...pendingImages(root())];
    if (!waits.length) {
      // nothing pending: watch briefly for a follow-up (a response that renders a component that fetches)
      if (await Promise.race([sleep(QUIET_MS).then(() => 'quiet'), timeout]) === 'late') return;
      if ([...inflight].some((e) => e.started >= since) || pendingImages(root()).length) continue;
      break;
    }
    if (await Promise.race([Promise.all(waits).then(() => 'done'), timeout]) === 'late') return;
    await frame(); // let what arrived render (it may add images or start the next request)
  }
  await frame();
  await frame();
}
