"""
update_polls.py — collect 2026 Senate / Governor / House polling + race ratings
from Wikipedia and write data/polls.json for the poll-tracker dashboard.

Source: Wikipedia's 2026 election articles (via the MediaWiki parse API), which
aggregate published polls and forecaster ratings. Best-effort: any table that
doesn't parse cleanly is skipped and noted in the "warnings" list.

Usage:
    python update_polls.py            # full run (~130 page fetches, ~3 min)
    python update_polls.py --no-house # skip the 50 per-state House pages
    python update_polls.py --publish  # full run, then commit + push data/polls.json
                                      # (what the daily scheduled task runs)
"""
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median

import requests
from bs4 import BeautifulSoup

API = "https://en.wikipedia.org/w/api.php"
WIKI = "https://en.wikipedia.org"
UA = {"User-Agent": "DPVA-poll-tracker/1.0 (brenner.tobe@vademocrats.org)"}
OUT = Path(__file__).parent / "data" / "polls.json"
SLEEP = 0.4            # be polite to Wikipedia
AVG_DAYS = 30          # polling-average window
ACTIVE_DAYS = 60       # "recently polled" window
TODAY = date.today()

STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
          "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
          "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
          "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
          "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
          "New Hampshire", "New Jersey", "New Mexico", "New York",
          "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
          "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
          "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
          "West Virginia", "Wisconsin", "Wyoming"]
ABBR = dict(zip(STATES, "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
                        "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
                        "SD TN TX UT VT VA WA WV WI WY".split()))

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

warnings = []
session = requests.Session()
session.headers.update(UA)


# ---------------------------------------------------------------- fetching

def fetch(page):
    """Return parsed article HTML for a Wikipedia title, or None if missing."""
    time.sleep(SLEEP)
    try:
        r = session.get(API, params={"action": "parse", "page": page, "prop": "text",
                                     "format": "json", "formatversion": 2,
                                     "redirects": 1}, timeout=45)
        j = r.json()
    except Exception as e:  # network / JSON trouble — note it and move on
        warnings.append(f"fetch failed: {page}: {e}")
        return None
    if "error" in j:
        warnings.append(f"missing page: {page}")
        return None
    return BeautifulSoup(j["parse"]["text"], "html.parser")


# ---------------------------------------------------------------- table helpers

def clean(text):
    """Strip footnote markers like [ 12 ] / [a] and normalize whitespace/dashes."""
    text = re.sub(r"\[\s*[^\]]{1,6}\s*\]", "", text)
    text = text.replace("\xa0", " ").replace("–", "-").replace("—", "-").replace("±", "")
    return re.sub(r"\s+", " ", text).strip()


def grid(table):
    """Expand a wikitable into a list of rows (lists of cells), honoring rowspan/colspan.
    Each cell is (clean_text, raw_text, bs4_cell)."""
    rows, pending = [], {}  # pending[col] = [remaining, cell]
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row, col, i = [], 0, 0
        while i < len(cells) or col in pending:
            if col in pending:
                cell = pending[col][1]
                pending[col][0] -= 1
                if pending[col][0] == 0:
                    del pending[col]
                row.append(cell)
                col += 1
                continue
            c = cells[i]
            i += 1
            raw = c.get_text(" ", strip=True)
            cell = (clean(raw), raw, c)
            span = int(re.sub(r"\D", "", c.get("colspan", "1")) or 1)
            rspan = int(re.sub(r"\D", "", c.get("rowspan", "1")) or 1)
            for _ in range(span):
                if rspan > 1:
                    pending[col] = [rspan - 1, cell]
                row.append(cell)
                col += 1
        rows.append(row)
    return rows


def section_of(el):
    h = el.find_previous(["h2", "h3", "h4"])
    return clean(h.get_text(" ", strip=True)) if h else ""


