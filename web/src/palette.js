// The palette for code that can't read CSS variables (ECharts draws on a canvas). Same values as app.css's :root.
// Chart colours were checked with the dataviz palette validator on the graphite panel surface PANEL (#141519), dark
// mode (12 Nov 2026): components blue / orange / aqua, favourability teal UP vs orange DOWN, sides SIDE_BLUE vs
// SIDE_RED and the five identities all pass. Orange DOWN and SIDE_RED are too close to tell apart (normal-vision
// ΔE 6.9), so sides only mark structure and favourability only colours signed numbers (docs/ui-redesign.md).
// The lime SIGNAL means "act on this" and is never a data series' colour.
export const PANEL = '#141519';
export const PANEL_2 = '#1b1d22';
export const LINE = '#2a2d34';
export const LINE_STRONG = '#3a3e47';
export const TEXT = '#ececee';
export const TEXT_2 = '#b4b8c1'; // a single series with no meaning of its own (win rate by split): neutral ink
export const MUTED = '#858a95';
export const SIGNAL = '#c8f135';
export const GOLD = SIGNAL; // the old name, kept for the charts that still read it
export const UP = '#1baf7a';
export const DOWN = '#e0702e';
export const SIDE_BLUE = '#4a8ee0';
export const SIDE_RED = '#e0564c';
export const COMPONENT = { threat: '#3987e5', matchup: '#d95926', comp: '#199e70' };
export const FONT_UI = 'Inter, "Segoe UI", system-ui, sans-serif';
export const FONT_MONO = '"JetBrains Mono", Consolas, monospace';
