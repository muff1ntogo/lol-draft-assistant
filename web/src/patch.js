// Riot's data calls this year's patches 16.x (match gameVersion, Data Dragon); players know them as 26.x. Stats, URLs and
// filters keep Riot's number, and only what's shown changes. Same rule as data/crawler.display_patch.
export function patchLabel(patch) {
  if (!patch) return patch;
  const [major, ...rest] = String(patch).split('.');
  return Number(major) >= 16 ? [Number(major) + 10, ...rest].join('.') : String(patch);
}

// What a person types ("26.20", or Riot's "16.20") -> Riot's number, for filters and the crawl's --patch.
export function riotPatch(text) {
  const m = String(text ?? '').trim().match(/^(\d{1,2})\.(\d{1,2})$/);
  if (!m) return '';
  const major = Number(m[1]);
  return `${major >= 26 ? major - 10 : major}.${Number(m[2])}`;
}
