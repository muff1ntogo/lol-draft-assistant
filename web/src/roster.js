// Shared by the Roster page, the Add player sheet and the profile page (docs/roster-profiles.md).
import { signed } from './format.js';

export const ROLES = ['top', 'jungle', 'mid', 'adc', 'support'];
export const ROLE_LABELS = { top: 'Top', jungle: 'Jungle', mid: 'Mid', adc: 'ADC', support: 'Support' };
export const TIERS = ['main', 'comfortable', 'learning'];
export const TIER_LABELS = { main: 'Main', comfortable: 'Comfortable', learning: 'Learning' };
export const TIER_CODES = { main: 'MN', comfortable: 'CF', learning: 'LN' }; // the two-letter tag on a pool strip
export const TIER_OPTIONS = TIERS.map((t) => [t, TIER_LABELS[t]]);
export const BRACKETS = ['iron', 'bronze', 'silver', 'gold', 'platinum', 'emerald'];

// One request: the parsed body, or an Error whose message is what the app said was wrong
export async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(path, { method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
                              body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new Error("the app didn't answer");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.errors?.join('; ') ?? `the app answered ${res.status}`);
  }
  return data;
}

export const playerPath = (riotId) => `/api/players/${encodeURIComponent(riotId)}`;
export const profileHref = (riotId) => `#/player/${encodeURIComponent(riotId)}`;
export const nameOf = (riotId) => riotId.split('#')[0];
export const tagOf = (riotId) => (riotId.includes('#') ? `#${riotId.split('#').slice(1).join('#')}` : '');

export const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
export const fixed = (x, digits = 1) => (x == null ? '—' : x.toFixed(digits));
export const whole = (x) => (x == null ? '—' : Math.round(x).toLocaleString());
export const pp = (x, digits = 1) => (x == null ? '—' : `${signed(x, digits)}pp`);
export const day = (ms) => (ms ? new Date(ms).toLocaleDateString([], { day: 'numeric', month: 'short' }) : '—');
export const rankText = (r) => (r ? `${r.label}${r.lp != null ? ` ${r.lp} LP` : ''}` : '');
export const QUEUE_LABELS = { solo: 'Solo/Duo', flex: 'Flex', fives: 'Ranked 5s' };

// A lane-execution figure is coloured only when its label says the band is clear of 0 (colour rule 3)
export const executionTone = (ex) => (ex?.label === 'Wins lane' ? 'up' : ex?.label === 'Loses lane' ? 'down' : null);
export const TREND = { up: ['↑', 'improving over the last 10 team games'], down: ['↓', 'slipping over the last 10 team games'],
                       flat: ['→', 'steady over the last 10 team games'] };

// What a role's pool is missing, most important first, as one line (engine/blind.pool_health)
export function healthLine(h) {
  if (!h) return null;
  if (!h.blind?.ok) return { warn: true, text: 'No safe blind pick' };
  if (h.setup?.applies && !h.setup.ok) return { warn: true, text: 'No setup champion' };
  if (h.counter?.main && !h.counter.ok) {
    const worst = h.counter.worst ?? [];
    return { warn: true, text: `Counters to ${h.counter.main}'s worst: ${worst.filter((w) => w.answered).length} of ${worst.length}` };
  }
  return { warn: false, text: `Blind pick ${h.blind.name}` };
}