def pct(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- ratings

RATING_SCALE = {"safe": 3, "solid": 3, "likely": 2, "lean": 1, "tilt": 0.5,
                "tossup": 0, "toss-up": 0, "toss up": 0}


def rating_value(text):
    """'Lean D (flip)' -> +1 ; 'Likely R' -> -2 ; 'Tossup' -> 0. D positive."""
    t = clean(text).lower().replace("(flip)", "").strip()
    if not t:
        return None
    if t.startswith("toss"):
        return 0.0
    m = re.match(r"(safe|solid|likely|lean|tilt)\s+(d|r)\b", t)
    if not m:
        return None
    v = RATING_SCALE[m.group(1)]
    return float(v if m.group(2) == "d" else -v)


def rating_label(v):
    if v is None:
        return None
    a, side = abs(v), ("D" if v > 0 else "R")
    if a < 0.25:
        return "Toss-up"
    if a < 0.75:
        return f"Tilt {side}"
    if a < 1.5:
        return f"Lean {side}"
    if a < 2.5:
        return f"Likely {side}"
    return f"Safe {side}"


def rater_name(header):
    """'Cook Sep 23, 2026 [28]' -> 'Cook'."""
    h = clean(header)
    m = re.split(r"\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", h, maxsplit=1)
    return m[0].strip().rstrip(".")


def parse_overall(text):
    """'D/I - 46 R - 47 7 tossups' -> {'D': 46, 'R': 47, 'toss': 7}."""
    t = clean(text)
    d = re.search(r"D(?:/I)?\s*-\s*(\d+)", t)
    r = re.search(r"\bR\s*-\s*(\d+)", t)
    toss = re.search(r"(\d+)\s*toss", t, re.I)
    if not d or not r:
        return None
    return {"D": int(d.group(1)), "R": int(r.group(1)), "toss": int(toss.group(1)) if toss else 0}


def parse_ratings_table(soup, table=None):
    """National ratings table (Senate/Governor 'Predictions', or the House election-ratings
    article) -> (race stubs, {rater: overall seat projection})."""
    if table is None:
        for t in soup.select("table.wikitable"):
            if section_of(t).startswith("Predictions"):
                table = t
                break
    if table is None:
        warnings.append("no Predictions table found")
        return [], {}
    rows = grid(table)
    hdr = [c[0] for c in rows[1]]
    try:
        first_rater = next(i for i, h in enumerate(hdr) if re.search(r"\b(19|20)\d\d\b", h)
                           and not h.startswith("Last"))
    except StopIteration:
        warnings.append("ratings table: no rater columns")
        return [], {}
    raters = [rater_name(h) for h in hdr[first_rater:]]
    races, overall = [], {}
    for r in rows[2:]:
        name = r[0][0]
        if name.startswith("Overall"):
            for k, rater in enumerate(raters):
                if first_rater + k < len(r):
                    o = parse_overall(r[first_rater + k][0])
                    if o:
                        overall[rater] = o
            continue
        if not name or len(r) < len(hdr):
            continue
        a = r[0][2].find("a", href=re.compile(r"^/wiki/2026"))
        ratings = {raters[k]: r[first_rater + k][0].replace("(flip)", "").strip()
                   for k in range(len(raters)) if r[first_rater + k][0]}
        state = name.replace("(special)", "").strip()
        races.append({
            "state": state,
            "special": "special" in name.lower(),
            "pvi": r[1][0],
            "incumbent": r[2][0],
            "last_election": r[3][0],
            "ratings": ratings,
            "url": WIKI + a["href"] if a else None,
            "page": a["href"].split("/wiki/")[1].replace("_", " ") if a else None,
        })
    return races, overall


def parse_infobox(soup):
    """Election infobox -> {party letter: {'current'|'before'|'up': n}}."""
    ib = soup.select_one("table.infobox")
    out, parties = {}, []
    if ib is None:
        return out
    for tr in ib.find_all("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if not cells or len(cells) > 4:   # skip the outer wrapper row that holds everything
            continue
        label, vals = cells[0], cells[1:]
        if label == "Party":
            parties = [v[:1] if v else None for v in vals]   # Republican -> R, Democratic -> D
            continue
        key = {"Current seats": "current", "Seats before": "before", "Seats up": "up"}.get(label)
        if key:
            for p, v in zip(parties, vals):
                n = re.match(r"(\d+)", v or "")
                if p and n:
                    out.setdefault(p, {})[key] = int(n.group(1))
    return out


def senate_not_up(soup):
    """'Seats' summary table, 'Not up' row -> {'D':, 'I':, 'R':}."""
    for t in soup.select("table.wikitable"):
        if not section_of(t).startswith("Seats"):
            continue
        rows = grid(t)
        hdr = [c[0] for c in rows[1]] if len(rows) > 1 else []
        for r in rows:
            if r[0][0] == "Not up":
                out = {}
                for i, h in enumerate(hdr):
                    n = re.match(r"(\d+)", r[i][0]) if i < len(r) else None
                    if n and h[:1] in "DIR" and h in ("Democratic", "Independent", "Republican"):
                        out[h[:1]] = int(n.group(1))
                return out
    warnings.append("senate: 'Not up' row not found")
    return {}


def parse_source_rankings(soup_or_nodes):
    """Per-race 'Source | Ranking | As of' tables -> {source: rating}."""
    out = {}
    for t in soup_or_nodes:
        rows = grid(t)
        if not rows or [c[0] for c in rows[0]][:2] != ["Source", "Ranking"]:
            continue
        for r in rows[1:]:
            if len(r) >= 2 and r[0][0]:
                src = re.sub(r"^The\s+", "", r[0][0])
                src = {"Cook Political Report": "Cook", "Sabato's Crystal Ball": "Sabato",
                       "Inside Elections": "IE", "Decision Desk HQ": "DDHQ"}.get(src, src)
                out[src] = r[1][0].replace("(flip)", "").strip()
    return out


def consensus(ratings):
    vals = [v for v in (rating_value(x) for x in ratings.values()) if v is not None]
    if not vals:
        return None, None
    v = median(vals)
    return round(v, 2), rating_label(v)


# ---------------------------------------------------------------- polls

def parse_date(text):
    """End date of a fieldwork range: 'September 17-20, 2026', 'Aug 30 - Sep 2, 2026',
    'December 28, 2025 - January 3, 2026'. Returns ISO date string or None."""
    t = clean(text)
    years = re.findall(r"\b(20\d\d)\b", t)
    if not years:
        return None
    year = int(years[-1])
    end = re.split(r"\s*-\s*", t)[-1]
    months = re.findall(r"\b([A-Za-z]{3})[a-z]*\.?", t)
    months = [m.lower() for m in months if m.lower() in MONTHS]
    end_months = [m.lower() for m in re.findall(r"\b([A-Za-z]{3})[a-z]*\.?", end)
                  if m.lower() in MONTHS]
    month = MONTHS[(end_months or months or [None])[-1]] if (end_months or months) else None
    days = re.findall(r"\b(\d{1,2})\b", re.sub(r"\b20\d\d\b", "", end))
    if not month or not days:
        return None
    try:
        return date(year, month, int(days[-1])).isoformat()
    except ValueError:
        return None


POP_RANK = {"LV": 0, "RV": 1, "V": 2, "A": 3}
SPONSOR_RE = re.compile(r"(?:sponsored by|commissioned by|conducted for|conducted on behalf of)\s+(.+)", re.I)


def sponsor_notes(table):
    """{footnote id: sponsor} for a page's 'Poll sponsored by X' notes."""
    root = table
    while root.parent is not None:
        root = root.parent
    cache = getattr(root, "_sponsors", None)
    if cache is None:
        cache = {}
        for li in root.find_all("li", id=re.compile(r"^cite_note-")):
            m = SPONSOR_RE.search(clean(li.get_text(" ", strip=True)))
            if m:
                cache[li["id"]] = re.split(r",\s|;", m.group(1))[0].rstrip(". ")[:60]
        root._sponsors = cache
    return cache


def poll_sponsor(cell, notes):
    for a in cell.select("sup a[href^='#cite_note-']"):
        sp = notes.get(a["href"][1:])
        if sp:
            return sp
    return None


def parse_poll_table(table):
    """General-election poll table with party-tagged candidate columns.
    Returns (candidates{party: name}, polls[]) or None if not a D-vs-R table."""
    rows = grid(table)
    if len(rows) < 2:
        return None
    hdr = [c[0] for c in rows[0]]
    if not hdr or not hdr[0].startswith("Poll source"):
        return None
    try:
        moe_i = next(i for i, h in enumerate(hdr) if h.startswith("Margin"))
    except StopIteration:
        return None
    cand_cols = {}
    for i, h in enumerate(hdr[moe_i + 1:], moe_i + 1):
        m = re.search(r"^(.*?)\s*\((D|R|I|L|G|DFL)\)$", h)
        if m:
            party = {"DFL": "D"}.get(m.group(2), m.group(2))
            cand_cols.setdefault(party, (i, m.group(1).strip()))
    if "D" not in cand_cols or "R" not in cand_cols:
        return None
    # Independents running with Dem support (e.g. Osborn in NE) — treat first I as the
    # anti-R candidate only when there is no D column; handled above by requiring D.
    other_i = [i for i, h in enumerate(hdr) if h in ("Other",)]
    und_i = [i for i, h in enumerate(hdr) if h.startswith("Undecided")]

    notes = sponsor_notes(table)
    polls, prev_src_cell = [], None
    for r in rows[1:]:
        if len(r) < len(hdr):
            continue
        src_cell = r[0][2]
        pollster = r[0][0]
        if not pollster or pollster.lower().startswith("poll source"):
            continue
        d, rep = pct(r[cand_cols["D"][0]][0]), pct(r[cand_cols["R"][0]][0])
        if d is None or rep is None:
            continue
        samp = r[2][0]
        n = re.search(r"([\d,]+)", samp)
        pop = re.search(r"\((LV|RV|A|V)\)", samp)
        moe = re.search(r"(\d+(?:\.\d+)?)", r[moe_i][0])
        poll = {
            "pollster": pollster,
            "partisan": (re.search(r"\((R|D)\)", pollster) or [None, None])[1],
            "sponsor": poll_sponsor(src_cell, notes),
            "dates": r[1][0],
            "end": parse_date(r[1][0]),
            "n": int(n.group(1).replace(",", "")) if n else None,
            "pop": pop.group(1) if pop else None,
            "moe": float(moe.group(1)) if moe else None,
            "d": d, "r": rep,
            "other": pct(r[other_i[0]][0]) if other_i else None,
            "undecided": pct(r[und_i[0]][0]) if und_i else None,
            "margin": round(d - rep, 1),
            "cands": {p: pct(r[i][0]) for p, (i, _) in cand_cols.items()},
        }
        poll["pollster"] = re.sub(r"\s*/\s*", " / ", poll["pollster"])
        # Same poll reported for several populations (rowspan'd source cell):
        # keep the preferred population (LV > RV > V > A).
        same = polls and (src_cell is prev_src_cell or
                          (poll["pollster"] == polls[-1]["pollster"] and r[1][0] == polls[-1]["dates"]))
        if same:
            last = polls[-1]
            if POP_RANK.get(poll["pop"], 9) < POP_RANK.get(last["pop"], 9):
                polls[-1] = poll
            continue
        prev_src_cell = src_cell
        polls.append(poll)
    cands = {p: name for p, (_, name) in cand_cols.items()}
    return cands, polls


def parse_aggregates(tables):
    """'Source of poll aggregation' tables -> [{source, updated, margin_text}]."""
    out = []
    for t in tables:
        rows = grid(t)
        if not rows or not rows[0][0][0].startswith("Source of poll aggregation"):
            continue
        hdr = [c[0] for c in rows[0]]
        if not any("(D)" in h for h in hdr) or not any("(R)" in h for h in hdr):
            continue  # primary aggregates
        for r in rows[1:]:
            if len(r) < 3 or not r[0][0]:
                continue
            out.append({"source": r[0][0], "updated": r[2][0],
                        "margin_text": r[-1][0]})
        break
    return out


def race_from_tables(tables):
    """Pick the main general-election matchup (first D-vs-R poll table) plus count
    of hypothetical matchups. Returns dict fields to merge into the race."""
    main, hypo = None, 0
    for t in tables:
        res = parse_poll_table(t)
        if not res:
            continue
        if main is None:
            main = res
        else:
            hypo += 1
    cands, polls = main if main else ({}, [])
    return {"candidates": cands, "polls": polls, "hypothetical_tables": hypo,
            "aggregates": parse_aggregates(tables)}


def summarize(race):
    """Add polling average, activity counts, trend and freshness fields."""
    polls = [p for p in race["polls"] if p["end"]]
    polls.sort(key=lambda p: p["end"], reverse=True)
    race["polls"] = polls + [p for p in race["polls"] if not p["end"]]
    cut_avg = (TODAY - timedelta(days=AVG_DAYS)).isoformat()
    cut_act = (TODAY - timedelta(days=ACTIVE_DAYS)).isoformat()
    recent = [p for p in polls if p["end"] >= cut_avg]
    # Fall back to the 3 most recent polls when nothing landed in the window.
    basis = recent or polls[:3]
    nonpart = [p for p in basis if not p["partisan"]]
    race["avg"] = round(mean(p["margin"] for p in basis), 1) if basis else None
    race["avg_nonpartisan"] = round(mean(p["margin"] for p in nonpart), 1) if nonpart else None
    race["avg_basis"] = (f"{len(recent)} polls, last {AVG_DAYS}d" if recent
                         else (f"last {len(basis)} polls (none in {AVG_DAYS}d)" if basis else None))
    # Multi-candidate races (strong independent/third party): D-R margin alone misleads,
    # so also carry each party's average share.
    by_party = {}
    for p in basis:
        for party, v in p.get("cands", {}).items():
            if v is not None:
                by_party.setdefault(party, []).append(v)
    race["avg_by_party"] = {k: round(mean(v), 1) for k, v in by_party.items()}
    ap = race["avg_by_party"]
    race["multi"] = (any(v >= 15 for k, v in ap.items() if k not in ("D", "R"))
                     or (bool(ap) and ap.get("D", 0) + ap.get("R", 0) < 70))
    race["n_polls"] = len(race["polls"])
    race["n_polls_60d"] = sum(1 for p in polls if p["end"] >= cut_act)
    race["last_poll"] = polls[0]["end"] if polls else None
    race["days_since_poll"] = (TODAY - date.fromisoformat(polls[0]["end"])).days if polls else None
    # Trend: last-30d average vs the 30 days before that.
    prior = [p for p in polls if (TODAY - timedelta(days=2 * AVG_DAYS)).isoformat()
             <= p["end"] < cut_avg]
    race["trend"] = (round(mean(p["margin"] for p in recent) - mean(p["margin"] for p in prior), 1)
                     if recent and prior else None)
    race["rating_score"], race["rating_consensus"] = consensus(race.get("ratings", {}))
    # Hot score: activity + closeness + forecaster competitiveness.
    act = min(race["n_polls_60d"], 10)
    close = max(0.0, 10 - abs(race["avg"])) if race["avg"] is not None else 0
    comp = {0: 8, 0.5: 7, 1: 5, 1.5: 4, 2: 2}.get(abs(race["rating_score"] or 3), 0) \
        if race["rating_score"] is not None else 0
    if race["rating_score"] is not None and abs(race["rating_score"]) < 2.5 and comp == 0:
        comp = 3
    race["hot_score"] = round(act + close + comp, 1)
    return race


# ---------------------------------------------------------------- collectors

def collect_statewide(national_page, office):
    """Returns (races, national-page soup, {rater: overall projection})."""
    soup = fetch(national_page)
    if soup is None:
        return [], None, {}
    races = []
    stubs, overall = parse_ratings_table(soup)
    for stub in stubs:
        if not stub["page"]:
            warnings.append(f"{office} {stub['state']}: no race link")
            continue
        page = fetch(stub["page"])
        tables = page.select("table.wikitable") if page else []
        race = {"office": office, "district": None, **stub, **race_from_tables(tables)}
        abbr = ABBR.get(stub["state"], stub["state"][:2].upper())
        race["id"] = f"{abbr}-{'SEN' if office == 'Senate' else 'GOV'}" + ("-S" if stub["special"] else "")
        race["label"] = f"{stub['state']}{' (special)' if stub['special'] else ''}"
        races.append(summarize(race))
        print(f"  {office:8} {race['label']:22} polls={race['n_polls']:3} "
              f"avg={race['avg']} rating={race['rating_consensus']}")
    return races, soup, overall


def collect_generic_ballot(soup):
    out = []
    if soup is None:
        return out
    for t in soup.select("table.wikitable"):
        if not section_of(t).startswith("Generic congressional ballot"):
            continue
        rows = grid(t)
        hdr = [c[0] for c in rows[0]]
        ri = next((i for i, h in enumerate(hdr) if h.startswith("Republican")), None)
        di = next((i for i, h in enumerate(hdr) if h.startswith("Democrat")), None)
        for r in rows[1:]:
            if len(r) < len(hdr) or ri is None:
                continue
            out.append({"source": r[0][0], "updated": r[2][0],
                        "r": pct(r[ri][0]), "d": pct(r[di][0]),
                        "margin_text": r[-1][0]})
        break
    return out


def house_pages(state):
    """Soups for a state's House article(s). At-large states use the singular title;
    big states (e.g. California) split districts into '(districts 1-26)' sub-articles."""
    base = f"2026 United States House of Representatives elections in {state}"
    soup = fetch(base)
    if soup is None:
        n = len(warnings)
        soup = fetch(f"2026 United States House of Representatives election in {state}")
        if soup is not None:
            del warnings[n - 1:]  # plural title missing is expected for at-large states
        return [(base.replace("elections in", "election in"), soup)] if soup is not None else []
    soups = [(base, soup)]
    prefix = "/wiki/" + base.replace(" ", "_") + "_(districts"
    subs = {a["href"] for a in soup.select("div.hatnote a[href]") if a["href"].startswith(prefix)}
    for href in sorted(subs):
        title = requests.utils.unquote(href.split("/wiki/")[1]).replace("_", " ")
        sub = fetch(title)
        if sub is not None:
            soups.append((title, sub))
    return soups


def collect_approval():
    """Presidential job approval from the aggregator table (Silver Bulletin, RCP, etc.)."""
    soup = fetch("Opinion polling on the second Trump presidency")
    if soup is None:
        return []
    for t in soup.select("table.wikitable"):
        rows = grid(t)
        if rows and rows[0][0][0] == "Aggregator":
            return [{"source": r[0][0], "updated": r[1][0], "approve": pct(r[2][0]),
                     "disapprove": pct(r[3][0])}
                    for r in rows[1:] if len(r) >= 4 and pct(r[2][0]) is not None]
    warnings.append("approval: aggregator table not found")
    return []


HOUSE_RATINGS_PAGE = "2026 United States House of Representatives election ratings"


def house_ratings():
    """Dedicated House ratings article -> ({race id: stub}, {rater: overall projection})."""
    soup = fetch(HOUSE_RATINGS_PAGE)
    if soup is None:
        return {}, {}
    table = next((t for t in soup.select("table.wikitable")
                  if len(grid(t)) > 1 and grid(t)[1][0][0] == "District"), None)
    if table is None:
        warnings.append("house ratings: table not found")
        return {}, {}
    stubs, overall = parse_ratings_table(soup, table)
    out = {}
    for st in stubs:
        m = re.match(r"(.+?)\s+(\d+|at-large)$", st["state"], re.I)
        if not m or m.group(1) not in ABBR:
            warnings.append(f"house ratings: can't parse district '{st['state']}'")
            continue
        state, dist = m.group(1), (m.group(2) if m.group(2).isdigit() else "AL")
        rid = f"{ABBR[state]}-{dist.zfill(2) if dist != 'AL' else 'AL'}"
        out[rid] = {**st, "state": state, "district": dist}
    return out, overall


def collect_house():
    """Districts with polls (per-state articles) merged with every district on the
    House ratings list. Returns (races, overall projections by rater)."""
    races = {}
    for state in STATES:
        for title, soup in house_pages(state):
            for r in house_races(state, title, soup):
                races[r["id"]] = r
        print(f"  House    {state:22} polled districts={sum(1 for r in races.values() if r['state'] == state)}")
    rated, overall = house_ratings()
    for rid, st in rated.items():
        abbr = ABBR[st["state"]]
        race = races.get(rid) or {
            "office": "House", "state": st["state"], "district": st["district"], "special": False,
            "id": rid, "label": f"{abbr}-{st['district']}",
            "url": st["url"] or f"{WIKI}/wiki/{HOUSE_RATINGS_PAGE.replace(' ', '_')}",
            "candidates": {}, "polls": [], "hypothetical_tables": 0, "aggregates": []}
        race.update({"ratings": st["ratings"], "pvi": st["pvi"],
                     "incumbent": st["incumbent"] or race.get("incumbent"),
                     "last_election": st["last_election"], "on_ratings_list": True})
        races[rid] = race
    print(f"  House    ratings list: {len(rated)} districts, {len(races)} total kept")
    return [summarize(r) for r in races.values()], overall


def section_incumbent(tables):
    """Sitting member from a district's election boxes (Party | Party | Candidate | Votes),
    which tag them "(incumbent)". Found in the last box (the general, or the primary if no
    general box yet) -> name; only in an earlier primary box -> lost renomination; no tag
    anywhere -> None (retiring/open seats don't carry the tag, so leave it unknown)."""
    boxes = [g for g in map(grid, tables)
             if len(g) > 1 and len(g[0]) > 2 and g[0][0][0] == "Party" and g[0][2][0] == "Candidate"]
    for i, g in enumerate(reversed(boxes)):
        for r in g[1:]:
            if len(r) > 2 and "(incumbent)" in r[2][0]:
                name = clean(r[2][0].replace("(incumbent)", ""))
                return name if i == 0 else f"{name} (lost renomination)"
    return None


def house_races(state, title, soup):
    """Districts in one state article that have D-vs-R general-election polls."""
    races = []
    # Split the article into district sections at each h2 "District N" / "At-large".
    heads = [h for h in soup.find_all("h2")
             if re.match(r"(District \d+|At-large|At large)", clean(h.get_text(" ", strip=True)))]
    if not heads:  # at-large states often have no district headings
        heads = [None]
    for idx, h in enumerate(heads):
        if h is None:
            tables = soup.select("table.wikitable")
            dist = "AL"
        else:
            nxt = heads[idx + 1] if idx + 1 < len(heads) else None
            tables = []
            # h2 is wrapped in div.mw-heading in current parser output
            start = h.parent if h.parent.name == "div" else h
            stop = (nxt.parent if nxt is not None and nxt.parent.name == "div" else nxt)
            for el in start.find_all_next():
                if el is stop:
                    break
                if el.name == "table" and "wikitable" in (el.get("class") or []):
                    tables.append(el)
            m = re.search(r"\d+", clean(h.get_text()))
            dist = m.group(0) if m else "AL"
        fields = race_from_tables(tables)
        if not fields["polls"]:
            continue
        abbr = ABBR[state]
        races.append({"office": "House", "state": state, "district": dist, "special": False,
                      "id": f"{abbr}-{dist.zfill(2) if dist != 'AL' else 'AL'}",
                      "label": f"{abbr}-{dist}", "pvi": None,
                      # ratings-list value overrides this in collect_house when present
                      "incumbent": section_incumbent(tables),
                      # per-district ratings: fallback for polled seats off the ratings list
                      "ratings": parse_source_rankings(tables),
                      "on_ratings_list": False,
                      "url": f"{WIKI}/wiki/{title.replace(' ', '_')}"
                             + (f"#District_{dist}" if dist != "AL" else ""),
                      **fields})
    return races


# ---------------------------------------------------------------- balance of power

VP_PARTY = "R"   # breaks 50-50 Senate ties (JD Vance)
BUCKETS = ["safe_d", "likely_d", "lean_d", "tossup", "lean_r", "likely_r", "safe_r", "unrated"]


def bucket(score):
    """Consensus score -> bucket. Tilt folds into Lean."""
    if score is None:
        return "unrated"
    a, side = abs(score), ("d" if score > 0 else "r")
    if a < 0.25:
        return "tossup"
    return f"{'lean' if a < 1.5 else 'likely' if a < 2.5 else 'safe'}_{side}"


def poll_side(r):
    """Poll lead of 3+ decides; under 3 = toss-up; unpolled or multi-candidate races
    fall back to the forecaster consensus."""
    if r["avg"] is not None and not r["multi"]:
        return "D" if r["avg"] >= 3 else "R" if r["avg"] <= -3 else "toss"
    s = r["rating_score"]
    if s is None:
        return "unrated"
    return "D" if s >= 0.25 else "R" if s <= -0.25 else "toss"


def rater_side(text):
    v = rating_value(text)
    return None if v is None else "D" if v > 0 else "R" if v < 0 else "toss"


def tally(races, base_d, base_r):
    b = {k: 0 for k in BUCKETS}
    for r in races:
        b[bucket(r["rating_score"])] += 1
    pol = {"D": base_d, "R": base_r, "toss": 0, "unrated": 0}
    for r in races:
        pol[poll_side(r)] += 1
    return {
        "buckets": b,
        "forecast": {"D": base_d + b["safe_d"] + b["likely_d"] + b["lean_d"],
                     "R": base_r + b["safe_r"] + b["likely_r"] + b["lean_r"],
                     "toss": b["tossup"], "unrated": b["unrated"]},
        "polls": pol,
    }


def build_balance(sen_soup, gov_soup, senate, gov, house, overall):
    bal = {}
    if sen_soup is not None:
        ib, nu = parse_infobox(sen_soup), senate_not_up(sen_soup)
        cur = {p: v.get("current") for p, v in ib.items() if v.get("current") is not None}
        base_d, base_r = nu.get("D", 0) + nu.get("I", 0), nu.get("R", 0)
        bal["Senate"] = {"seats": 100, "majority": 51, "tiebreak": VP_PARTY, "current": cur,
                         "base": {"D": base_d, "R": base_r}, "base_label": "not up in 2026",
                         "d_label": "D/I", "up": len(senate), **tally(senate, base_d, base_r),
                         "raters": overall.get("Senate", {})}
    if gov_soup is not None:
        ib = parse_infobox(gov_soup)
        cur = {p: v.get("before") for p, v in ib.items() if v.get("before") is not None}
        base = {p: v.get("before", 0) - v.get("up", 0) for p, v in ib.items()}
        raters = {}
        for r in gov:
            for name, txt in r["ratings"].items():
                side = rater_side(txt)
                if side:
                    t = raters.setdefault(name, {"D": base.get("D", 0), "R": base.get("R", 0), "toss": 0})
                    t[side] += 1
        bal["Governor"] = {"seats": 50, "majority": None, "current": cur,
                           "base": {"D": base.get("D", 0), "R": base.get("R", 0)},
                           "base_label": "not up in 2026", "d_label": "D", "up": len(gov),
                           **tally(gov, base.get("D", 0), base.get("R", 0)), "raters": raters}
    if house:
        hsoup = fetch("2026 United States House of Representatives elections")
        ib = parse_infobox(hsoup) if hsoup is not None else {}
        cur = {p: v.get("current") for p, v in ib.items() if v.get("current") is not None}
        cur["vacant"] = 435 - sum(cur.values())
        listed = [r for r in house if r.get("on_ratings_list")]
        hov = overall.get("House", {})
        # Seats off the ratings list are uncompetitive; back their party split out of each
        # rater's overall total (overall D minus that rater's D-side listed seats), take the median.
        outs_d, outs_r = [], []
        for name, o in hov.items():
            sides = [rater_side(r["ratings"].get(name, "")) for r in listed]
            outs_d.append(o["D"] - sides.count("D"))
            outs_r.append(o["R"] - sides.count("R"))
        base_d = int(median(outs_d)) if outs_d else 0
        base_r = int(median(outs_r)) if outs_r else 0
        if base_d + base_r + len(listed) != 435:
            warnings.append(f"house balance: {base_d}+{base_r}+{len(listed)} listed != 435")
        bal["House"] = {"seats": 435, "majority": 218, "current": cur,
                        "base": {"D": base_d, "R": base_r}, "base_label": "safe seats off the ratings list",
                        "d_label": "D", "up": len(listed), **tally(listed, base_d, base_r), "raters": hov}
    return bal


MIN_RACES = 150   # a full run has ~230; fewer means Wikipedia fetches broke — don't publish it


def publish(data):
    """Commit data/polls.json and push to GitHub (Pages serves it). Returns exit code."""
    import subprocess
    if len(data["races"]) < MIN_RACES or not data["balance"].get("Senate"):
        print(f"NOT publishing: only {len(data['races'])} races / missing balance — keeping yesterday's data live")
        return 1
    repo = Path(__file__).parent

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    git("add", "data/polls.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("No data changes to publish.")
        return 0
    c = git("commit", "-m", f"Daily poll refresh {data['as_of']}")
    p = git("push", "origin", "main")
    print(c.stdout.strip().splitlines()[0] if c.stdout else c.stderr.strip())
    print("Pushed." if p.returncode == 0 else f"PUSH FAILED: {p.stderr.strip()}")
    return p.returncode


def main():
    skip_house = "--no-house" in sys.argv
    started = datetime.now()
    print("Senate…")
    senate, sen_soup, sen_overall = collect_statewide("2026 United States Senate elections", "Senate")
    print("Governor…")
    gov, gov_soup, _ = collect_statewide("2026 United States gubernatorial elections", "Governor")
    house, house_overall = [], {}
    if not skip_house:
        print("House…")
        house, house_overall = collect_house()
    data = {
        "generated": started.isoformat(timespec="seconds"),
        "as_of": TODAY.isoformat(),
        "settings": {"avg_days": AVG_DAYS, "active_days": ACTIVE_DAYS},
        "source": "Wikipedia 2026 election articles (polls and forecaster ratings as compiled there)",
        "generic_ballot": collect_generic_ballot(sen_soup),
        "approval": collect_approval(),
        "balance": build_balance(sen_soup, gov_soup, senate, gov, house,
                                 {"Senate": sen_overall, "House": house_overall}),
        "races": senate + gov + house,
        "warnings": warnings,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    polled = sum(1 for r in data["races"] if r["n_polls"])
    print(f"\nWrote {OUT} — {len(data['races'])} races ({polled} with polls), "
          f"{len(warnings)} warnings, {(datetime.now() - started).seconds}s")
    for w in warnings:
        print("  warning:", w)
    if "--publish" in sys.argv:
        sys.exit(publish(data))


if __name__ == "__main__":
    main()
