#!/usr/bin/env python3
"""
fetch_fixtures.py

Pulls match fixtures/results from Wikipedia tournament pages and writes
them into fixtures.json, which matches.html reads to show games for a
selected day.

Wikipedia doesn't use one consistent match layout across pages, so thisthisthis
supports three parsing strategies, selected per league via the "parser"
key in LEAGUES:

  - "vevent"    : tournament bracket pages using the hCard/vevent match-box
                  layout (team name in .fn, venue in .location, referee in
                  .attendee) - e.g. Rugby World Cup / Championship pages.
  - "wikitable" : plain results tables (class="wikitable") with a header
                  row naming columns like Home/Away/Score/Venue/Referee -
                  handles rowspan cells (shared date/venue across rows)
                  and works regardless of column order.
  - "fivb_template": FIVB Volleyball Nations League pages, which use a
                  bespoke {{Vb res 12|...}} template with 3-letter country
                  codes rather than any table/microdata markup - parsed
                  from wikitext directly rather than rendered HTML.
  - "cfl_schedule": CFL team-season pages (e.g. "2026 Ottawa Redblacks
                  season"), which list one team's full schedule as
                  "vs./at. Opponent" rows rather than naming both teams.
                  There's no single Wikipedia page with every CFL match,
                  so this parser is fed one page per team (via the
                  "team_pages" config key) and only keeps each team's
                  home ("vs.") rows, so every match is captured exactly
                  once even though it appears on two different pages.

A league can fetch from more than one Wikipedia page (e.g. a tournament
split into "Southern Hemisphere Series" / "Northern Hemisphere Series"
articles) by setting "pages": [...] instead of "page": "..." in its
LEAGUES entry; results from each page are merged under the same league
key.

Usage:
    python3 fetch_fixtures.py                 # fetch every configured league
    python3 fetch_fixtures.py u20-jwc-2026     # fetch just one league
    python3 fetch_fixtures.py --list           # show configured league keys

Requires: beautifulsoup4
    pip install beautifulsoup4 --break-system-packages
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

OUTPUT_FILE = Path(__file__).parent / "fixtures.json"
print(OUTPUT_FILE)
API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "fixtures-fetcher/1.0 (personal project; contact: cmcnabnay)"}

# Minimum gap between consecutive Wikipedia API requests, and retry/backoff
# settings for when a burst of requests (e.g. CFL's 9 team pages) still
# trips the rate limiter and gets a 429 back.
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 5
_last_request_time = 0.0


def _throttled_urlopen(req):
    """urlopen wrapped with a minimum delay between calls and retry/backoff
    on HTTP 429 (Too Many Requests), so fetching many pages back-to-back
    (e.g. CFL's 9 team-season pages) doesn't just fail outright."""
    global _last_request_time
    for attempt in range(MAX_RETRIES):
        wait = REQUEST_DELAY_SECONDS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = urlopen(req, timeout=30)
            _last_request_time = time.monotonic()
            return resp
        except HTTPError as e:
            _last_request_time = time.monotonic()
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                backoff = float(retry_after) if retry_after else (2 ** attempt) * 2
                print(f"  ... rate limited (429), retrying in {backoff:.0f}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise

# ---------------------------------------------------------------------------
# Add new tournaments here. `page` must be the exact Wikipedia article title
# (the part after /wiki/ in the URL, spaces as underscores). `parser` picks
# which strategy above to use; `year` is needed by the "wikitable" and
# "fivb_template" parsers when the page's date columns omit the year.
# ---------------------------------------------------------------------------
LEAGUES = {
    "u20-jwc-2026": {
        "name": "World Rugby Junior World Championship 2026",
        "page": "2026_World_Rugby_Junior_World_Championship",
        "sport": "rugby",
        "parser": "vevent",
        "utc_offset": 4,  # Georgia (GET), no DST
    },
    "nations-championship-2026": {
        "name": "Rugby Nations Championship 2026",
        # The old single "2026_Nations_Championship" page only had bare
        # date/score rows with no kickoff time. The tournament is actually
        # documented as two separate articles, each using the rugbybox
        # match-box layout (same as the JWC page below), which does carry
        # a kickoff time per match.
        "pages": [
            "2026_Nations_Championship_Southern_Hemisphere_Series",
            "2026_Nations_Championship_Northern_Hemisphere_Series",
        ],
        "sport": "rugby",
        "parser": "rugbybox",
        # no utc_offset needed - each match states its own UTC offset directly
    },
    "nations-cup-2026": {
        "name": "World Rugby Nations Cup 2026",
        # Same story as Nations Championship above: the real per-match
        # kickoff times live on the two regional-series pages, not the
        # old bare summary page.
        "pages": [
            "2026_World_Rugby_Nations_Cup_Americas-Pacific_Series",
            "2026_World_Rugby_Nations_Cup_European-African-Asian_Series",
        ],
        "sport": "rugby",
        "parser": "rugbybox",
        # no utc_offset needed - each match states its own UTC offset directly
    },
    "nrl-2026": {
        "name": "NRL 2026",
        "page": "2026_NRL_season_results",
        "sport": "rugby-league",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 10,  # AEST; most home cities are east-coast Australia.
                           # Simplification: doesn't account for AEDT during
                           # Oct-Apr, or NZ Warriors' home games (Auckland,
                           # UTC+12/+13) - overridden via venue lookup below.
    },
    "super-league-2026": {
        "name": "Super League Rugby 2026",
        "page": "2026_Super_League_season_results",
        "sport": "rugby-league",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 1,  # UK (BST); Catalans Dragons/Toulouse Olympique
                          # home games (France, also UTC+2 in summer) are
                          # close enough not to need an override here
    },
    "afle-2026": {
        "name": "American Football League Europe 2026",
        "page": "2026_American_Football_League_Europe_season",
        "sport": "american-football",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 2,  # Central European Summer Time
    },
    "efa-2026": {
        "name": "European Football Alliance 2026",
        "page": "2026_European_Football_Alliance_season",
        "sport": "american-football",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 2,  # Central European Summer Time
    },
    "fivb-nations-league-2026": {
        "name": "FIVB Men's Volleyball Nations League 2026",
        "page": "2026_FIVB_Men's_Volleyball_Nations_League",
        "sport": "volleyball",
        "parser": "fivb_template",
        "year": 2026,
        # no single utc_offset - each pool states its own timezone in the
        # wikitext ("All times are ... (UTC-04:00)"), parsed per-match
    },
    "cfl-2026": {
        "name": "CFL 2026",
        "sport": "canadian-football",
        "parser": "cfl_schedule",
        "year": 2026,
        # Kickoff times on these pages carry their own zone abbreviation
        # (EDT/CDT/MDT/PDT), which the parser reads per-row, so this is
        # only a fallback for the rare row where that's missing.
        "utc_offset": -4,
        # No single Wikipedia page lists every CFL game, so this is
        # fetched from each team's own season page instead. Only that
        # team's home ("vs.") rows are kept from each page, so every
        # match ends up in the list exactly once.
        "team_pages": {
            "2026_Saskatchewan_Roughriders_season": "Saskatchewan Roughriders",
            "2026_Edmonton_Elks_season": "Edmonton Elks",
            "2026_Calgary_Stampeders_season": "Calgary Stampeders",
            "2026_Winnipeg_Blue_Bombers_season": "Winnipeg Blue Bombers",
            "2026_BC_Lions_season": "BC Lions",
            "2026_Montreal_Alouettes_season": "Montreal Alouettes",
            "2026_Hamilton_Tiger-Cats_season": "Hamilton Tiger-Cats",
            "2026_Toronto_Argonauts_season": "Toronto Argonauts",
            "2026_Ottawa_Redblacks_season": "Ottawa Redblacks",
        },
    },
    "fiba-wcq-africa-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Africa",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Africa)",
        "sport": "basketball",
        "parser": "basketballbox",
        # No single utc_offset - each match's "location" field names a host
        # country, looked up in COUNTRY_UTC_OFFSETS per-match instead.
    },
    "fiba-wcq-americas-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Americas",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Americas)",
        "sport": "basketball",
        "parser": "basketballbox",
    },
    "fiba-wcq-asia-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Asia",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Asia)",
        "sport": "basketball",
        "parser": "basketballbox",
    },
    "fiba-wcq-europe-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Europe",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Europe)",
        "sport": "basketball",
        "parser": "basketballbox",
    },
}

# Keyword -> UTC offset (hours), checked against a match's venue text to
# override a league's default utc_offset for tournaments/rounds hosted in
# a different country than usual (e.g. Nations Championship games, or
# Catalans Dragons/Toulouse home games in Super League). Add more entries
# as needed; unmatched venues just fall back to the league default.
VENUE_UTC_OFFSETS = {
    "tbilisi": 4, "kutaisi": 4,
    "london": 1, "twickenham": 1, "cardiff": 1, "dublin": 1, "edinburgh": 1,
    "york": 1, "wigan": 1, "leeds": 1, "hull": 1, "warrington": 1, "liverpool": 1,
    "perpignan": 2, "toulouse": 2, "paris": 2, "marseille": 2, "lyon": 2,
    "turin": 2, "genoa": 2, "udine": 2,
    "johannesburg": 2, "cape town": 2, "durban": 2, "pretoria": 2,
    "buenos aires": -3, "mendoza": -3, "santiago del estero": -3,
    "cordoba": -3, "san juan": -3,
    "sydney": 10, "melbourne": 10, "brisbane": 10, "canberra": 10,
    "newcastle": 10, "perth": 8,
    "auckland": 12, "wellington": 12, "christchurch": 12,
    "tokyo": 9, "suva": 12,
    "edmonton": -6, "winnipeg": -5,
    "santiago": -4, "vina del mar": -4, "la serena": -4,
    "commerce city": -6, "charlotte": -4, "cary": -4,
    "montevideo": -3,
    "bucharest": 2, "lisbon": 0, "madrid": 1, "hong kong": 8,
    # CFL host cities (fallback only - the schedule pages give an explicit
    # zone abbreviation like "EDT" per match, which takes priority; see
    # CFL_TZ_OFFSETS / extract_cfl_time below)
    "ottawa": -4, "toronto": -4, "hamilton": -4, "montreal": -4,
    "regina": -6, "calgary": -6, "vancouver": -7,
}

# CFL schedule pages state each kickoff's timezone as an abbreviation
# (e.g. "7:00 p.m. EDT") rather than a UTC offset, and Saskatchewan in
# particular doesn't observe DST (always CST/-6) so a single per-city
# offset wouldn't be reliable across the season anyway. This is checked
# per-match before falling back to the venue-city table above.
CFL_TZ_OFFSETS = {
    "EDT": -4, "EST": -5,
    "CDT": -5, "CST": -6,
    "MDT": -6, "MST": -7,
    "PDT": -7, "PST": -8,
}


# 3-letter country codes used by the FIVB VNL page's {{vb-rt|..}}/{{vb|..}}
# sub-templates. Add more here if a league/year introduces new ones -
# unrecognised codes just get displayed as-is (e.g. "XYZ").
FIVB_COUNTRY_CODES = {
    "ARG": "Argentina", "BEL": "Belgium", "BRA": "Brazil", "BUL": "Bulgaria",
    "CAN": "Canada", "CHN": "China", "CUB": "Cuba", "FRA": "France",
    "GER": "Germany", "IRI": "Iran", "IRN": "Iran", "ITA": "Italy",
    "JPN": "Japan", "NED": "Netherlands", "POL": "Poland", "QAT": "Qatar",
    "SLO": "Slovenia", "SRB": "Serbia", "TUR": "Turkey", "UKR": "Ukraine",
    "USA": "United States", "AUS": "Australia", "CZE": "Czech Republic",
    "EGY": "Egypt", "FIN": "Finland",
}


def guess_utc_offset(venue_text, default_offset):
    """Look for a known city/venue keyword to override a league's default
    UTC offset (e.g. a Nations Championship match hosted in Auckland
    instead of the usual host country)."""
    if venue_text:
        lower = venue_text.lower()
        for keyword, offset in VENUE_UTC_OFFSETS.items():
            if keyword in lower:
                return offset
    return default_offset


def compute_utc(date_out, time_out, utc_offset):
    """Combine a local date+time with a UTC offset (hours) into an ISO 8601
    UTC datetime string, e.g. '2026-07-02T14:00:00+00:00'. Returns None if
    date, time, or offset isn't known - there's no safe way to place an
    event in UTC (or bucket it to the right calendar day for the viewer)
    without all three."""
    if not date_out or not time_out or utc_offset is None:
        return None
    try:
        local_dt = datetime.strptime(f"{date_out} {time_out}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    utc_dt = local_dt - timedelta(hours=utc_offset)
    return utc_dt.replace(tzinfo=timezone.utc).isoformat()


def fetch_page_html(page_title: str) -> str:
    """Fetch the rendered HTML body of a Wikipedia article via the MediaWiki API."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "text",
        "format": "json",
        "formatversion": "2",
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    url = f"{API_URL}?{query}"
    req = Request(url, headers=HEADERS)
    with _throttled_urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"Wikipedia API error for '{page_title}': {data['error']}")
    return data["parse"]["text"]


