# National Poll Tracker (poll-tracker) — Reference

## 1. What it is

A daily-glance view of the 2026 midterm landscape: balance of power, recent
polls, polling averages, and forecaster ratings for every Senate and Governor
race, plus every House district on the competitive ratings list (and any other
polled district).

Not a model and not complete. It republishes what Wikipedia editors have
compiled from published polls and forecasters.

- **Live:** https://brennertobe07.github.io/poll-tracker/
- **Repo:** `brennertobe07/poll-tracker` (public — contains only public polling data)
- **Audience:** internal for now (decided 2026-09-28) — no vadems.org subdomain / Cloudflare Access yet
- **Cross-links:** header button to 2028 Watch (`intel\2028-watch`), which links back here
- **Local preview:** `python -m http.server 8765` in the repo, open http://localhost:8765
  (opening index.html from disk fails — the page fetches `data/polls.json`)

## 2. File map

```
poll-tracker/
├── CLAUDE.md             project index (short)
├── docs/REFERENCE.md     this file
├── update_polls.py       collector: Wikipedia → data/polls.json (+ --publish)
├── index.html            dashboard (single file, DPVA dark theme, no build step)
└── data/
    ├── polls.json        generated data (committed; dashboard reads it)
    └── last_run.log      scheduled-task output (gitignored)
```

## 3. Data sources (all via MediaWiki parse API)

| Data | Wikipedia article | Table used |
|---|---|---|
| Senate race list + ratings (10 raters) + per-rater seat totals | 2026 United States Senate elections | "Predictions" (+ its "Overall" row) |
| Senate current seats / seats not up | same | infobox; "Seats" table "Not up" row |
| Generic ballot aggregates | same | "Generic congressional ballot…" |
| Governor race list + ratings | 2026 United States gubernatorial elections | "Predictions" |
| Governor seats before / up | same | infobox |
| House competitive districts + ratings (9 raters) + per-rater seat totals | 2026 United States House of Representatives election ratings | first table (+ "Overall" row) |
| House current seats | 2026 United States House of Representatives elections | infobox |
| Per-race polls + published averages | race article linked from each Predictions row | poll tables with `(D)`/`(R)` headers |
| House district polls | …House of Representatives elections in {State} | per `District N` h2 section |
| House at-large | …House of Representatives **election** in {State} (singular) | whole page |
| House big states | `…in {State} (districts 1–26)` sub-articles, found via hatnote | same as above |
| Presidential approval | Opinion polling on the second Trump presidency | "Aggregator" table |

## 4. Parsing rules

- **Main matchup** = the first poll table in the article whose candidate headers
  carry party tags and include both a `(D)` and an `(R)`. Primary polls have no
  party tags, so they're skipped automatically. Later tagged tables are
  hypothetical matchups — counted (`hypothetical_tables`) but not averaged.
- **Duplicate populations** (same poll reported as RV and LV): keep one,
  preferring LV > RV > V > A.
- **Partisan pollster** = `(R)`/`(D)` in the pollster name (Wikipedia's convention).
- **Sponsor** = resolved from the row's footnote ("Poll sponsored by X").
- **House races kept** = every district on the ratings list (`on_ratings_list: true`)
  plus any other district with a D-vs-R poll (ratings from its own per-district
  "Source/Ranking" table).

## 5. Derived fields

Per race:

| Field | Meaning |
|---|---|
| `avg` | mean D−R margin of polls ending in the last 30 days; if none, the last 3 polls (`avg_basis` says which) |
| `avg_nonpartisan` | same, excluding `(R)`/`(D)` pollsters |
| `avg_by_party` | mean share per party column over the same polls |
| `multi` | true when a third candidate averages ≥15% or D+R < 70% — D−R margin is misleading there |
| `trend` | last-30-day avg minus the prior 30 days' avg (positive = moving toward D) |
| `rating_score` / `rating_consensus` | median of all raters on a −3 (Safe R) … +3 (Safe D) scale |
| `n_polls_60d`, `days_since_poll` | activity / staleness |
| `hot_score` | min(polls in 60d, 10) + max(0, 10 − abs(avg)) + rating competitiveness (Toss-up 8, Tilt 7, Lean 5, Likely 2) |

`balance` (per chamber: Senate, House, Governor):

| Field | Meaning |
|---|---|
| `current` | seats held now (infobox); House adds `vacant` |
| `base` | seats not in play: Senate/Gov seats not up; House = uncompetitive seats off the ratings list, estimated as median over raters of (rater's overall D total − rater's D-side listed seats) |
| `buckets` | seats up, by consensus rating (Tilt folds into Lean) |
| `forecast` | base + Safe/Likely/Lean per side; `toss` = consensus Toss-up |
| `polls` | base + races where the poll avg leads by ≥3; <3 = toss; unpolled or `multi` races use the consensus rating |
| `raters` | each forecaster's own seat totals (Senate/House from Wikipedia's "Overall" rows; Governor computed from the per-race columns + base) |
| `majority`, `tiebreak` | 51 (Senate, VP = R breaks 50–50), 218 (House), none (Governor) |

Top level also has `generic_ballot`, `approval`, and `warnings` (pages or
tables that failed — empty on a clean run).

Incumbency is derived in `index.html`, not the scraper: `incParty()` matches
`incumbent` to a candidate (full name, else last name + first initial,
accents/Jr./III ignored) and shows an **INC** badge; an `incumbent` ending in
`(retiring)` / `(term-limited)` / `(lost renomination)` / `(open seat)`, or `New seat` /
`Vacant`, shows "open seat" instead.

## 6. Daily workflow

Automated: Windows Task Scheduler task **`Poll_Tracker_Update`**, daily 06:15, runs

```
python update_polls.py --publish    (in C:\repos\intel\poll-tracker, output → data\last_run.log)
```

which rebuilds `data/polls.json`, commits it ("Daily poll refresh YYYY-MM-DD"),
and pushes to `main`; GitHub Pages redeploys in ~1 minute.

Safety: `--publish` refuses to push (exit 1) if the run produced fewer than 150
races or no Senate balance — yesterday's data stays live. The dashboard header
shows "data over a day old" if `generated` is more than 36 h old.

Manual:
```
cd C:\repos\intel\poll-tracker
python update_polls.py              # ~3 min, ~135 page fetches, no push
python update_polls.py --no-house   # ~1 min, Senate + Governor only (House balance empty)
python update_polls.py --publish    # same as the scheduled task
```

If the page looks stale: check `data\last_run.log` and the task's Last Run
Result in Task Scheduler. The task is set to run only while Brenner is logged
on (no stored password); switching it to "run whether user is logged on or not"
is done in the Task Scheduler GUI.

## 7. Known limits

- California top-two D-vs-D (or R-vs-R) House races have no D/R pair, so they
  don't show polls.
- Wikipedia article titles/layouts can change; a missing page shows up in
  `warnings` instead of crashing.
- Ratings are compiled by Wikipedia editors and may lag the forecasters by a day or two.
- The House "Polls" projection is mostly ratings — few districts are polled.
- House `incumbent` comes from the ratings list; for polled districts off that list it
  falls back to `section_incumbent()` (the "(incumbent)" tag in the district's
  election boxes). With no tag, it reads the district infobox's "Incumbent U.S.
  Representative"; if that member isn't a candidate there, it records
  "Name (open seat)" (e.g. CA-11, Pelosi).
