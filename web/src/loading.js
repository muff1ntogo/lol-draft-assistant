// Pages appear all at once: a new page is mounted out of sight, and shown only when the requests it started have
// answered (or after MAX_WAIT_MS, so a slow endpoint never blocks the app). The old page stays up meanwhile and leaves
// as the new one comes in, so the loading time hides behind the transition instead of showing as half-filled panels.
// Only requests started after the page was mounted count, so another page's polling never holds it back.

export const MAX_WAIT_MS = 700;
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

const frame = () => new Promise((r) => requestAnimationFrame(() => r()));

// Resolves once every request started at or after `since` has finished, and the page has had two frames to render
// what came back (a request that starts another one, e.g. a list then its first item, is waited for too).
export async function settled(since, maxMs = MAX_WAIT_MS) {
  const deadline = since + maxMs;
  const timeout = new Promise((r) => setTimeout(r, Math.max(0, deadline - performance.now())));
  await frame(); // let the page mount and start its requests
  for (;;) {
    const mine = [...inflight].filter((e) => e.started >= since);
    if (!mine.length) break;
    const outcome = await Promise.race([Promise.all(mine.map((e) => e.done)).then(() => 'done'), timeout.then(() => 'late')]);
    if (outcome === 'late') return;
    await frame(); // a response may have started the next request
  }
  await frame();
  await frame();
}