def fetch_page_wikitext(page_title: str) -> str:
    """Fetch the raw wikitext source of a Wikipedia article via the MediaWiki API."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    url = f"{API_URL}?{query}"
    req = Request(url, headers=HEADERS)
    with _throttled_urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"Wikipedia API error for '{page_title}': {data['error']}")
    return data["parse"]["wikitext"]


def normalize_date(iso_date, date_text, time_text):
    """Best-effort conversion of Wikipedia's date/time fields to YYYY-MM-DD / HH:MM."""
    date_out, time_out = None, None

    if iso_date:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", iso_date)
        if m:
            date_out = m.group(1)
        tm = re.search(r"T(\d{2}:\d{2})", iso_date)
        if tm:
            time_out = tm.group(1)

    if not date_out and date_text:
        cleaned = re.sub(r"\[.*?\]", "", date_text).strip()
        for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y"):
            try:
                date_out = datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    if not time_out and time_text:
        tm = re.search(r"(\d{1,2}:\d{2})", time_text)
        if tm:
            time_out = tm.group(1)

    return date_out, time_out


def clean_team_name(raw: str) -> str:
    """Strip bonus-point annotations like '(2 BP)' that get glued onto team names,
    wherever they land (prefix or suffix), and tidy whitespace."""
    cleaned = re.sub(r"\(\s*\d+\s*BP\s*\)", "", raw)
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_referee(raw: str) -> str:
    """Turn 'George Selwood ( England )' into 'George Selwood (England)'."""
    cleaned = re.sub(r"\(\s+", "(", raw)
    cleaned = re.sub(r"\s+\)", ")", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_matches(html: str, league_key: str, cfg: dict):
    """
    Parse match boxes on the page. These are divs with
    itemtype="http://schema.org/SportsEvent" and class "vevent", using
    hCard-style markup: team names in .fn spans, venue in .location,
    referee in .attendee. No itemprop attributes are present, and the
    score is plain text between the two team names rather than its own
    element, so it's pulled out with a regex over the box's text.
    """
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for box in soup.select('[itemtype="http://schema.org/SportsEvent"]'):
        team_els = box.select(".fn")
        if len(team_els) < 2:
            continue

        home = clean_team_name(team_els[0].get_text(" ", strip=True))
        away = clean_team_name(team_els[1].get_text(" ", strip=True))
        if not home or not away:
            continue

        box_text = box.get_text(" ", strip=True)

        # Date, e.g. "27 June 2026"
        date_match = re.search(r"\b(\d{1,2} [A-Z][a-z]+ \d{4})\b", box_text)
        date_text = date_match.group(1) if date_match else None

        # Kick-off time, e.g. "18:00" (grab the first one - the box also
        # repeats times inside try/con-scoring detail like "40+3'", which
        # won't match this HH:MM pattern)
        time_match = re.search(r"\b(\d{1,2}:\d{2})\b", box_text)
        time_text = time_match.group(1) if time_match else None

        # Score sits as plain text between the two team names, before any
        # try/con/penalty detail - e.g. "25–24" or "104–7". Not present
        # for matches that haven't been played yet.
        score_match = re.search(r"\b(\d{1,3})\s*[–-]\s*(\d{1,3})\b", box_text)
        score = f"{score_match.group(1)}-{score_match.group(2)}" if score_match else None

        venue_el = box.select_one(".location")
        venue = venue_el.get_text(" ", strip=True) if venue_el else None
        if venue:
            venue = re.sub(r"\s+", " ", venue).strip()

        referee_el = box.select_one(".attendee")
        referee = clean_referee(referee_el.get_text(" ", strip=True)) if referee_el else None

        date_out, time_out = normalize_date(None, date_text, time_text)
        offset = guess_utc_offset(venue, cfg.get("utc_offset"))
        utc = compute_utc(date_out, time_out, offset)

        matches.append(
            {
                "league": league_key,
                "home": home,
                "away": away,
                "score": score,
                "date": date_out,
                "time": time_out,
                "utc": utc,
                "venue": venue,
                "referee": referee,
            }
        )

    return matches


def table_to_grid(table):
    """
    Expand an HTML <table> into a full rectangular grid of cell text,
    resolving rowspan/colspan so that cells "carried down" from an earlier
    row (e.g. a venue shared by two matches) show up in every row they
    logically belong to, instead of only their originating row.
    """
    grid = []
    carry = {}  # col_index -> [remaining_rows, text]

    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        cell_idx = 0
        row_out = {}
        col = 0

        while cell_idx < len(cells) or any(v[0] > 0 for v in carry.values()):
            if carry.get(col, [0])[0] > 0:
                row_out[col] = carry[col][1]
                carry[col][0] -= 1
                col += 1
                continue
            if cell_idx >= len(cells):
                break
            cell = cells[cell_idx]
            cell_idx += 1
            text = cell.get_text(" ", strip=True)
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)
            for i in range(colspan):
                row_out[col + i] = text
                if rowspan > 1:
                    carry[col + i] = [rowspan - 1, text]
            col += colspan

        width = (max(row_out.keys()) + 1) if row_out else 0
        grid.append([row_out.get(i, "") for i in range(width)])

    return grid


