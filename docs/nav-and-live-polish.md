# Top bar and Live polish (8 Nov 2026)

Fifth task from `ui-engine-review.md` (decision 3 and section 1). Built in one stage.

## Top bar

**Live · Prep · Review · Roster**, then **Crawl** (database icon) and **Settings** (gear) on the right, with
tooltips and screen-reader labels. Every page keeps its address; a tab stays lit on the pages it groups:

| Tab | Pages |
| --- | --- |
| Review | History (`#/history`) and Team trends (`#/team`), switched by a small sub-bar (`#/review` opens History) |
| Roster | the Pools page (`#/pools`; `#/roster` works too), now titled "Roster" |
| Settings icon | Settings, Tags (`#/tags`, `#/champions`) and Rules (`#/rules`) |

"Champ select live →" still appears when a draft starts while you're on another page.

## Live

- **Timer** turns gold under 10 s and red under 5 s.
- **Contested picks.** A pick card says "contested: 9% they pick it next" when the enemy's next pick takes it 3%+ of
  the time (`CONTESTED`; from `enemy_pick_probs`, the same model as their likely picks). No line = safe to delay as
  far as their next pick goes.
- **Thin data.** "rare pick: few games behind these numbers" when the champion is picked in under 0.5% of the
  player's bracket's games in that role (`RARE_PICK_RATE`).
- **Hover vs our #1.** The hover card shows its difference from our top suggestion ("−2.1pp vs Orianna").
- **Keys:** D and S switch Draft and Strategy (ignored while typing).
- **Compact layout** under 1,100 px wide (narrower side columns, smaller header), one column under 760 px, so the
  app fits beside the client.

Tests: `tests/test_live_polish.py`.
