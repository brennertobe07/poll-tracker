# poll-tracker

Daily-glance dashboard of 2026 US Senate, Governor, and House polling,
forecaster ratings, and balance of power. Data is scraped from Wikipedia's 2026
election articles into `data/polls.json`; a single-file HTML dashboard reads it.
Live at https://brennertobe07.github.io/poll-tracker/ — refreshed daily 06:15
by the `Poll_Tracker_Update` scheduled task (`update_polls.py --publish`).

**Read `docs/REFERENCE.md` before non-trivial work** — file map, data shape,
parsing rules, and daily workflow live there.

## Conventions
- Best-effort, not complete: tables that don't parse are skipped, not fatal.
- Python: stdlib + requests + bs4 only (no pandas.read_html — lxml isn't installed).
- Margins are D minus R (positive = Democrat ahead) everywhere.
- Dashboard stays single-file HTML, DPVA dark theme, no build step.
- Be polite to Wikipedia: keep the fetch delay and the descriptive User-Agent.
- `--publish` is the only thing that pushes; it refuses thin runs (< 150 races).