def find_header_row(grid):
    """Find the row that names the columns. With two-tier headers (e.g. a
    'Home/Score/Away/Match information' row above a 'Day & Time/Venue/
    Referee/Attendance' row), rowspan carries 'Home'/'Score'/'Away' down
    into the second row too, so both rows can look like candidates - pick
    whichever candidate row maps to the most distinct column roles.
    Returns (row_index, row) or (None, None) if no header row is found."""
    best_idx, best_row, best_score = None, None, -1
    for idx, row in enumerate(grid):
        lower = [c.lower() for c in row]
        has_home = any("home" in c for c in lower)
        has_away = any("away" in c for c in lower)
        if not (has_home and has_away):
            continue
        roles = map_columns(row)
        if len(roles) > best_score:
            best_idx, best_row, best_score = idx, row, len(roles)
    return best_idx, best_row


def map_columns(header_row):
    """Map semantic roles to column indices based on header text, so this
    works regardless of column order or exact header wording."""
    roles = {}
    for i, raw in enumerate(header_row):
        h = raw.lower()
        if "home" in h:
            roles["home"] = i
        elif "away" in h:
            roles["away"] = i
        elif "score" in h or "result" in h:
            roles["score"] = i
        elif "venue" in h:
            roles["venue"] = i
        elif "referee" in h or h.strip() == "ref":
            roles["referee"] = i
        elif "attendance" in h:
            roles["attendance"] = i
        elif ("day" in h and "time" in h) or ("date" in h and "time" in h):
            roles["datetime"] = i
        elif "date" in h:
            roles["date"] = i
        elif "time" in h:
            roles["time"] = i
    return roles


def to_24h(time_str):
    """Convert '5:00 pm' / '17:00' style text to 'HH:MM'."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", time_str, re.IGNORECASE)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), m.group(2), m.group(3)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute}"


def parse_day_month(text, year):
    """Parse '23 May' / 'Sat, 23 May' / '1 March' style text (no year) into
    YYYY-MM-DD using the league's configured year. Returns None if no
    day+month pattern is found (e.g. text is just a weekday like
    'Thursday' with the actual date only implied by a round heading)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)", text)
    if not m:
        return None
    day, month = m.group(1), m.group(2)
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{day} {month} {year}", fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def find_round_date_range(table, year):
    """
    Later rounds on pages like NRL's often give only a weekday name
    ('Thursday, 7:00 pm') rather than a full date, relying on a nearby
    heading like 'March 12-15' for context. Look at the text immediately
    preceding the table for that kind of range and return a
    {weekday_name: 'YYYY-MM-DD'} map covering every date in it.
    """
    preceding_text = table.find_previous(string=re.compile(r"[A-Z][a-z]+\s+\d{1,2}\s*[–-]\s*\d{1,2}"))
    if not preceding_text:
        return {}
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2})\s*[–-]\s*(\d{1,2})", preceding_text)
    if not m:
        return {}
    month, d1, d2 = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        start = datetime.strptime(f"{d1} {month} {year}", "%d %B %Y")
    except ValueError:
        return {}
    mapping = {}
    for offset in range(0, max(d2 - d1, 0) + 1):
        dt = start + timedelta(days=offset)
        mapping[dt.strftime("%A")] = dt.strftime("%Y-%m-%d")
    return mapping


def resolve_date(combined_text, year, round_dates):
    """Try a full day+month first; if that's absent, fall back to matching
    a weekday name against the round's date range."""
    date_out = parse_day_month(combined_text, year)
    if date_out:
        return date_out
    wd_match = re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", combined_text)
    if wd_match and round_dates:
        return round_dates.get(wd_match.group(1))
    return None
BARE_ROW_RE = re.compile(
    r"\|align=right\|([^|]+)\|\|align=right\|\{\{ru-rt\|([A-Za-z]+)[^}]*\}\}"
    r"\|\|align=center\|(.+?)\|\|\{\{ru\|([A-Za-z]+)[^}]*\}\}\|\|(.*)"
)

