// The app's motion vocabulary: one place for durations, curves and the Svelte transitions built on them, so every
// page moves the same way. The rules (also in app.css's tokens):
// - Entrances ease out; on-screen movement eases in and out; no spring bounce in everyday transitions.
// - Exits are about 20% shorter than entrances. Within a page the new state comes in as the old leaves; whole pages
//   go one at a time (the old one fades to blank, then the new one rises in; App.svelte).
// - Dropdown-like reveals stay within 150-250 ms; list rows stagger by 30 ms.
// - Reveals and colour changes use a mask (clip-path), not a fade from the centre.
// - Popovers start at scale 0.95 from their trigger and go back to it, never into the centre.
// - Moving objects travel on arcs; anything that follows the pointer is never tweened.
// - Shared objects (the nav's underline) keep one key, so it moves between places instead of blinking.
// prefers-reduced-motion turns every one of these into an instant change.
import { crossfade } from 'svelte/transition';

export const DUR = { enter: 220, menu: 200, move: 280, stagger: 30, pulse: 520, exitPage: 160 }; // exitPage: app.css --dur-page-exit
export const EXIT = 0.8; // exits are 20% shorter than entrances
const STAGGER_CAP = 12; // a long list shouldn't keep its last row waiting

// Curves as functions of t in [0, 1]. Quartic out for entrances (fast start, soft landing), cubic in-out for
// movement already on screen, cubic in for the short exits.
export const easeOut = (t) => 1 - (1 - t) ** 4;
export const easeInOut = (t) => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2);
export const easeIn = (t) => t ** 3;
// The same curves for CSS and the Web Animations API
export const CSS_EASE = {
  out: 'cubic-bezier(0.25, 1, 0.5, 1)',
  inOut: 'cubic-bezier(0.65, 0, 0.35, 1)',
  in: 'cubic-bezier(0.32, 0, 0.67, 0)',
};

export function reducedMotion() {
  try { return matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; }
}

const timing = (duration, direction) => {
  const out = direction === 'out';
  return { duration: reducedMotion() ? 0 : Math.round(out ? duration * EXIT : duration), easing: out ? easeIn : easeOut };
};

// A mask reveal from the top edge (expanding panels, tables, rows of chips): the content is uncovered in place, not
// faded in from nothing. Going out it is covered again, upwards, a little quicker.
export function reveal(node, { duration = DUR.menu, delay = 0, from = 'top' } = {}, { direction } = {}) {
  const inset = (k) => (from === 'left' ? `inset(0 ${k}% 0 0)` : `inset(0 0 ${k}% 0)`);
  return {
    delay,
    ...timing(duration, direction),
    css: (t, u) => `clip-path: ${inset(u * 100)}; opacity: ${Math.min(1, 0.4 + t)};`,
  };
}

// List rows: a short rise with a 30 ms stagger per row (pass the row's index as i).
export function rise(node, { i = 0, duration = DUR.enter, y = 6 } = {}, { direction } = {}) {
  const out = direction === 'out';
  return {
    delay: out || reducedMotion() ? 0 : Math.min(i, STAGGER_CAP) * DUR.stagger,
    ...timing(duration, direction),
    css: (t, u) => `opacity: ${t}; transform: translateY(${(u * y).toFixed(2)}px);`,
  };
}

// Popovers and tooltips: from 0.95 at the trigger's side (origin), back to it on the way out.
// base keeps a transform the element already has (e.g. translateX(-50%)).
export function pop(node, { duration = DUR.menu, origin = 'top center', base = '' } = {}, { direction } = {}) {
  node.style.transformOrigin = origin;
  return {
    ...timing(duration, direction),
    css: (t) => `opacity: ${t}; transform: ${base} scale(${0.95 + 0.05 * t});`,
  };
}

// Icons (and short labels) that change in place: crossfade with a little scale and blur. Use it on both the old and
// the new state inside a {#key}, laid over each other, so one comes in as the other leaves.
export function swap(node, { duration = 180 } = {}, { direction } = {}) {
  return {
    ...timing(duration, direction),
    css: (t, u) => `opacity: ${t}; transform: scale(${0.8 + 0.2 * t}); filter: blur(${(u * 3).toFixed(2)}px);`,
  };
}

