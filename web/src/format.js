// Shared by the History strip and the Team page's trends (docs/draft-vs-execution.md): one source for how a signed number
// and each stat read.

// Rounded first, so a value that shows as zero has no sign (never "−0.0")
export function signed(x, digits) {
  const shown = Math.abs(x).toFixed(digits);
  return `${Number(shown) === 0 ? '' : x > 0 ? '+' : '−'}${shown}`;
}

// stat -> [name, kind]; kind picks the format: lead (whole, signed), lead1 (one decimal, signed), pct, count
export const STATS = {
  gold: ['Gold lead', 'lead'], xp: ['XP lead', 'lead'], cs: ['CS lead at 14', 'lead1'], gold_25: ['Gold lead at 25', 'lead'],
  deaths_solo: ['Solo deaths before 14', 'count'], deaths_ganked: ['Ganked deaths before 14', 'count'],
  jungle_cs: ['Jungle CS at 15', 'count'], kp: ['Kill participation before 15', 'pct'], vision: ['Wards placed + killed per minute', 'count'],
  dragons: ['Dragons by 15 (us − them)', 'lead1'], grubs: ['Grubs by 15 (us − them)', 'lead1'],
  towers: ['Towers by 15 (us − them)', 'lead1'], plates: ['Plates by 15 (us − them)', 'lead1'],
};

export const statName = (stat) => STATS[stat]?.[0] ?? stat;