# 3-letter country codes used by the {{ru|..}}/{{ru-rt|..}} templates on
# Nations Championship / Nations Cup style pages. Add more as needed -
# unrecognised codes just get displayed as-is (e.g. "XYZ").
RUGBY_COUNTRY_CODES = {
    "NZL": "New Zealand", "FRA": "France", "JPN": "Japan", "ITA": "Italy",
    "AUS": "Australia", "IRE": "Ireland", "FIJ": "Fiji", "WAL": "Wales",
    "RSA": "South Africa", "ENG": "England", "ARG": "Argentina", "SCO": "Scotland",
    "CAN": "Canada", "CHI": "Chile", "SAM": "Samoa", "TON": "Tonga",
    "USA": "United States", "ESP": "Spain", "ROM": "Romania", "ROU": "Romania",
    "URU": "Uruguay", "ZIM": "Zimbabwe", "GEO": "Georgia", "HKG": "Hong Kong",
    "POR": "Portugal",
}


# 3-letter codes used by the {{bk|..}}/{{bk-rt|..}} templates on FIBA
# World Cup qualifying pages. These are FIBA/IOC-style codes and don't
# always match ISO-3166 (e.g. Nigeria is "NGR" not "NGA", DR Congo is
# "COD"). Unrecognised codes just display as-is (e.g. "XYZ").
BASKETBALL_COUNTRY_CODES = {
    # Africa
    "CPV": "Cape Verde", "SSD": "South Sudan", "LBA": "Libya", "CMR": "Cameroon",
    "TUN": "Tunisia", "NGR": "Nigeria", "GUI": "Guinea", "RWA": "Rwanda",
    "EGY": "Egypt", "ANG": "Angola", "SEN": "Senegal", "MAD": "Madagascar",
    "CIV": "Ivory Coast", "MLI": "Mali", "UGA": "Uganda", "COD": "DR Congo",
    "MAR": "Morocco", "ALG": "Algeria", "KEN": "Kenya", "BEN": "Benin",
    "MOZ": "Mozambique", "GAB": "Gabon", "GHA": "Ghana", "CGO": "Congo",
    "TAN": "Tanzania", "ZAM": "Zambia", "RSA": "South Africa",
    # Americas
    "USA": "United States", "CAN": "Canada", "MEX": "Mexico", "ARG": "Argentina",
    "BRA": "Brazil", "PUR": "Puerto Rico", "DOM": "Dominican Republic",
    "VEN": "Venezuela", "BAH": "Bahamas", "URU": "Uruguay", "PAN": "Panama",
    "COL": "Colombia", "CHI": "Chile", "PAR": "Paraguay", "IVB": "Virgin Islands",
    "JAM": "Jamaica", "BER": "Bermuda", "NCA": "Nicaragua", "CUB": "Cuba",
    "ECU": "Ecuador", "BOL": "Bolivia", "CRC": "Costa Rica", "HON": "Honduras",
    "ESA": "El Salvador", "GUA": "Guatemala", "BAR": "Barbados",
    "TRI": "Trinidad and Tobago", "ARU": "Aruba", "GUY": "Guyana",
    "SUR": "Suriname", "HAI": "Haiti", "ISV": "U.S. Virgin Islands",
    # Asia / Oceania
    "CHN": "China", "JPN": "Japan", "PHI": "Philippines", "IRI": "Iran",
    "LBN": "Lebanon", "JOR": "Jordan", "IND": "India", "KOR": "South Korea",
    "KAZ": "Kazakhstan", "AUS": "Australia", "NZL": "New Zealand",
    "TPE": "Chinese Taipei", "QAT": "Qatar", "KSA": "Saudi Arabia",
    "BRN": "Bahrain", "SYR": "Syria", "IRQ": "Iraq", "INA": "Indonesia",
    "HKG": "Hong Kong", "THA": "Thailand", "GUM": "Guam",
    # Europe
    "FRA": "France", "GER": "Germany", "ESP": "Spain", "ITA": "Italy",
    "GRE": "Greece", "TUR": "Turkey", "POL": "Poland", "SRB": "Serbia",
    "LTU": "Lithuania", "LAT": "Latvia", "BEL": "Belgium", "NED": "Netherlands",
    "ISL": "Iceland", "FIN": "Finland", "SWE": "Sweden", "POR": "Portugal",
    "GBR": "Great Britain", "ISR": "Israel", "MNE": "Montenegro",
    "BIH": "Bosnia and Herzegovina", "CRO": "Croatia", "SLO": "Slovenia",
    "UKR": "Ukraine", "HUN": "Hungary", "CZE": "Czech Republic", "EST": "Estonia",
    "GEO": "Georgia", "MKD": "North Macedonia", "ROU": "Romania", "BUL": "Bulgaria",
    "SUI": "Switzerland", "AUT": "Austria", "DEN": "Denmark", "NOR": "Norway",
    "LUX": "Luxembourg", "CYP": "Cyprus", "MLT": "Malta", "SVK": "Slovakia",
    "AZE": "Azerbaijan", "KOS": "Kosovo", "IRL": "Ireland", "ARM": "Armenia",
    "ALB": "Albania", "MDA": "Moldova", "AND": "Andorra", "SMR": "San Marino",
}