// Shared objects: the same key on both ends moves one object from its old place to its new one (an on-screen move, so
// ease in and out), instead of one disappearing and another appearing.
export const [sendShared, receiveShared] = crossfade({
  duration: () => (reducedMotion() ? 0 : DUR.move),
  easing: easeInOut,
  fallback: (node, params, options) => reveal(node, { from: 'left' }, options),
});

// A brief pulse to confirm something worked (a save, an applied setting). Re-runs whenever `value` changes to a
// new success; failures (ok false) don't pulse.
export function pulse(node, params = {}) {
  let last = params.value;
  const run = () => {
    if (reducedMotion() || !node.animate) return;
    node.animate(
      [
        { transform: 'scale(1)', boxShadow: '0 0 0 0 rgba(63, 185, 80, 0.45)' },
        { transform: 'scale(1.03)', boxShadow: '0 0 0 6px rgba(63, 185, 80, 0.12)', offset: 0.35 },
        { transform: 'scale(1)', boxShadow: '0 0 0 10px rgba(63, 185, 80, 0)' },
      ],
      { duration: DUR.pulse, easing: CSS_EASE.out },
    );
  };
  if (params.ok !== false && params.value) run();
  return {
    update(next = {}) {
      if (next.value && next.value !== last && next.ok !== false) run();
      last = next.value;
    },
  };
}

// Objects that move between laid-out positions (the synergy ring's icons): when {x, y} changes, travel there on an arc
// instead of a straight line. With a centre (cx, cy) the path follows the circle round it; without one it bows
// sideways by `bow` of the distance (0 = straight, for rows sliding within a list). A change of `width` (the window
// being resized) snaps instead: a layout that follows the user's hand is never tweened. The element is placed at the new spot at once and animated from the old one
// with a transform, so layout and hit-testing are right from the first frame.
export function arcMove(node, params) {
  let prev = params;
  let anim = null;
  const STEPS = 12;
  function path(a, b, centre, bow) {
    const pts = [];
    if (centre) {
      const ra = Math.hypot(a.x - centre.cx, a.y - centre.cy), rb = Math.hypot(b.x - centre.cx, b.y - centre.cy);
      const aa = Math.atan2(a.y - centre.cy, a.x - centre.cx);
      let ab = Math.atan2(b.y - centre.cy, b.x - centre.cx);
      if (ab - aa > Math.PI) ab -= 2 * Math.PI;
      if (aa - ab > Math.PI) ab += 2 * Math.PI;
      for (let s = 0; s <= STEPS; s++) {
        const k = s / STEPS, r = ra + (rb - ra) * k, ang = aa + (ab - aa) * k;
        pts.push({ x: centre.cx + r * Math.cos(ang), y: centre.cy + r * Math.sin(ang) });
      }
    } else {
      const dx = b.x - a.x, dy = b.y - a.y;
      for (let s = 0; s <= STEPS; s++) {
        const k = s / STEPS, lift = 4 * k * (1 - k) * bow; // a parabola: 0 at both ends
        pts.push({ x: a.x + dx * k - dy * lift, y: a.y + dy * k + dx * lift });
      }
    }
    return pts;
  }
  return {
    update(next) {
      const resized = prev && next.width !== prev.width;
      const moved = prev && !resized && (Math.abs(next.x - prev.x) > 0.5 || Math.abs(next.y - prev.y) > 0.5);
      if (resized) anim?.cancel();
      if (moved && !reducedMotion() && node.animate) {
        // start from where it is now if a previous move is still running
        const from = anim && anim.playState === 'running' && prev.at ? prev.at() : prev;
        anim?.cancel();
        const pts = path(from, next, next.cx != null ? { cx: next.cx, cy: next.cy } : null, next.bow ?? 0.2);
        const base = next.base ?? '';
        const frames = pts.map((p) => ({ transform: `translate(${p.x - next.x}px, ${p.y - next.y}px) ${base}` }));
        anim = node.animate(frames, { duration: DUR.move, easing: CSS_EASE.inOut });
        const started = performance.now();
        next = { ...next, at: () => {
          const k = Math.min(1, (performance.now() - started) / DUR.move);
          return pts[Math.round(easeInOut(k) * STEPS)];
        } };
      }
      prev = next;
    },
    destroy() { anim?.cancel(); },
  };
}
