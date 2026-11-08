# History charts (8 Nov 2026)

Third task from `ui-engine-review.md` (decision 4: Apache ECharts). Built in one stage.

## What's on the page

An **Overview** band above History's list and recap (collapsible; remembered per browser). It follows the list's
queue / side / patch filters; the result filter doesn't apply. Remakes and games without a result are left out.

| Chart | What it shows |
| --- | --- |
| Win rate vs predicted | rolling win rate over the last 10 games (solid blue) and the mean predicted P(win) (dashed orange), 50% dotted |
| Calibration | predicted vs actual per 10% bin, dot size by games, the 95% interval as a whisker, the diagonal = well calibrated. Live games with a prediction only (imported games have none). Replaces the sidebar's calibration table |
| What decided each game | per game, the draft / early game / conversion split from `game_execution` (pp of a win), stacked by sign |
| Win rate by side / queue / our #1 suggestion / pinned plan | bars with 95% Wilson whiskers, games in the label. "#1 suggestion": live games where we took our top suggestion on half our turns or more |
| Players | per player (name from the draft, else the role) and champion: games, win rate (interval on hover), mean gold lead at 15 from `own_game_participants` (teal ahead, amber behind) |
| Our bans | most banned champions and our win rate in those games, against the overall |

API: `GET /api/history/stats?queue=&side=&patch=` (`api/history_stats.py`), read-only.

## Notes

- ECharts is imported module by module (`echarts/core` plus bar, line, scatter, custom, grid, tooltip, legend,
  mark line, SVG renderer) in `web/src/EChart.svelte`, and the History charts are a lazily loaded chunk (~560 kB,
  187 kB gzipped): the rest of the UI didn't grow (`vite.config.js` raises the size warning to 600 kB).
- Colours: the component set already validated on the panel surface (blue `#3987e5`, orange `#d95926`, aqua
  `#199e70`; rerun through the dataviz validator, all checks pass) and the favourability pair for gold.
- A ban's own effect on P(win) isn't shown: the draft model's P(win) only moves on picks.

Tests: `tests/test_history_stats.py`.