# Country name (lowercase, as it appears in a match's "location" field, or
# derived from the home team when location has no country) -> IANA
# timezone name. Using real timezone names (not a fixed UTC-offset number)
# is what lets Python's zoneinfo correctly apply daylight saving time for
# whatever specific date each match falls on - many of these countries
# (all of Europe, Canada, the Bahamas, etc.) are +1 hour different in
# summer vs winter, which a single hardcoded offset can't represent.
COUNTRY_TIMEZONES = {
    # Africa (little/no DST observed across these, but IANA handles it if so)
    "tunisia": "Africa/Tunis", "cameroon": "Africa/Douala", "egypt": "Africa/Cairo",
    "senegal": "Africa/Dakar", "angola": "Africa/Luanda", "mali": "Africa/Bamako",
    "ivory coast": "Africa/Abidjan", "côte d'ivoire": "Africa/Abidjan",
    "kenya": "Africa/Nairobi", "nigeria": "Africa/Lagos", "south africa": "Africa/Johannesburg",
    "morocco": "Africa/Casablanca", "algeria": "Africa/Algiers", "rwanda": "Africa/Kigali",
    "uganda": "Africa/Kampala", "south sudan": "Africa/Juba", "cape verde": "Atlantic/Cape_Verde",
    "libya": "Africa/Tripoli", "guinea": "Africa/Conakry", "mozambique": "Africa/Maputo",
    "dr congo": "Africa/Kinshasa", "democratic republic of the congo": "Africa/Kinshasa",
    "gabon": "Africa/Libreville", "ghana": "Africa/Accra", "congo": "Africa/Brazzaville",
    "tanzania": "Africa/Dar_es_Salaam", "zambia": "Africa/Lusaka", "madagascar": "Indian/Antananarivo",
    # Americas (many of these DO observe DST - this matters a lot in summer)
    "united states": "America/New_York", "canada": "America/Toronto",
    "mexico": "America/Mexico_City", "argentina": "America/Argentina/Buenos_Aires",
    "brazil": "America/Sao_Paulo", "puerto rico": "America/Puerto_Rico",
    "dominican republic": "America/Santo_Domingo", "venezuela": "America/Caracas",
    "bahamas": "America/Nassau", "uruguay": "America/Montevideo", "panama": "America/Panama",
    "colombia": "America/Bogota", "chile": "America/Santiago", "paraguay": "America/Asuncion",
    "virgin islands": "America/St_Thomas", "jamaica": "America/Jamaica",
    "bermuda": "Atlantic/Bermuda", "nicaragua": "America/Managua", "cuba": "America/Havana",
    "ecuador": "America/Guayaquil", "bolivia": "America/La_Paz", "costa rica": "America/Costa_Rica",
    "honduras": "America/Tegucigalpa", "el salvador": "America/El_Salvador",
    "guatemala": "America/Guatemala", "barbados": "America/Barbados",
    "trinidad and tobago": "America/Port_of_Spain", "aruba": "America/Aruba",
    "guyana": "America/Guyana", "suriname": "America/Paramaribo", "haiti": "America/Port-au-Prince",
    # Asia / Oceania (no DST in these countries)
    "china": "Asia/Shanghai", "japan": "Asia/Tokyo", "philippines": "Asia/Manila",
    "iran": "Asia/Tehran", "lebanon": "Asia/Beirut", "jordan": "Asia/Amman",
    "india": "Asia/Kolkata", "south korea": "Asia/Seoul", "kazakhstan": "Asia/Almaty",
    "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland",
    "chinese taipei": "Asia/Taipei", "taiwan": "Asia/Taipei", "qatar": "Asia/Qatar",
    "saudi arabia": "Asia/Riyadh", "bahrain": "Asia/Bahrain", "syria": "Asia/Damascus",
    "iraq": "Asia/Baghdad", "indonesia": "Asia/Jakarta", "hong kong": "Asia/Hong_Kong",
    "thailand": "Asia/Bangkok", "united arab emirates": "Asia/Dubai", "kuwait": "Asia/Kuwait",
    "palestine": "Asia/Gaza", "guam": "Pacific/Guam",
    # Europe (all of these observe EU-wide DST, roughly late March - late October)
    "france": "Europe/Paris", "germany": "Europe/Berlin", "spain": "Europe/Madrid",
    "italy": "Europe/Rome", "greece": "Europe/Athens", "turkey": "Europe/Istanbul",
    "poland": "Europe/Warsaw", "serbia": "Europe/Belgrade", "lithuania": "Europe/Vilnius",
    "latvia": "Europe/Riga", "belgium": "Europe/Brussels", "netherlands": "Europe/Amsterdam",
    "iceland": "Atlantic/Reykjavik", "finland": "Europe/Helsinki", "sweden": "Europe/Stockholm",
    "portugal": "Europe/Lisbon", "great britain": "Europe/London", "united kingdom": "Europe/London",
    "israel": "Asia/Jerusalem", "montenegro": "Europe/Podgorica",
    "bosnia and herzegovina": "Europe/Sarajevo", "croatia": "Europe/Zagreb",
    "slovenia": "Europe/Ljubljana", "ukraine": "Europe/Kyiv", "hungary": "Europe/Budapest",
    "czech republic": "Europe/Prague", "czechia": "Europe/Prague", "estonia": "Europe/Tallinn",
    "georgia": "Asia/Tbilisi", "north macedonia": "Europe/Skopje", "romania": "Europe/Bucharest",
    "bulgaria": "Europe/Sofia", "switzerland": "Europe/Zurich", "austria": "Europe/Vienna",
    "denmark": "Europe/Copenhagen", "norway": "Europe/Oslo", "luxembourg": "Europe/Luxembourg",
    "cyprus": "Asia/Nicosia", "malta": "Europe/Malta", "slovakia": "Europe/Bratislava",
    "azerbaijan": "Asia/Baku", "kosovo": "Europe/Belgrade", "ireland": "Europe/Dublin",
    "armenia": "Asia/Yerevan", "albania": "Europe/Tirane", "moldova": "Europe/Chisinau",
    "andorra": "Europe/Andorra", "san marino": "Europe/Rome",
}

# Host-city overrides for countries that span multiple timezones, where the
# country-level default above would be wrong for certain cities (e.g. Perth
# is UTC+8 while the rest of Australia used for the country default is
# UTC+10/+11). Matched by substring against the match's combined
# venue+location text, checked BEFORE falling back to the country default.
CITY_TIMEZONE_OVERRIDES = {
    "perth": "Australia/Perth", "adelaide": "Australia/Adelaide",
    "darwin": "Australia/Darwin", "brisbane": "Australia/Brisbane",
    "vancouver": "America/Vancouver", "calgary": "America/Edmonton",
    "winnipeg": "America/Winnipeg", "honolulu": "Pacific/Honolulu",
    "anchorage": "America/Anchorage", "denver": "America/Denver",
    "phoenix": "America/Phoenix", "los angeles": "America/Los_Angeles",
    "oakland": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "chicago": "America/Chicago",
    "manaus": "America/Manaus",
}


def guess_timezone(location_text, venue_text, home_country_name):
    """Work out the IANA timezone for a match: prefer a specific city
    override if one of our known multi-timezone cities appears in the
    venue/location text, then a country named directly in the location
    text, then fall back to the home team's own country (reliable for
    genuine home-and-away fixtures; less so for neutral-venue tournament
    windows, but those pages tend to state the host country explicitly
    in 'location' anyway, so the first check already covers them)."""
    combined = f"{venue_text or ''} {location_text or ''}".lower()
    for city, tz_name in CITY_TIMEZONE_OVERRIDES.items():
        if city in combined:
            return tz_name
    if location_text:
        lower = location_text.lower()
        for country, tz_name in COUNTRY_TIMEZONES.items():
            if country in lower:
                return tz_name
    if home_country_name:
        return COUNTRY_TIMEZONES.get(home_country_name.lower())
    return None


def compute_utc_from_timezone(date_out, time_out, tz_name):
    """Like compute_utc(), but takes a real IANA timezone name instead of a
    fixed numeric offset, so DST is applied correctly for the specific
    date of each match."""
    if not date_out or not time_out or not tz_name:
        return None
    try:
        local_dt = datetime.strptime(f"{date_out} {time_out}", "%Y-%m-%d %H:%M")
        local_dt = local_dt.replace(tzinfo=ZoneInfo(tz_name))
    except Exception:
        return None
    return local_dt.astimezone(timezone.utc).isoformat()


def strip_wikilinks(text):
    """Turn '[[Article|Display text]]' / '[[Article]]' into just the
    visible display text."""
    def repl(m):
        inner = m.group(1)
        return inner.split("|")[-1]
    return re.sub(r"\[\[([^\]]+)\]\]", repl, text).strip()


def extract_bare_score(cell):
    """Pull the visible score out of a cell that's either a wikilink
    ('[[...|34-32]]') or plain text ('v' for not yet played)."""
    cell = cell.strip()
    if cell.startswith("[["):
        inner = cell[2:-2] if cell.endswith("]]") else cell[2:]
        visible = inner.split("|")[-1].strip()
    else:
        visible = cell
    if visible.lower() in ("v", "vs", "", "tba", "tbc", "tbd"):
        return None
    return visible.replace("–", "-").strip()


