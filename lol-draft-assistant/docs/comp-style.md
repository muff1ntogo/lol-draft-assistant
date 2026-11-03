# Task: comp type and team-fight style, live in the draft

Read `CLAUDE.md`, `docs/SPEC.md` (Draft engine, UI) and `docs/synergy-visualizer.md` first. Build in the three
stages below, stopping after each for review. Write the tests for each stage first.

Replaces the Data Dragon class rules in `learning/style.py` (engage / dive / poke / balanced) with one shared
definition; "engage" becomes "Front to back".

## What it shows

- **Comp indicator** for both teams on the Live screen: comp type (Early / Mix / Late), fight style shares
  (Dive / Poke / Front to back, "Mixed" until one leads) and a confidence bar (picks so far out of 5).
- **Champion badges** wherever champions appear (suggestion cards, locked picks on both sides, Synergy nodes,
  Pools): e.g. `Late · Front-line`. Suggestion cards also say what the pick does to our comp
  (`→ pushes Late`, `→ completes Front to back`).
- **Suggestion count:** up to 5 cards in tournament ban phase 1 and our picks 1–3; 3 cards in ban phase 2
  and our picks 4–5. Flex: 5 for our picks 1–3, 3 for picks 4–5; simultaneous bans unchanged. Cards switch to
  a compact layout at 5 so the Live screen (with "Next 2 picks") still fits 1920×1080.

## Sources

1. **Early / late (power curve): crawl data.** Store game length per match (backfilled from the Riot cache,
   no API calls). Per champion and role:
   `curve = logit(WR in games > 32 min) − logit(WR in games < 25 min)`, each bucket shrunk towards the
   champion's own win rate, so a simply strong champion doesn't read as late. Thresholds give Early / Mid / Late;
   thin data gives no label.
2. **Dive / poke / front to back: a reviewed table.** A seed file with every champion's style weights (sum 1)
   and a front-line flag, written by Claude from champion knowledge and Data Dragon classes, marked
   "seeded" until reviewed. Edited on a Champions page; the user's edits always override the seed.

## Comp labels

- Comp type: mean curve of the team's picks against the thresholds.
- Fight style: sum of style weights; a style is named at ≥ 45% share with ≥ 2 contributors, else "Mixed".
  Front to back also needs a front-liner.
- Confidence: picks / 5.

## Stages

**A. Data.** Game length (crawler + cache backfill), short/long game counts in `champ_role_stats`, the seed file,
user overrides, a Champions editor page. Tests: an early champion gets a negative curve; thin data gives no
label; overrides beat the seed; the seed covers every champion.

**B. Live.** Indicator for both teams, badges, 5 / 3 card counts. Tests: labels converge as picks are added;
5 cards at our first picks, 3 at our last two; guardrails hold at 5; 1920×1080 screenshot without overflow;
response-time budgets.

**C. Ranking.** A comp-coherence term in the pick score (like damage balance), pulling towards the style set
by hand on the Team page if any, else the style our comp is converging on. **It must not take over shot calling
in our first 3 picks:** the term is damped there and full strength only for picks 4–5. Configurable, can be
switched off. Tests: 0 at our first pick; damped in picks 1–3; a pick completing the target style ranks
higher at pick 4; the Team page uses the same labels.

## Decisions (2 Nov 2026)

1. Style labels: seed written by Claude, reviewed by the user.
2. Style affects ranking, but must not fully take over shot calling in the first 3 picks.
3. Show the enemy's comp type and style too.
4. Flex: 5 / 3 cards by our pick number (1–3 / 4–5).

## Built (2 Nov 2026)

- **Stage A.** Real data (patch 16.21, 11,733 matches): 230 champion-roles with ≥ 50 short + long games, curve
  sd 0.22. ±0.10 labelled two thirds of them (mostly noise at 100–200 games), so the Early / Late threshold is
  **±0.15** (about half labelled).
- **Stage B.** `engine/comp.py`: `badge`, `team_comp`, `comp_effect`, `card_count`; profiles travel in
  `EngineData.profiles`. Badges name every style with weight ≥ 0.5, so a 0.5 / 0.5 champion reads
  "Front to back / Dive" (it counts towards both). Comp type uses "Mix" for the middle. Style-share colours are
  categorical slots 5–7 (violet / magenta / green; slots 1–3 are Threat / Matchup / Comp on the same screen),
  validated all-pairs on `#161b22`. Pools page badges added after review.
- **Stage C.** `DraftEngine.coherence`: `comp_weight × damping × (champion's weight in the target style − 1/3)`
  in log-odds (default 0.15, about +2.5pp of pick value for a perfect fit at full strength). Front to back: the
  first front-liner counts as a fit of weight + 0.5 (capped at 1). Target: the Team page's style ("Mixed" = no
  pull), else the comp's named style, else its leading style at ≥ 45%. It is added to pick **value**, not to
  P(win): a preference, not a measured effect. Damping by our pick number **0 / 0.1 / 0.25 / 1 / 1**. On real
  data (16.21) picks 2–3 are near ties (median gap between #1 and #2 ~0.5pp); ¼ / ½ still decided 40–53% of
  them, 0.1 / 0.25 changes the top pick at pick 2 in 3% of drafts and at pick 3 only breaks ties under ~0.6pp.
  Settings: on/off and strength. The Team page classifies logged drafts with `team_comp` (old "engage" /
  "balanced" labels read as Front to back / Mixed). The Live comp panel says which style our picks lean
  towards and why.
- **Varied Next 2 picks** (after review): choosing each pair, every earlier appearance of its champions costs
  `pair_repeat_margin` = 0.12 log-odds (~3pp). Real data, 120 back-to-back turns: one champion in all three
  pairs 13% → 1%, different champions 3.2 → 4.4 of 6, the third pair ~1.1pp further from the best.
