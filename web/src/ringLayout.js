// The Teamfight view of the synergy visualiser: a pentagon of role groups, every champion's icon on one circle. A chord
// of a circle stays inside it, so a line between two champions can never pass behind a third.
// Around the ring: Top, Jungle, Mid, ADC, Support, with the lane pairs on the rim and the rest forming the star inside.

const CORNERS = { top: -90, jungle: -18, mid: 54, adc: 126, support: 198 }; // degrees, clockwise from the right
// The role names sit outside the ring. Jungle's and Support's go up into the empty corners, so they don't cost it width.
const HEADS = { top: -90, jungle: -48, mid: 54, adc: 126, support: 228 };
const SPAN = (58 * Math.PI) / 180; // of the ring one role may fill, leaving a gap between corners
const STEP = 48; // px between neighbouring icons
const MIN_STEP = 26;
const MIN_RADIUS = 170;
const HEAD_W = 134; // the role's name and add box
const HEAD_H = 58;
const HEAD_GAP = 36; // from the ring's icons to the role name
const ICON_GAP = 6;
const MAX_ICON = 40;
const MIN_ICON = 22;

export function ringLayout(cols, width) {
  const most = Math.max(1, ...cols.map((c) => c.nodes.length));
  const wanted = Math.max(MIN_RADIUS, ((most - 1) * STEP) / SPAN);
  const headRoom = (width / 2 - HEAD_W - 10) / Math.cos((HEADS.jungle * Math.PI) / 180) - MAX_ICON / 2 - HEAD_GAP;
  const radius = Math.max(MIN_RADIUS, Math.min(wanted, width / 2 - MAX_ICON, headRoom));
  const step = most > 1 ? Math.max(MIN_STEP, Math.min(STEP, (radius * SPAN) / (most - 1))) : STEP;
  const icon = Math.max(MIN_ICON, Math.min(MAX_ICON, step - ICON_GAP));

  const headAt = radius + icon / 2 + HEAD_GAP;
  const cx = width / 2;
  const cy = headAt + HEAD_H + 8;
  const bottom = Math.max(radius + icon / 2 + 12, headAt * Math.sin((CORNERS.mid * Math.PI) / 180) + HEAD_H / 2);

  const nodes = {};
  const heads = {};
  for (const col of cols) {
    const corner = (CORNERS[col.role] * Math.PI) / 180;
    // Left-hand corners run the other way round, so a pool reads top to bottom on both sides.
    const direction = Math.cos(corner) < -1e-6 ? -1 : 1;
    col.nodes.forEach((n, j) => {
      const angle = corner + (direction * (j - (col.nodes.length - 1) / 2) * step) / radius;
      nodes[`${col.role}:${n.id}`] = { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
    });
    const head = (HEADS[col.role] * Math.PI) / 180;
    heads[col.role] = { x: cx + headAt * Math.cos(head), y: cy + headAt * Math.sin(head),
                        place: col.role === 'top' ? 'above' : direction < 0 ? 'left' : 'right' };
  }
  return { nodes, heads, icon, height: Math.ceil(cy + bottom + 10) };
}