def parse_full_date(text):
    """Parse a date that already includes its year, e.g. '4 July 2026'."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    day, month, year = m.group(1), m.group(2), m.group(3)
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{day} {month} {year}", fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_bare_table_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse the header-less pipe-table fixture format used by pages like
    '2026 Nations Championship' and '2026 World Rugby Nations Cup':

        |align=right|DATE||align=right|{{ru-rt|HOME}}||align=center|
        [[...|SCORE]]||{{ru|AWAY}}||VENUE [Attendance: N]

    (ru-rt = home/host team, ru = away team). No kickoff time is given in
    this format, only a date, so "time"/"utc" stay None for every match
    parsed this way.
    """
    default_offset = cfg.get("utc_offset")
    matches = []

    for line in wikitext.splitlines():
        m = BARE_ROW_RE.search(line)
        if not m:
            continue
        date_text, code1, score_cell, code2, venue_raw = m.groups()

        home = RUGBY_COUNTRY_CODES.get(code1.upper(), code1.upper())
        away = RUGBY_COUNTRY_CODES.get(code2.upper(), code2.upper())

        score = extract_bare_score(score_cell)
        date_out = parse_full_date(date_text)

        venue = strip_wikilinks(venue_raw)
        venue = re.sub(r"\s*Attendance:\s*[\d,]+", "", venue).strip()
        venue = venue if venue else None

        matches.append(
            {
                "league": league_key,
                "home": home,
                "away": away,
                "score": score,
                "date": date_out,
                "time": None,
                "utc": None,
                "venue": venue,
                "referee": None,
            }
        )

    return matches


def parse_wikitable_matches(html: str, league_key: str, cfg: dict):
    """
    Parse plain wikitable-based results pages (NRL, Super League, AFLE,
    EFA style). Column order and exact header wording vary between pages,
    so columns are located by keyword rather than position - see
    map_columns(). Rowspan cells (shared date/venue across matches) are
    resolved via table_to_grid() before we ever look at column indices.
    """
    year = cfg.get("year", datetime.now().year)
    default_offset = cfg.get("utc_offset")
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for table in soup.find_all("table"):
        grid = table_to_grid(table)
        header_idx, header_row = find_header_row(grid)
        if not header_row:
            continue
        roles = map_columns(header_row)
        if "home" not in roles or "away" not in roles:
            continue

        round_dates = find_round_date_range(table, year)

        for row in grid[header_idx + 1:]:
            if len(row) <= max(roles.values()):
                continue

            home = row[roles["home"]].strip()
            away = row[roles["away"]].strip()
            if not home or not away:
                continue
            # Skip "Bye:" / "Source:" rows, which end up with identical
            # (colspan-merged) text repeated across every column.
            if home == away or "bye" in home.lower() or "source" in home.lower():
                continue

            score = row[roles["score"]].strip() if "score" in roles else None
            score = re.sub(r"\*+$", "", score).strip() if score else None
            if score in ("", "–", "-", "v"):
                score = None

            venue = row[roles["venue"]].strip() if "venue" in roles else None
            referee = row[roles["referee"]].strip() if "referee" in roles else None
            referee = referee if referee else None

            date_out, time_out = None, None
            if "datetime" in roles:
                combined = row[roles["datetime"]]
                date_out = resolve_date(combined, year, round_dates)
                time_out = to_24h(combined)
            else:
                if "date" in roles:
                    date_out = resolve_date(row[roles["date"]], year, round_dates)
                if "time" in roles:
                    time_out = to_24h(row[roles["time"]])

            offset = guess_utc_offset(venue, default_offset)
            utc = compute_utc(date_out, time_out, offset)

            matches.append(
                {
                    "league": league_key,
                    "home": home,
                    "away": away,
                    "score": score,
                    "date": date_out,
                    "time": time_out,
                    "utc": utc,
                    "venue": venue if venue else None,
                    "referee": referee,
                }
            )

    return matches


def _extract_wikitable_cells(row_chunk):
    """Pull cell text out of one wikitext table row (the lines between two
    "|-" row markers). Each cell lives on its own "!..." or "|..." line;
    an attribute list before the last "|" on that line (align=, style=,
    colspan=, etc.) is dropped, HTML tags and wikilinks are stripped, and
    external links like "[https://... Recap]" are reduced to their
    display text. Returns cell text in on-page order."""
    cells = []
    for line in row_chunk.split("\n"):
        line = line.strip()
        if not line or line.startswith("{|") or line.startswith("|}"):
            continue
        if line[0] not in "!|":
            continue
        rest = line[1:]
        content = rest.split("|", 1)[1] if "|" in rest else rest
        content = re.sub(r"<[^>]+>", "", content)
        content = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", content)
        content = strip_wikilinks(content)
        cells.append(content.strip())
    return cells


def parse_cfl_date(text, year):
    """Parse CFL schedule dates like 'Sat, June 6' / 'Fri, Jul 31' /
    'Sat, Sept 19' (weekday prefix, full or abbreviated month, no year -
    the year comes from the league config instead)."""
    if not text:
        return None
    cleaned = re.sub(r"^[A-Za-z]+,\s*", "", text.strip())
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned)
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2})", cleaned)
    if not m:
        return None
    month, day = m.group(1), m.group(2)
    for fmt in ("%b %d", "%B %d"):
        try:
            dt = datetime.strptime(f"{month} {day}", fmt)
            return dt.replace(year=year).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_cfl_time(text):
    """Parse '7:00 p.m. EDT' style text into ('19:00', utc_offset_hours).
    Either element can come back None if that part isn't present/known."""
    if not text:
        return None, None
    time_out = None
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", text, re.IGNORECASE)
    if m:
        hour, minute, ampm = int(m.group(1)), m.group(2), m.group(3).lower()
        if ampm == "p" and hour != 12:
            hour += 12
        if ampm == "a" and hour == 12:
            hour = 0
        time_out = f"{hour:02d}:{minute}"
    tzm = re.search(r"\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\b", text)
    offset = CFL_TZ_OFFSETS.get(tzm.group(1)) if tzm else None
    return time_out, offset


