// Riot's data calls this year's patches 16.x (match gameVersion, Data Dragon); players know them as 26.x. Stats, URLs and
// filters keep Riot's number, and only what's shown changes. Same rule as data/crawler.display_patch.
export function patchLabel(patch) {
  if (!patch) return patch;
  const [major, ...rest] = String(patch).split('.');
  return Number(major) >= 16 ? [Number(major) + 10, ...rest].join('.') : String(patch);
}