def parse_cfl_schedule(wikitext: str, league_key: str, cfg: dict, team_name: str):
    """
    Parse one CFL team's season-schedule table. Each row names only the
    *opponent* ("vs. Toronto Argonauts" / "at. Toronto Argonauts"), not
    both teams, so this only keeps "vs." (home) rows - the corresponding
    "at." row on the opponent's own page is skipped - so each match is
    produced exactly once no matter how many team pages get parsed.
    Column positions aren't assumed to be fixed; each field is located by
    matching its own shape (a date-like cell, a HH:MM am/pm cell, a
    vs./at. cell, a W/L-scoreline cell) so minor per-page layout
    differences don't break parsing.
    """
    year = cfg.get("year", datetime.now().year)
    default_offset = cfg.get("utc_offset")
    matches = []

    for block in re.findall(r"\{\|.*?\n\|\}", wikitext, re.DOTALL):
        if not re.search(r"\b(?:vs\.|at\.)\s*\[\[", block):
            continue  # not the schedule table (draft picks, standings, roster, ...)

        for chunk in re.split(r"\n\|-[^\n]*\n", block):
            cells = _extract_wikitable_cells(chunk)
            if len(cells) < 4 or any("bye" in c.lower() for c in cells):
                continue

            opp_idx = next(
                (i for i, c in enumerate(cells) if re.match(r"^(vs\.|at\.)\s+\S", c)), None
            )
            if opp_idx is None:
                continue

            prefix, opponent = re.match(r"^(vs\.|at\.)\s+(.*)", cells[opp_idx]).groups()
            opponent = opponent.strip()
            home, away = (team_name, opponent) if prefix == "vs." else (opponent, team_name)
            if home != team_name:
                continue  # this same match shows up as an "at." row on the away team's page

            date_text = next(
                (c for c in cells[:opp_idx] if re.search(r"[A-Za-z]{3,9}\.?\s*\d{1,2}\b", c)), None
            )
            time_text = next(
                (c for c in cells if re.search(r"\d{1,2}:\d{2}\s*[ap]\.?\s*m\.?", c, re.IGNORECASE)),
                None,
            )
            score_text = next(
                (c for c in cells[opp_idx + 1:] if re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", c)),
                None,
            )

            venue_text = None
            for c in cells[opp_idx + 1:]:
                if not c or c == score_text:
                    continue
                if re.match(r"^[\d,]+$", c):  # attendance
                    continue
                if re.match(r"^\d{1,2}[-–]\d{1,2}$", c):  # W-L record
                    continue
                if re.search(r"\b(TSN|RDS|CBSSN|ESPN|CBS)\b", c):  # TV networks
                    continue
                if "http" in c.lower() or c.lower().startswith("recap"):
                    continue
                venue_text = c
                break

            date_out = parse_cfl_date(date_text, year)
            time_out, tz_offset = extract_cfl_time(time_text)

            score = None
            if score_text:
                sm = re.search(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", score_text)
                if sm:
                    score = f"{sm.group(1)}-{sm.group(2)}"

            offset = tz_offset if tz_offset is not None else guess_utc_offset(venue_text, default_offset)
            utc = compute_utc(date_out, time_out, offset)

            matches.append(
                {
                    "league": league_key,
                    "home": home,
                    "away": away,
                    "score": score,
                    "date": date_out,
                    "time": time_out,
                    "utc": utc,
                    "venue": venue_text,
                    "referee": None,
                }
            )

    return matches


def find_templates(wikitext, name):
    """Find all {{name|...}} calls in wikitext (brace-depth aware, so
    nested templates like {{vb-rt|TUR}} inside don't confuse matching).
    Returns the raw inner text of each call (without the outer {{ }})."""
    results = []
    search_str = "{{" + name
    idx = 0
    while True:
        start = wikitext.find(search_str, idx)
        if start == -1:
            break
        depth = 0
        i = start
        end = None
        while i < len(wikitext) - 1:
            if wikitext[i:i + 2] == "{{":
                depth += 1
                i += 2
                continue
            if wikitext[i:i + 2] == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    end = i
                    break
                continue
            i += 1
        if end is None:
            break
        results.append(wikitext[start + 2:end - 2])
        idx = end
    return results


def split_template_params(inner):
    """Split a template's inner content on top-level pipes, ignoring pipes
    nested inside {{...}} sub-templates or [[...]] links."""
    params = []
    brace_depth = 0
    bracket_depth = 0
    current = []
    i = 0
    while i < len(inner):
        two = inner[i:i + 2]
        if two == "{{":
            brace_depth += 1
            current.append(two)
            i += 2
            continue
        if two == "}}":
            brace_depth -= 1
            current.append(two)
            i += 2
            continue
        if two == "[[":
            bracket_depth += 1
            current.append(two)
            i += 2
            continue
        if two == "]]":
            bracket_depth -= 1
            current.append(two)
            i += 2
            continue
        if inner[i] == "|" and brace_depth == 0 and bracket_depth == 0:
            params.append("".join(current))
            current = []
            i += 1
            continue
        current.append(inner[i])
        i += 1
    params.append("".join(current))
    return params


def split_by_timezone_sections(wikitext):
    """
    FIVB's page states each pool's/week's timezone inline, e.g.
    '* All times are Eastern Daylight Time (UTC-04:00).' - split the
    wikitext on these markers so matches in each section can be converted
    using the correct offset rather than a single guess for the whole
    tournament. Returns a list of (utc_offset_hours_or_None, chunk_text).
    """
    pattern = re.compile(r"All times are.*?UTC\s*([+\u2212-])\s*(\d{1,2})(?::(\d{2}))?", re.IGNORECASE)
    found = list(pattern.finditer(wikitext))
    if not found:
        return [(None, wikitext)]

    segments = []
    if found[0].start() > 0:
        segments.append((None, wikitext[:found[0].start()]))
    for idx, m in enumerate(found):
        sign = -1 if m.group(1) in ("-", "\u2212") else 1
        hours = int(m.group(2))
        minutes = int(m.group(3)) if m.group(3) else 0
        offset = sign * (hours + minutes / 60)
        start = m.end()
        end = found[idx + 1].start() if idx + 1 < len(found) else len(wikitext)
        segments.append((offset, wikitext[start:end]))
    return segments


def parse_fivb_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse FIVB Volleyball Nations League match results, which use a
    bespoke {{Vb res 12|date|time|{{vb-rt|CODE}}|setscore|{{vb|CODE}}|
    set1|set2|set3|set4|set5|attendance|12=refs}} template rather than
    any table or microdata markup that could be parsed generically. Each
    pool/week states its own timezone inline, so matches are converted to
    UTC per-section rather than with one offset for the whole page.
    """
    year = cfg.get("year", datetime.now().year)
    matches = []
    code_pattern = re.compile(r"\{\{vb-?r?t?\|([A-Za-z]{2,4})\}\}")

    for section_offset, chunk in split_by_timezone_sections(wikitext):
        for inner in find_templates(chunk, "Vb res 12"):
            params = split_template_params(inner)[1:]  # drop template name
            positional = [p for p in params if not re.match(r"^\s*\d+\s*=", p)]

            if len(positional) < 5:
                continue

            date_text, time_text, team1_raw, score, team2_raw = positional[:5]

            m1 = code_pattern.search(team1_raw)
            m2 = code_pattern.search(team2_raw)
            if not m1 or not m2:
                continue  # placeholder slot for a not-yet-determined knockout match

            home = FIVB_COUNTRY_CODES.get(m1.group(1).upper(), m1.group(1).upper())
            away = FIVB_COUNTRY_CODES.get(m2.group(1).upper(), m2.group(1).upper())

            score = score.strip()
            score = score.replace("–", "-") if score and score.strip("- ") else None

            date_out = parse_day_month(date_text, year)
            time_out = to_24h(time_text) if time_text.strip() else None
            utc = compute_utc(date_out, time_out, section_offset)

            matches.append(
                {
                    "league": league_key,
                    "home": home,
                    "away": away,
                    "score": score,
                    "date": date_out,
                    "time": time_out,
                    "utc": utc,
                    "venue": None,
                    "referee": None,
                }
            )

    return matches


def parse_rugbybox_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse {{rugbybox|...}} template instances, used by the Nations
    Championship / Nations Cup "Series" sub-articles (as opposed to the
    header-less bare_table format on their old parent pages). Each match
    gives an explicit date, time, AND UTC offset - e.g.
    'time = 14:00 [[Uruguay Time|UYT]] ([[UTC-3]])' - so unlike every
    other parser here, no venue-based offset guessing is needed: the
    page tells us the true UTC offset directly.
    """
    matches = []
    code_pattern = re.compile(r"\{\{(?:ru-rt|ru)\|([A-Za-z]+)")
    tz_pattern = re.compile(r"UTC\s*([+\u2212-])\s*(\d{1,2})(?::(\d{2}))?")

    for inner in find_templates(wikitext, "rugbybox"):
        params = split_template_params(inner)[1:]  # drop template name
        field = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                field[k.strip().lower()] = v.strip()

        team1_raw = field.get("team1", "")
        team2_raw = field.get("team2", "")
        m1 = code_pattern.search(team1_raw)
        m2 = code_pattern.search(team2_raw)
        if not m1 or not m2:
            continue

        home = RUGBY_COUNTRY_CODES.get(m1.group(1).upper(), m1.group(1).upper())
        away = RUGBY_COUNTRY_CODES.get(m2.group(1).upper(), m2.group(1).upper())

        score = field.get("score", "").strip()
        score = score.replace("–", "-") if score else None
        score = score if score else None

        date_out = parse_full_date(field.get("date", ""))

        time_field = field.get("time", "")
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_field)
        time_out = time_match.group(0) if time_match else None

        offset = None
        tz_match = tz_pattern.search(time_field)
        if tz_match:
            sign = -1 if tz_match.group(1) in ("-", "\u2212") else 1
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3)) if tz_match.group(3) else 0
            offset = sign * (hours + minutes / 60)

        utc = compute_utc(date_out, time_out, offset)

        venue = strip_wikilinks(field.get("stadium", ""))
        venue = venue if venue else None

        referee = strip_wikilinks(field.get("referee", ""))
        referee = referee if referee else None

        matches.append(
            {
                "league": league_key,
                "home": home,
                "away": away,
                "score": score,
                "date": date_out,
                "time": time_out,
                "utc": utc,
                "venue": venue,
                "referee": referee,
            }
        )

    return matches


def parse_basketballbox_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse {{basketballbox collapsible|...}} template instances used by
    FIBA World Cup qualifying pages (Africa/Americas/Asia/Europe). Teams
    are given as {{bk-rt|CODE}} (home) / {{bk|CODE}} (away) 3-letter codes,
    sometimes wrapped in '''bold''' to mark the winner - stripped before
    use. Unlike rugbybox, there's no explicit UTC offset field; instead a
    real IANA timezone is derived (see guess_timezone()) from the venue
    city, the 'location' field's country, or the home team's country, so
    DST is applied correctly for each match's actual date.
    """
    matches = []
    code_pattern = re.compile(r"\{\{bk(?:-rt)?\|([A-Za-z]{2,4})")

    for inner in find_templates(wikitext, "basketballbox"):
        params = split_template_params(inner)[1:]  # drop template name
        field = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                field[k.strip().lower()] = v.strip()

        teamA_raw = field.get("teama", "")
        teamB_raw = field.get("teamb", "")
        mA = code_pattern.search(teamA_raw)
        mB = code_pattern.search(teamB_raw)
        if not mA or not mB:
            continue

        home = BASKETBALL_COUNTRY_CODES.get(mA.group(1).upper(), mA.group(1).upper())
        away = BASKETBALL_COUNTRY_CODES.get(mB.group(1).upper(), mB.group(1).upper())

        score_a = field.get("scorea", "").replace("'''", "").strip()
        score_b = field.get("scoreb", "").replace("'''", "").strip()
        score = f"{score_a}-{score_b}" if score_a and score_b else None

        date_out = parse_full_date(field.get("date", ""))
        time_text = field.get("time", "").strip()
        time_out = to_24h(time_text) if time_text else None

        venue = strip_wikilinks(field.get("arena", ""))
        venue = venue if venue else None

        location = strip_wikilinks(field.get("location", ""))

        # Prefer a specific host city (handles multi-timezone countries
        # like Australia/Perth), then a country named directly in the
        # location text (e.g. the Africa page's "Radès, Tunisia" for its
        # single-venue tournament windows), then fall back to the home
        # team's own country - reliable for genuine home-and-away fixtures
        # (e.g. Europe/Americas pages, which only give a bare city like
        # "Fribourg" with no country attached).
        tz_name = guess_timezone(location, venue, home)
        utc = compute_utc_from_timezone(date_out, time_out, tz_name)

        # Combine arena + host country/city into one venue string for display
        venue_display = ", ".join(v for v in (venue, location) if v) or None

        referee = field.get("referee", "").strip()
        referee = referee if referee else None

        matches.append(
            {
                "league": league_key,
                "home": home,
                "away": away,
                "score": score,
                "date": date_out,
                "time": time_out,
                "utc": utc,
                "venue": venue_display,
                "referee": referee,
            }
        )

    return matches


def load_existing():
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"updated": None, "leagues": {}, "matches": []}


def save(data):
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_and_parse(cfg, key):
    parser_type = cfg.get("parser", "vevent")
    pages = cfg.get("pages") or ([cfg["page"]] if "page" in cfg else [])

    if parser_type == "vevent":
        matches = []
        for page in pages:
            html = fetch_page_html(page)
            matches.extend(parse_matches(html, key, cfg))
        return matches

    if parser_type == "wikitable":
        matches = []
        for page in pages:
            html = fetch_page_html(page)
            matches.extend(parse_wikitable_matches(html, key, cfg))
        return matches

    if parser_type == "bare_table":
        matches = []
        for page in pages:
            wikitext = fetch_page_wikitext(page)
            matches.extend(parse_bare_table_matches(wikitext, key, cfg))
        return matches

    if parser_type == "rugbybox":
        matches = []
        for page in pages:
            wikitext = fetch_page_wikitext(page)
            matches.extend(parse_rugbybox_matches(wikitext, key, cfg))
        return matches

    if parser_type == "fivb_template":
        wikitext = fetch_page_wikitext(pages[0])
        return parse_fivb_matches(wikitext, key, cfg)

    if parser_type == "basketballbox":
        matches = []
        for page in pages:
            wikitext = fetch_page_wikitext(page)
            matches.extend(parse_basketballbox_matches(wikitext, key, cfg))
        return matches

    if parser_type == "cfl_schedule":
        matches = []
        seen = set()
        for page, team_name in cfg["team_pages"].items():
            wikitext = fetch_page_wikitext(page)
            for m in parse_cfl_schedule(wikitext, key, cfg, team_name):
                # Safety net alongside the home-row-only filter in
                # parse_cfl_schedule, in case a page ever double-lists a game.
                sig = (m["date"], m["home"], m["away"])
                if sig in seen:
                    continue
                seen.add(sig)
                matches.append(m)
        return matches

    raise ValueError(f"Unknown parser type '{parser_type}' for league '{key}'")


def run(league_keys):
    data = load_existing()
    data.setdefault("leagues", {})
    data["matches"] = [m for m in data.get("matches", []) if m["league"] not in league_keys]

    for key in league_keys:
        cfg = LEAGUES[key]
        if "team_pages" in cfg:
            source_desc = f"{len(cfg['team_pages'])} team pages"
        else:
            pages = cfg.get("pages") or ([cfg["page"]] if "page" in cfg else [])
            source_desc = ", ".join(pages)
        print(f"Fetching {cfg['name']} ({source_desc}) ...")
        try:
            matches = fetch_and_parse(cfg, key)
            print(f"  -> found {len(matches)} matches")
            if not matches:
                print("  !! no matches parsed - the page's match-box markup may differ, "
                      "check LEAGUES config / page name", file=sys.stderr)
            data["matches"].extend(matches)
            data["leagues"][key] = {"name": cfg["name"], "sport": cfg["sport"]}
        except Exception as e:
            print(f"  !! failed: {e}", file=sys.stderr)

    data["matches"].sort(key=lambda m: (m["date"] or "9999-99-99", m["time"] or "99:99"))
    save(data)
    print(f"Wrote {len(data['matches'])} total matches to {OUTPUT_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Fetch sports fixtures from Wikipedia.")
    parser.add_argument("leagues", nargs="*", help="league keys to fetch (default: all)")
    parser.add_argument("--list", action="store_true", help="list configured leagues and exit")
    args = parser.parse_args()

    if args.list:
        for key, cfg in LEAGUES.items():
            print(f"  {key:20s} {cfg['name']}")
        return

    keys = args.leagues or list(LEAGUES.keys())
    unknown = [k for k in keys if k not in LEAGUES]
    if unknown:
        print(f"Unknown league key(s): {', '.join(unknown)}", file=sys.stderr)
        print("Use --list to see configured leagues.", file=sys.stderr)
        sys.exit(1)

    run(keys)


if __name__ == "__main__":
    main()