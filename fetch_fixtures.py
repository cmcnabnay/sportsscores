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
                  layout (team name in .fn, venue in .location) - e.g.
                  Rugby World Cup / Championship pages. Referees aren't
                  scraped even though the box markup also carries one.
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
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import truststore
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

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
            resp = urlopen(req, timeout=30, context=_SSL_CONTEXT)
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
        # Tournament finished - stop re-fetching (fixtures AND standings)
        # on every default run. Stored matches/standings/league entry are
        # left exactly as-is in fixtures.json (run() never touches a key
        # that isn't in the list it's asked to process), so matches.html
        # keeps showing the final results and table. Run
        # `python3 fetch_fixtures.py u20-jwc-2026 --force` manually if a
        # correction ever needs to be pulled in after the fact.
        "completed": True,
        "utc_offset": 4,  # Georgia (GET), no DST
        "tag_sections": True,  # tags each match with its enclosing Pool/
                                # bracket (h3) and round (h4) headings, so
                                # matches.html can render pool tables and a
                                # knockout bracket, not just a flat list
        "standings": {
            "page": "2026_World_Rugby_Junior_World_Championship",
            # However many pools exist this year (Pool A, Pool B, ...) -
            # each gets its own heading, so grab all of them rather than
            # hardcoding a count.
            "auto_groups": {"heading_pattern": r"^Pool_[A-Za-z0-9]+$"},
        },
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
        "standings": {
            # The two conference tables live on the old parent page (the
            # "Championship division: Tables" section), even though
            # fixtures themselves moved to the two Series sub-articles.
            "page": "2026_Nations_Championship",
            "groups": [
                {"label": "Northern Hemisphere", "heading_id": "Northern_Hemisphere"},
                {"label": "Southern Hemisphere", "heading_id": "Southern_Hemisphere"},
            ],
        },
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
        "standings": {
            "page": "2026_World_Rugby_Nations_Cup",
            "groups": [
                {"label": "Americas-Pacific", "heading_id": "Americas-Pacific_pool"},
                {"label": "European-African-Asian", "heading_id": "European-African-Asian_pool"},
            ],
        },
    },
    "currie-cup-2026": {
        "name": "Currie Cup Premier Division 2026",
        "page": "2026_Currie_Cup_Premier_Division",
        "sport": "rugby",
        "parser": "rugbybox2",
        "utc_offset": 2,  # South Africa Standard Time (SAST), no DST -
                          # always used directly rather than via
                          # guess_utc_offset(), see parse_rugbybox2_matches.
        "standings": {
            "page": "2026_Currie_Cup_Premier_Division",
            "groups": [
                {"label": "Premier Division", "heading_ids": ["Standings", "Table"]},
            ],
        },
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
        "standings": {
            # Confirmed from the page's own wikitext:
            #   ==Ladder==
            #   {{NRL2026Ladder}}
            # i.e. the season page's "Ladder" section is a bare
            # transclusion - the actual table data lives entirely on
            # Template:NRL2026Ladder, not on the season page itself.
            # Fetching that template page directly (see the "template"
            # key - tried before any heading_ids fallback below) sidesteps
            # any heading-rendering quirk on the season page entirely.
            "page": "2026_NRL_season",
            "groups": [
                {
                    "label": "Ladder",
                    "template": "NRL2026Ladder",
                    "heading_ids": ["Ladder", "Table"],
                }
            ],
        },
        # NRL's season page carries its own official per-club attendance
        # table (==Attendances== -> ===Club figures===: Team/Games/Total/
        # Average/...). Use it directly instead of computing attendance
        # from individual match records - it already excludes Magic Round
        # home games from each club's Games figure (Wikipedia marks this
        # with a footnote asterisk on the number itself), which a naive
        # "count every home fixture" computation over fixtures.json can't
        # replicate without separately knowing which fixtures were Magic
        # Round. If the heading has drifted, fetch_attendance_table()
        # falls back to a page-wide scan for a same-shaped table.
        "attendance_table": {
            "page": "2026_NRL_season",
            "heading_ids": ["Club_figures", "Attendances"],
        },
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
        "standings": {
            # Confirmed from the page's own wikitext:
            #   == Table ==
            #   {{2026 Super League regular season table}}
            # Same story as NRL above - the table itself lives entirely
            # on the Template: page, not the season page, so that's
            # fetched directly rather than relying on the season page's
            # rendered HTML.
            "page": "2026_Super_League_season",
            "groups": [
                {
                    "label": "Table",
                    "template": "2026 Super League regular season table",
                    "heading_ids": ["Table", "League_table"],
                }
            ],
        },
    },
    "afle-2026": {
        "name": "American Football League Europe 2026",
        "page": "2026_American_Football_League_Europe_season",
        "sport": "american-football",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 2,  # Central European Summer Time
        # This page's results table is headed "Home"/"Away" but the actual
        # on-page convention for this sport lists Away team first, Home
        # team second (i.e. what's under "Home" is really the away side) -
        # see the swap_home_away handling in parse_wikitable_matches.
        "swap_home_away": True,
        "standings": {
            "page": "2026_American_Football_League_Europe_season",
            "groups": [
                {"label": "North/West Division", "heading_id": "North/West_Division"},
                {"label": "South/East Division", "heading_id": "South/East_Division"},
                {"label": "League Wide", "heading_id": "League_Wide"},
            ],
        },
    },
    "efa-2026": {
        "name": "European Football Alliance 2026",
        "page": "2026_European_Football_Alliance_season",
        "sport": "american-football",
        "parser": "wikitable",
        "year": 2026,
        "utc_offset": 2,  # Central European Summer Time
        # Same Away-team-first convention as AFLE - see afle-2026's comment.
        "swap_home_away": True,
        "standings": {
            # Confirmed from the page's own wikitext:
            #   ===Standings===
            #   {{2026 European Football Alliance standings}}
            # Same story as NRL/Super League above.
            "page": "2026_European_Football_Alliance_season",
            "groups": [
                {
                    "label": "Standings",
                    "template": "2026 European Football Alliance standings",
                    "heading_ids": ["Standings", "Table", "League_table"],
                }
            ],
        },
    },
    "fivb-nations-league-2026": {
        "name": "FIVB Men's Volleyball Nations League 2026",
        "page": "2026_FIVB_Men's_Volleyball_Nations_League",
        "sport": "volleyball",
        "parser": "fivb_template",
        "year": 2026,
        # no single utc_offset - each pool states its own timezone in the
        # wikitext ("All times are ... (UTC-04:00)"), parsed per-match
        "standings": {
            "page": "2026_FIVB_Men's_Volleyball_Nations_League",
            "groups": [{"label": "Preliminary Round", "heading_id": "Ranking"}],
        },
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
        "standings": {
            # Each division is its OWN template - confirmed to exist as
            # separate pages, Template:2026 CFL West Division standings
            # and Template:2026 CFL East Division standings - unlike NRL/
            # Super League/EFA above there's no need to distinguish them
            # by position under a shared heading; each is fetched by its
            # own template name directly, which is unambiguous.
            "page": "2026_CFL_season",
            "groups": [
                {
                    "label": "West Division",
                    "template": "2026 CFL West Division standings",
                    "heading_ids": ["Standings", "Division_standings", "Table"],
                    "position": 0,
                },
                {
                    "label": "East Division",
                    "template": "2026 CFL East Division standings",
                    "heading_ids": ["Standings", "Division_standings", "Table"],
                    "position": 1,
                },
            ],
        },
    },
    # --- FIBA World Cup 2027 qualification standings -----------------------
    # These four pages reuse the same group letters (Group A, Group B, ...)
    # across multiple different rounds/phases (e.g. Europe's Pre-Qualifiers
    # Second Round and Qualifiers First Round both have a "Group A"), so a
    # single flat "grab every heading matching Group_X" scan (the old
    # auto_groups approach) mixed unrelated rounds' tables together under
    # duplicate-looking labels. Instead, "phases" scopes the search: each
    # phase (optionally with its own "rounds" sub-list, for pages that have
    # a Pre-Qualifiers/Qualifiers split above the round level) is anchored
    # to its own heading id, and every standings-shaped table nested under
    # that heading - however many groups it actually contains, whatever
    # they're called ("Group A", "Best fourth-placed team", "Ranking of
    # second-placed teams", ...) - is picked up automatically. See
    # fetch_phased_standings()/collect_subsection_tables(). Group labels
    # come out as "Phase \u00b7 Round \u00b7 Group" breadcrumbs, which
    # matches.html splits on to build the round-picker menu.
    #
    # Heading ids below are best-effort (built from each page's documented
    # round names) - if Wikipedia's actual anchor text differs, a run will
    # print a "phase heading ... not found" warning rather than silently
    # showing nothing, so any mismatch is easy to spot and fix.
    "fiba-wcq-africa-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Africa",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Africa)",
        "sport": "basketball",
        "parser": "basketballbox",
        # No single utc_offset - each match's "location" field names a host
        # country, looked up in COUNTRY_UTC_OFFSETS per-match instead.
        "standings": {
            "page": "2027_FIBA_Basketball_World_Cup_qualification_(Africa)",
            "phases": [
                {"label": "First Round", "heading_id": ["First_round", "First_Round"]},
                {"label": "Second Round", "heading_id": ["Second_round", "Second_Round"]},
            ],
        },
    },
    "fiba-wcq-americas-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Americas",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Americas)",
        "sport": "basketball",
        "parser": "basketballbox",
        "standings": {
            "page": "2027_FIBA_Basketball_World_Cup_qualification_(Americas)",
            "phases": [
                {
                    "label": "Pre-Qualifiers",
                    "heading_id": ["Pre-Qualifiers", "Pre-qualifiers"],
                    "rounds": [
                        {"label": "First Round", "heading_id": ["First_round", "First_Round"]},
                        {"label": "Second Round", "heading_id": ["Second_round", "Second_Round"]},
                    ],
                },
                {
                    "label": "Qualifiers",
                    "heading_id": ["Qualifiers"],
                    "rounds": [
                        # 2nd occurrence of these heading names on the page
                        # - MediaWiki disambiguates repeated ids as _2, _3...
                        {"label": "First Round", "heading_id": ["First_round_2", "First_Round_2", "First_round", "First_Round"]},
                        {"label": "Second Round", "heading_id": ["Second_round_2", "Second_Round_2", "Second_round", "Second_Round"]},
                    ],
                },
            ],
        },
    },
    "fiba-wcq-asia-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Asia",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Asia)",
        "sport": "basketball",
        "parser": "basketballbox",
        "standings": {
            "page": "2027_FIBA_Basketball_World_Cup_qualification_(Asia)",
            "phases": [
                {"label": "First Round", "heading_id": ["First_round", "First_Round"]},
                {"label": "Second Round", "heading_id": ["Second_round", "Second_Round"]},
            ],
        },
    },
    "fiba-wcq-europe-2027": {
        "name": "FIBA Basketball World Cup 2027 Qualification - Europe",
        "page": "2027_FIBA_Basketball_World_Cup_qualification_(Europe)",
        "sport": "basketball",
        "parser": "basketballbox",
        "standings": {
            "page": "2027_FIBA_Basketball_World_Cup_qualification_(Europe)",
            "phases": [
                {
                    "label": "Pre-Qualifiers",
                    "heading_id": ["Pre-Qualifiers", "Pre-qualifiers"],
                    "rounds": [
                        {"label": "First Round", "heading_id": ["First_round", "First_Round"]},
                        {"label": "Second Round", "heading_id": ["Second_round", "Second_Round"]},
                    ],
                },
                {
                    "label": "Qualifiers",
                    "heading_id": ["Qualifiers"],
                    "rounds": [
                        {"label": "First Round", "heading_id": ["First_round_2", "First_Round_2", "First_round", "First_Round"]},
                        {"label": "Second Round", "heading_id": ["Second_round_2", "Second_Round_2", "Second_round", "Second_Round"]},
                    ],
                },
            ],
        },
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


# Caches each page's content (wikitext and/or rendered HTML) for the
# lifetime of one run, keyed by page title. Several leagues fetch the same
# page twice - once for matches, once for standings (e.g. u20-jwc,
# super-league, afle all point their "standings" config at the same page
# used for matches) - so without this, that's a fully duplicated network
# request for identical content every single run.
_page_cache = {}


def fetch_page_html(page_title: str) -> str:
    """Fetch the rendered HTML body of a Wikipedia article via the MediaWiki API."""
    cached = _page_cache.get(page_title, {})
    if "html" in cached:
        return cached["html"]

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
    html = data["parse"]["text"]
    _page_cache.setdefault(page_title, {})["html"] = html
    return html


def fetch_page_wikitext(page_title: str) -> str:
    """Fetch the raw wikitext source of a Wikipedia article via the
    lightweight ?action=raw endpoint, rather than action=parse&prop=wikitext.
    Both return the same underlying source, but action=parse routes through
    MediaWiki's full parser/rendering pipeline server-side even when all
    that's wanted is the raw text - that's the more expensive of the two
    operations to run, and anonymous API access gets throttled harder for
    it. action=raw skips rendering entirely and just serves the page
    content directly, which is lighter on Wikipedia's end and far less
    likely to trip a 429."""
    cached = _page_cache.get(page_title, {})
    if "wikitext" in cached:
        return cached["wikitext"]

    url = f"https://en.wikipedia.org/w/index.php?title={quote(page_title)}&action=raw"
    req = Request(url, headers=HEADERS)
    with _throttled_urlopen(req) as resp:
        wikitext = resp.read().decode("utf-8")
    if wikitext.lstrip().startswith("<") and "#REDIRECT" not in wikitext.upper():
        # A raw fetch returning HTML/XML instead of wikitext usually means
        # the page title is wrong (redirected to a login/error page rather
        # than raising a clean error) - fail loudly instead of silently
        # trying to regex-parse HTML as if it were wikitext.
        raise RuntimeError(f"Unexpected non-wikitext response for '{page_title}' via action=raw")
    _page_cache.setdefault(page_title, {})["wikitext"] = wikitext
    return wikitext


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


def strip_citations(text: str) -> str:
    """Strip inline citation markup that sometimes gets glued onto a venue
    field, e.g. a '<ref>{{cite news |url=...|title=...}}</ref>' or a bare
    '{{cite news|...}}' template with no surrounding <ref> tags. Wikipedia
    articles often source a venue change (e.g. a "home" team playing a
    neutral-venue game abroad) with an inline citation right after the
    venue name, which we don't want showing up in the UI."""
    if not text:
        return text
    # <ref ...>...</ref> and self-closing <ref ... />
    cleaned = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<ref[^>]*/>", "", cleaned, flags=re.IGNORECASE)
    # Bare {{cite ...}} / {{Cite ...}} templates not wrapped in <ref> tags
    cleaned = re.sub(r"\{\{\s*[Cc]ite[^{}]*\}\}", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def preceding_heading_breadcrumb(element, levels=(3, 4)):
    """Return {level: heading_text} for the nearest heading at each given
    level that appears before `element` in document order - i.e. which
    section(s) this element sits inside, most specific included. Used to
    tag which pool/bracket and round a knockout match belongs to, straight
    from the page's own heading structure (e.g. h3 "Thirteenth-place
    bracket" > h4 "Thirteenth-place semi-finals") rather than hardcoding
    bracket shapes per tournament."""
    breadcrumb = {}
    remaining = set(levels)
    for h in element.find_all_previous(re.compile(r"^h[2-6]$")):
        level = int(h.name[1])
        if level in remaining:
            breadcrumb[level] = h.get_text(" ", strip=True)
            remaining.discard(level)
        if not remaining:
            break
    return breadcrumb


def parse_matches(html: str, league_key: str, cfg: dict):
    """
    Parse match boxes on the page. These are divs with
    itemtype="http://schema.org/SportsEvent" and class "vevent", using
    hCard-style markup: team names in .fn spans, venue in .location. No
    itemprop attributes are present, and the score is plain text between
    the two team names rather than its own element, so it's pulled out
    with a regex over the box's text. The box also carries a referee name
    in .attendee, but that isn't scraped/displayed.
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
            venue = strip_citations(re.sub(r"\s+", " ", venue).strip())

        # Attendance shows up as plain text like "Attendance: 45,123" inside
        # the box, not its own element - same free-text situation as the
        # date/time/score fields above.
        attendance_match = re.search(r"Attendance:?\s*([\d,]+)", box_text, re.IGNORECASE)
        attendance = attendance_match.group(1).replace(",", "") if attendance_match else None

        date_out, time_out = normalize_date(None, date_text, time_text)
        offset = guess_utc_offset(venue, cfg.get("utc_offset"))
        utc = compute_utc(date_out, time_out, offset)

        group, round_name = None, None
        if cfg.get("tag_sections"):
            breadcrumb = preceding_heading_breadcrumb(box, levels=(3, 4))
            group = breadcrumb.get(3)
            round_name = breadcrumb.get(4)

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
                "attendance": attendance,
                "group": group,
                "round": round_name,
            }
        )

    return matches


def table_to_grid(table):
    """
    Expand an HTML <table> into a full rectangular grid of cell text,
    resolving rowspan/colspan so that cells "carried down" from an earlier
    row (e.g. a venue shared by two matches) show up in every row they
    logically belong to, instead of only their originating row.

    Before reading each cell's text, strips any embedded "v t e" (view-
    talk-edit) navbar box - the standard Wikipedia widget (class contains
    "navbar", e.g. on many standings templates like NRL2026Ladder or
    2026 Super League regular season table) that lets editors jump to/
    edit the template, usually planted right in the header row's corner
    cell alongside "Team". Left in, its text ("v t e") glues onto that
    cell's real text (e.g. "Team" becomes "Team v t e"), which silently
    breaks _standings_column_role()'s exact-match lookup and makes the
    whole table look like it has no recognizable team column at all.
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
            for navbar in cell.select(".navbar"):
                navbar.decompose()
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
    "edmonton": "America/Edmonton",
    "winnipeg": "America/Winnipeg", "honolulu": "Pacific/Honolulu",
    "anchorage": "America/Anchorage", "denver": "America/Denver",
    "phoenix": "America/Phoenix", "los angeles": "America/Los_Angeles",
    "oakland": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "chicago": "America/Chicago",
    "manaus": "America/Manaus",
    # World Rugby Nations Cup / Nations Championship Americas-Pacific Series
    # host cities. Most of these are neutral venues for Samoa/Tonga (neither
    # of which has a COUNTRY_TIMEZONES entry, since they aren't playing in
    # their own country), so a city-level override is the only reliable way
    # to get these right - the home-team-country fallback in guess_timezone()
    # would otherwise silently guess wrong (or fail outright for Samoa/Tonga).
    "montevideo": "America/Montevideo",
    "vina del mar": "America/Santiago", "viña del mar": "America/Santiago",
    "santiago": "America/Santiago",
    "charlotte": "America/New_York",
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
        attendance_match = re.search(r"Attendance:\s*([\d,]+)", venue, re.IGNORECASE)
        attendance = attendance_match.group(1).replace(",", "") if attendance_match else None
        venue = re.sub(r"\s*Attendance:\s*[\d,]+", "", venue).strip()
        venue = strip_citations(venue) if venue else None

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
                "attendance": attendance,
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
            venue = strip_citations(venue) if venue else None

            attendance = row[roles["attendance"]].strip() if "attendance" in roles else None
            attendance_match = re.search(r"([\d,]+)", attendance) if attendance else None
            attendance = attendance_match.group(1).replace(",", "") if attendance_match else None

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

            # American football pages (AFLE, EFA) use away-score-first in the
            # Result column. Older table layouts also invert the Home/Away
            # column labels (what's headed "Home" is actually the away team).
            # Newer table layouts correctly label columns "Away team"/"Home
            # team", so only the score direction needs fixing, not the teams.
            # Detect which format this table uses by checking whether the
            # "away" column header includes the word "team" - if it does, the
            # labels are correct and we only flip the score; if not, we also
            # swap the team assignments. Both formats then produce the same
            # home/away attribution so duplicates merge naturally.
            if cfg.get("swap_home_away"):
                away_header = header_row[roles["away"]].lower()
                if "team" not in away_header:
                    home, away = away, home
                if score:
                    sm = re.match(r"^\s*(\d{1,3})\s*[-\u2013]\s*(\d{1,3})\s*$", score)
                    if sm:
                        score = f"{sm.group(2)}-{sm.group(1)}"

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
                    "attendance": attendance,
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

    # CFL team-season pages put Preseason / Regular season / Postseason
    # games in their own separate schedule tables, each under its own big
    # section heading (e.g. "==Preseason==" ... "===Schedule===" ... {|
    # ... |} ... "==Regular season==" ... "===Standings===" ... "===Schedule==="
    # ... {| ... |}). The nearest heading right above a given table is
    # usually the "Schedule"/"Standings" subheading, not the Preseason/
    # Regular season one - so rather than just taking whatever heading
    # comes right before the table, only headings whose own text names an
    # actual section (Preseason/Regular season/Postseason) update which
    # section we're in; a "Schedule" or "Standings" subheading in between
    # is simply ignored, and the state carries through it. "=" counts on
    # each side aren't assumed to match (some pages have malformed heading
    # markup like "=Preseason==").
    section_headings = []
    for m in re.finditer(r"^={1,6}\s*([^=\n]+?)\s*={1,6}\s*$", wikitext, re.MULTILINE):
        text = m.group(1).strip()
        if re.search(r"pre[- ]?season", text, re.IGNORECASE):
            section_headings.append((m.start(), "preseason"))
        elif re.search(r"regular[- ]?season", text, re.IGNORECASE):
            section_headings.append((m.start(), "regular"))
        elif re.search(r"post[- ]?season|play-?offs?", text, re.IGNORECASE):
            section_headings.append((m.start(), "postseason"))

    def section_at(pos):
        section = None
        for hpos, sec in section_headings:
            if hpos >= pos:
                break
            section = sec
        return section

    for block_match in re.finditer(r"\{\|.*?\n\|\}", wikitext, re.DOTALL):
        block = block_match.group(0)
        if not re.search(r"\|\s*(?:vs\.|at)\s+\S", block):
            continue  # not the schedule table (draft picks, standings, roster, ...)

        if section_at(block_match.start()) == "preseason":
            continue  # preseason games don't count as part of the season schedule

        current_section = None
        for chunk in re.split(r"\n\|-[^\n]*\n", block):
            cells = _extract_wikitable_cells(chunk)
            has_opponent = any(re.match(r"^(vs\.|at)\s+\S", c) for c in cells)

            if not has_opponent:
                # Not a game row - possibly a section-divider row (e.g. a
                # colspan'd "'''Preseason'''" / "'''Regular Season'''"
                # sub-header sitting inside this same table, rather than a
                # separate table under its own wiki heading, on pages laid
                # out that way instead). Track which section we're in so
                # preseason rows can be skipped even though they live in
                # the same {| ... |} block as the regular-season rows.
                divider_text = " ".join(cells)
                if re.search(r"pre[- ]?season", divider_text, re.IGNORECASE):
                    current_section = "preseason"
                elif re.search(r"regular[- ]?season|post[- ]?season|play-?offs?", divider_text, re.IGNORECASE):
                    current_section = None
                continue

            if current_section == "preseason":
                continue  # preseason games don't count as part of the season schedule

            if len(cells) < 4 or any("bye" in c.lower() for c in cells):
                continue

            opp_idx = next(
                (i for i, c in enumerate(cells) if re.match(r"^(vs\.|at)\s+\S", c)), None
            )
            if opp_idx is None:
                continue

            prefix, opponent = re.match(r"^(vs\.|at)\s+(.*)", cells[opp_idx]).groups()
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
            attendance = None
            for c in cells[opp_idx + 1:]:
                if not c or c == score_text:
                    continue
                if re.match(r"^[\d,]+$", c):  # attendance
                    attendance = c.replace(",", "")
                    continue
                if re.match(r"^\d{1,2}[-–]\d{1,2}$", c):  # W-L record
                    continue
                if re.search(r"\b(TSN|RDS|CBSSN|ESPN|CBS)\b", c):  # TV networks
                    continue
                if "http" in c.lower() or c.lower().startswith("recap"):
                    continue
                if venue_text is None:
                    venue_text = c

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
                    "attendance": attendance,
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

            # Unlike the other match-box templates, Vb res 12 (and its
            # documented sibling Vb res 51) doesn't carry a per-match
            # attendance parameter at all - the page-level infobox instead
            # hand-tallies a single running total via a giant #expr sum of
            # literal numbers in HTML comments per pool, with no reliable
            # link back to individual matches. So there's no attendance to
            # extract here.
            attendance = None

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
                    "attendance": attendance,
                }
            )

    return matches


def parse_rugbybox_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse {{rugbybox|...}} template instances, used by the Nations
    Championship / Nations Cup "Series" sub-articles (as opposed to the
    header-less bare_table format on their old parent pages). Each match
    is EXPECTED to give an explicit date, time, AND UTC offset - e.g.
    'time = 14:00 [[Uruguay Time|UYT]] ([[UTC-3]])' - so ideally no
    venue-based offset guessing is needed: the page tells us the true UTC
    offset directly.

    In practice these "Series" sub-articles are new pages that don't
    always follow that exact format (e.g. the offset annotation is
    missing, worded differently, or the venue is a neutral one for
    Samoa/Tonga rather than the home team's own country), and a silent
    failure here means the match falls all the way back in matches.html
    to displaying the raw *venue-local* kickoff time unconverted - which
    looks fine at a glance but is wrong for anyone not in that venue's
    timezone. So this now has two layers: try the page-stated offset
    first (checked against both the date and time fields, and tolerant of
    a couple of extra UTC/GMT spellings), then fall back to a real IANA
    timezone guessed from the venue city (guess_timezone/CITY_TIMEZONE_OVERRIDES)
    so DST is still applied correctly. A stderr warning is printed if
    neither works, so a bad/unrecognized page format shows up immediately
    instead of silently mis-displaying kickoff times.
    """
    matches = []
    code_pattern = re.compile(r"\{\{(?:ru-rt|ru)\|([A-Za-z]+)")
    # Accepts "UTC-3", "UTC−3:00", "UTC +3", and "GMT-3" style annotations.
    tz_pattern = re.compile(r"(?:UTC|GMT)\s*([+\u2212-])\s*(\d{1,2})(?::(\d{2}))?")

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

        date_field = field.get("date", "")
        date_out = parse_full_date(date_field)

        time_field = field.get("time", "")
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_field)
        time_out = time_match.group(0) if time_match else None

        venue = strip_citations(strip_wikilinks(field.get("stadium", "")))
        venue = venue if venue else None

        offset = None
        tz_match = tz_pattern.search(time_field) or tz_pattern.search(date_field)
        if tz_match:
            sign = -1 if tz_match.group(1) in ("-", "\u2212") else 1
            hours = int(tz_match.group(2))
            minutes = int(tz_match.group(3)) if tz_match.group(3) else 0
            offset = sign * (hours + minutes / 60)

        utc = compute_utc(date_out, time_out, offset)

        if utc is None and date_out and time_out:
            # No usable explicit offset on the page - fall back to a real
            # timezone guessed from the venue city (falls back further to
            # the home team's own country, though that's unreliable for
            # Samoa/Tonga neutral-venue games with no country tz entry).
            tz_name = guess_timezone(None, venue, home)
            if tz_name:
                utc = compute_utc_from_timezone(date_out, time_out, tz_name)
            if utc is None:
                print(
                    f"  !! rugbybox: couldn't resolve a UTC offset for "
                    f"{home} v {away} ({date_out} {time_out}, venue={venue!r}) "
                    f"- displaying venue-local time uncorrected",
                    file=sys.stderr,
                )

        attendance = strip_wikilinks(field.get("attendance", "")).strip()
        attendance_match = re.search(r"([\d,]+)", attendance) if attendance else None
        attendance = attendance_match.group(1).replace(",", "") if attendance_match else None

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
                "attendance": attendance,
            }
        )

    return matches


RUT_TEAM_RE = re.compile(r"\{\{Rut\|([^}|]+)")


def parse_rugbybox2_matches(wikitext: str, league_key: str, cfg: dict):
    """
    Parse {{Rugbybox collapsible2|...}} template instances, used by the
    Currie Cup page. Unlike the {{rugbybox}} template (Nations Championship/
    Nations Cup - team1=/team2= fields, {{ru|CODE}} 3-letter country codes,
    score derived implicitly), this template gives team names directly via
    home=/away= fields wrapping a {{Rut|Team Name}} template (sometimes with
    a trailing bonus-point annotation like "(2 BP)"), and states the final
    score directly in a score= field (e.g. "24\u201326") rather than needing
    it inferred from try/con/penalty detail.

    All Currie Cup venues are inside South Africa, which is UTC+2 year-round
    (no DST) - so this always uses the league's configured utc_offset
    directly rather than guess_utc_offset()'s venue-keyword lookup, which
    would otherwise misfire on e.g. "Wellington" (mapped there to New
    Zealand, UTC+12) when the actual venue is Wellington, South Africa.
    """
    matches = []
    offset = cfg.get("utc_offset")

    for inner in find_templates(wikitext, "Rugbybox collapsible2"):
        params = split_template_params(inner)[1:]  # drop template name
        field = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                field[k.strip().lower()] = v.strip()

        home_raw = field.get("home", "")
        away_raw = field.get("away", "")
        m1 = RUT_TEAM_RE.search(home_raw)
        m2 = RUT_TEAM_RE.search(away_raw)
        home = clean_team_name(m1.group(1)) if m1 else clean_team_name(strip_wikilinks(home_raw))
        away = clean_team_name(m2.group(1)) if m2 else clean_team_name(strip_wikilinks(away_raw))
        if not home or not away:
            continue

        score = field.get("score", "").strip()
        score = score.replace("\u2013", "-").replace("\u2212", "-") if score else None
        score = score if score else None

        date_out = parse_full_date(field.get("date", ""))
        time_field = field.get("time", "")
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_field)
        time_out = time_match.group(0) if time_match else None

        venue = strip_citations(strip_wikilinks(field.get("stadium", "")))
        venue = venue if venue else None

        utc = compute_utc(date_out, time_out, offset)

        attendance = strip_wikilinks(field.get("attendance", "")).strip()
        attendance_match = re.search(r"([\d,]+)", attendance) if attendance else None
        attendance = attendance_match.group(1).replace(",", "") if attendance_match else None

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
                "attendance": attendance,
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

        venue = strip_citations(strip_wikilinks(field.get("arena", "")))
        venue = venue if venue else None

        location = strip_citations(strip_wikilinks(field.get("location", "")))

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

        attendance = strip_wikilinks(field.get("attendance", "")).strip()
        attendance_match = re.search(r"([\d,]+)", attendance) if attendance else None
        attendance = attendance_match.group(1).replace(",", "") if attendance_match else None

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
                "attendance": attendance,
            }
        )

    return matches


# ---------------------------------------------------------------------------
# Standings / tables
#
# Every league can optionally define a "standings" config so matches.html
# has a table to show alongside fixtures, e.g.:
#
#   "standings": {
#       "page": "2026_Nations_Championship",
#       "groups": [
#           {"label": "Northern Hemisphere", "heading_id": "Northern_Hemisphere"},
#           {"label": "Southern Hemisphere", "heading_id": "Southern_Hemisphere"},
#       ],
#   }
#
# Rather than re-implementing every sport's Lua-computed points table
# (Wikipedia's Module:Sports table, or a hand-maintained template like
# "2026 CFL West Division standings"), this fetches the page's RENDERED
# HTML (same "action=parse&prop=text" call already used for vevent/
# wikitable fixtures) - by the time it's rendered, the module/template has
# already computed the actual wikitable, so all this needs to do is find
# the right <table class="wikitable"> and read off whichever columns it
# has, by header keyword (Team/Pld/W/D/L/Pts/etc.) rather than fixed
# position, the same way map_columns() does for fixture tables.
#
# A group is located either by:
#   - "template": the name of a Wikipedia Template: page (without the
#     "Template:" prefix) that's directly transcluded for this table,
#     e.g. "NRL2026Ladder" for a season page whose wikitext just says
#     "{{NRL2026Ladder}}" under its "Ladder" heading. This is the most
#     reliable option when it applies: a bare transclusion means the
#     table data lives entirely on the template's own page, not the
#     season page, so this fetches and parses THAT page directly rather
#     than hoping the season page's rendered HTML has a findable heading
#     sitting above it. Tried first, before any heading-based lookup
#     below. Optionally paired with "template_position" (0-indexed) if
#     the template page itself contains more than one candidate table.
#   - "heading_id": the id of the heading immediately before it (checked
#     against both the heading tag's own id and a wrapped "mw-headline"
#     span, to work with either MediaWiki HTML output style), optionally
#     with "position" (0-indexed) when more than one table follows the
#     same heading before the next one (e.g. two divisional tables both
#     sitting under a single "Standings" heading).
#   - "heading_ids": like "heading_id" but a list of alternatives, tried
#     in order - use this when a page's section title for the standings
#     table has been observed to vary or drift (e.g. "Ladder" vs "Table").
#   - "table_index": the Nth wikitable on the whole page, for pages with
#     no useful heading to anchor on.
#
# "template" and "heading_id(s)"/"table_index" can be combined (as they
# are for nrl-2026/super-league-2026/efa-2026/cfl-2026 below) - "template"
# is tried first, and the others only come into play if that specific
# template page ever goes away or gets renamed.
#
# If none of the above locate a table (e.g. every configured heading id
# is stale because the page was restructured), fetch_standings() falls
# back to a page-wide scan for tables that simply *look* like standings
# tables (have a recognizable team column) and picks the group's
# "position"-th one - see find_all_standings_tables(). A stderr note is
# printed whenever this fallback is what actually found the table, so a
# stale config still gets flagged for a human to fix, without the
# standings themselves going missing from fixtures.json in the meantime.
#
# For pages with a variable, unpredictable number of same-shaped groups
# (e.g. one table per qualifying pool, however many pools exist that
# year), use "auto_groups": {"heading_pattern": r"regex"} instead of a
# fixed "groups" list - every heading whose id matches the pattern gets
# its own group, labelled with the heading's own text.
# ---------------------------------------------------------------------------

STANDINGS_COLUMN_KEYWORDS = {
    "team": ("team", "club", "country", "nation"),
    "played": ("pld", "gp", "played", "mp"),
    "win": ("w", "won", "wins"),
    "draw": ("d", "drawn", "draws", "tied", "t"),
    "loss": ("l", "lost", "losses"),
    "for": ("pf", "gf", "for"),
    "against": ("pa", "ga", "against"),
    "diff": ("diff", "+/-", "gd", "pd"),
    "points": ("pts", "points"),
}


def _standings_column_role(header_text):
    h = re.sub(r"[^a-z+/-]", "", header_text.strip().lower())
    for role, keywords in STANDINGS_COLUMN_KEYWORDS.items():
        if h in keywords:
            return role
    return None


def _standings_header_row(grid):
    """Unlike find_header_row() (which requires Home/Away columns for
    fixture tables), a standings header row just needs a recognizable
    team-ish column. Scans down from the top since some pages put a
    caption or a qualification-legend row before the real header."""
    for idx, row in enumerate(grid):
        roles = {}
        for i, cell in enumerate(row):
            role = _standings_column_role(cell)
            if role and role not in roles:
                roles[role] = i
        if "team" in roles:
            return idx, roles
    return None, {}


HIGHLIGHT_COLOR_RE = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,6}|[a-zA-Z]+)")


def _row_highlight_color(tr):
    """Wikipedia's standings tables/modules (e.g. Module:Sports table's
    col_XX/text_XX params) mark which rows advance/qualify/get relegated
    by giving that <tr> - or, sometimes, just its leading cell(s) - an
    inline background-color style, rather than any semantic class. Pull
    that color straight off the rendered HTML so the app can reproduce it
    (e.g. a pale green row = advances to the next round) instead of
    showing every row identically. Returns None if no such style is set."""
    style = tr.get("style", "") or ""
    m = HIGHLIGHT_COLOR_RE.search(style)
    if m:
        return m.group(1)
    for cell in tr.find_all(["td", "th"]):
        m = HIGHLIGHT_COLOR_RE.search(cell.get("style", "") or "")
        if m:
            return m.group(1)
    return None


def parse_standings_table(table):
    """Turn a rendered <table class="wikitable"> standings table into a
    list of {"team", "played", "win", "draw", "loss", "for", "against",
    "diff", "points", "highlight"} dicts (any field the table doesn't have
    stays None). Column order/wording varies by sport/page, so columns are
    matched by header keyword rather than position."""
    grid = table_to_grid(table)
    if not grid:
        return []
    header_idx, roles = _standings_header_row(grid)
    if header_idx is None:
        return []

    def get_num(row, role):
        if role not in roles or roles[role] >= len(row):
            return None
        raw = row[roles[role]].replace("\u2212", "-")
        m = re.search(r"-?\d+", raw)
        return int(m.group(0)) if m else None

    raw_trs = table.find_all("tr")

    rows_out = []
    for tr_idx, row in enumerate(grid[header_idx + 1:], start=header_idx + 1):
        if roles["team"] >= len(row):
            continue
        team = strip_citations(re.sub(r"\s+", " ", row[roles["team"]]).strip())
        # Strip footnote/marker artifacts some pages glue onto an otherwise
        # normal team name - a leading "x-"/"y-"/"z-" clinched-status
        # marker (common on North American standings pages), a trailing
        # "[a]"-style footnote reference (e.g. Super League's Warrington
        # Wolves/Hull KR groundshare notes), or a trailing "(H)"/"(A)"
        # host/away designation on an international qualifying page (e.g.
        # FIBA's host nations). Left in place these read as a second,
        # different-looking "team" for a club/nation that already has its
        # own proper row elsewhere.
        team = re.sub(r"^[xyz]\s*[-\u2013]\s*", "", team, flags=re.IGNORECASE)
        team = re.sub(r"\s*\[[a-zA-Z0-9]{1,3}\]\s*$", "", team)
        team = re.sub(r"\s*\((?:H|A)\)\s*$", "", team, flags=re.IGNORECASE)
        if not team or team.lower() in ("source", "notes", "key", "notes:"):
            continue
        stat_values = (
            get_num(row, "played"), get_num(row, "win"), get_num(row, "draw"),
            get_num(row, "loss"), get_num(row, "points"),
        )
        if all(v is None for v in stat_values):
            # No numeric stats at all - almost certainly a colspan'd
            # legend/footnote row (its text gets smeared across every
            # column by colspan expansion, including the team column),
            # not an actual team, e.g. "Qualification for Semifinals".
            continue
        highlight = _row_highlight_color(raw_trs[tr_idx]) if tr_idx < len(raw_trs) else None
        rows_out.append(
            {
                "team": team,
                "played": get_num(row, "played"),
                "win": get_num(row, "win"),
                "draw": get_num(row, "draw"),
                "loss": get_num(row, "loss"),
                "for": get_num(row, "for"),
                "against": get_num(row, "against"),
                "diff": get_num(row, "diff"),
                "points": get_num(row, "points"),
                "highlight": highlight,
            }
        )
    return rows_out


def extract_color_legend(table):
    """Best-effort scan for a legend mapping each highlight color to what
    it means (e.g. '#E8FFD8 -> Qualification for Semifinals'). Wikipedia
    standings tables usually carry this either as a single colspan'd row
    inside the table itself, or as a short list/paragraph immediately
    following the table, built out of small color-swatch spans followed by
    label text. Returns {color: label}; an empty dict just means the app
    shows colored rows without a text explanation, which is a fine
    degradation - not every page's legend markup is guessable in advance.
    """
    legend = {}

    def scan(container):
        for swatch in container.find_all(["span", "td", "th"]):
            m = HIGHLIGHT_COLOR_RE.search(swatch.get("style", "") or "")
            if not m:
                continue
            color = m.group(1).lower()
            if color in legend:
                continue
            label = swatch.get_text(" ", strip=True)
            if not label:
                parts, nxt = [], swatch.next_sibling
                while nxt is not None and not parts:
                    if getattr(nxt, "get", None) and HIGHLIGHT_COLOR_RE.search(nxt.get("style", "") or ""):
                        break
                    text = nxt if isinstance(nxt, str) else nxt.get_text(" ", strip=True)
                    if text and text.strip():
                        parts.append(text.strip())
                    nxt = nxt.next_sibling
                label = " ".join(parts).strip(" -\u2013:")
            if label and len(label) < 120:
                legend[color] = label

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) == 1 and cells[0].get("colspan"):
            scan(cells[0])

    sib, hops = table.find_next_sibling(), 0
    while sib is not None and hops < 4:
        if sib.name and (re.match(r"^h[1-6]$", sib.name) or sib.name == "table"):
            break
        scan(sib)
        sib = sib.find_next_sibling()
        hops += 1

    return legend


def build_group_result(table):
    """Bundle a table's rows + whatever color legend could be found for it
    into the shape stored in fixtures.json / consumed by matches.html:
    {"rows": [...], "legend": {color: label}}. Returns None if the table
    didn't actually contain any usable standings rows."""
    rows = parse_standings_table(table)
    if not rows:
        return None
    return {"rows": rows, "legend": extract_color_legend(table)}


def find_table_after_heading(soup, heading_id, position=0):
    """Return the Nth (0-indexed) table that appears after the heading
    with this id, stopping the search at the next heading. Deliberately
    does NOT require a 'wikitable' CSS class - template/module-rendered
    standings tables (e.g. EFA/CFL's standings templates, or Nations Cup's
    {{#invoke:Sports table}}) don't reliably carry that class the way a
    hand-written wikitext table does, even though the rendered output is a
    perfectly normal <table>. The real validation happens one level up in
    parse_standings_table() (via _standings_header_row's requirement of a
    recognizable team column) - this just needs to find candidate tables,
    not judge them. Returns None if not found (a common/expected outcome
    if a page's real section titles differ from what's configured -
    callers should warn, not crash)."""
    anchor = soup.find(id=heading_id)
    if anchor is None:
        return None

    count = 0
    for el in anchor.find_all_next():
        if el.name == "table":
            if count == position:
                return el
            count += 1
            continue
        if el.name and re.match(r"^h[1-6]$", el.name):
            break
        if el.get("class") and "mw-heading" in el.get("class"):
            break
    return None


def find_all_standings_tables(soup):
    """Scan the ENTIRE page, in document order, for every <table> that
    parse_standings_table() can turn into actual rows (i.e. it has a
    recognizable team column - see _standings_header_row). This is the
    fallback used when a configured heading_id can't be found, or is
    found but doesn't sit above a real standings table any more.

    This is deliberately heading-agnostic: NRL/Super League/EFA/CFL (and
    presumably others over time) have all been observed drifting away
    from whatever heading id or page a "standings" config was written
    against - Wikipedia editors rename sections ("Standings" -> "Table"
    -> "League table"), or move the table to a different article
    entirely (Super League's table lives on the season overview page,
    not the "_season_results" page fixtures come from). Rather than
    chasing each rename, this just finds every table on the page that
    *looks* like a standings table by shape, and callers pick the one
    they want by position - which is stable even when the heading text
    around it isn't."""
    tables = []
    for table in soup.find_all("table"):
        rows = parse_standings_table(table)
        if rows:
            tables.append((table, rows))
    return tables


def _resolve_group_table(soup, group, fallback_tables):
    """Locate one group's standings table, trying (in order):
      0. "template": fetch the named Template: page directly and use
         whatever standings-shaped table(s) it contains. This is by far
         the most reliable option when the standings are a bare
         transclusion like "{{NRL2026Ladder}}" or
         "{{2026 Super League regular season table}}" on the season
         page - the table data doesn't actually live on the season page
         at all, it lives entirely on the Template: page, so fetching
         and parsing the season page's rendered HTML for it is fetching
         the wrong document. Going straight to the template page instead
         sidesteps every heading-drift/page-restructure issue at once,
         since the template's own page basically never has anything on
         it BUT the table.
      1. every heading id the group names (heading_id, or heading_ids for
         when a section is known to go by more than one name)
      2. an explicit table_index (Nth table on the whole page)
      3. position-th entry among every table on the page that looks like
         a real standings table (see find_all_standings_tables) - the
         last-resort fallback for when the heading itself has moved/been
         renamed and neither of the above still lines up.
    Returns (rows, how) where `how` describes which strategy worked, for
    logging - or (None, None) if nothing worked at all.
    """
    position = group.get("position", 0)

    if "template" in group:
        template_page = f"Template:{group['template']}"
        try:
            template_html = fetch_page_html(template_page)
        except Exception as e:
            print(f"  !! standings: couldn't fetch {template_page!r}: {e}", file=sys.stderr)
        else:
            template_soup = BeautifulSoup(template_html, "html.parser")
            template_tables = find_all_standings_tables(template_soup)
            template_position = group.get("template_position", 0)
            if template_position < len(template_tables):
                tbl, rows = template_tables[template_position]
                if rows:
                    return tbl, rows, f"template {group['template']!r}"

    heading_ids = group.get("heading_ids")
    if heading_ids is None:
        heading_ids = [group["heading_id"]] if "heading_id" in group else []

    for heading_id in heading_ids:
        table = find_table_after_heading(soup, heading_id, position)
        if table is not None:
            rows = parse_standings_table(table)
            if rows:
                return table, rows, f"heading {heading_id!r}"

    if "table_index" in group:
        idx = group["table_index"]
        all_tables = soup.find_all("table")
        if idx < len(all_tables):
            table = all_tables[idx]
            rows = parse_standings_table(table)
            if rows:
                return table, rows, f"table_index {idx}"

    # Last resort: position-th table on the whole page that actually
    # looks like a standings table (has a recognizable team column plus
    # at least one stat column). Only used when everything above missed,
    # so a genuinely absent standings section still falls through to the
    # "couldn't find" warning below rather than grabbing an unrelated
    # table.
    if position < len(fallback_tables):
        tbl, rows = fallback_tables[position]
        if rows:
            return tbl, rows, f"page-wide fallback (position {position})"

    return None, None, None


def heading_tag_by_id(soup, heading_id):
    """Find the h1-h6 tag for a given heading id (or the first id in a
    list of candidates that actually exists on the page), tolerating both
    id-on-the-heading-tag (current MediaWiki) and id-on-a-nested
    <span class="mw-headline"> (older convention). Returns None if none of
    the candidates are found."""
    candidates = heading_id if isinstance(heading_id, (list, tuple)) else [heading_id]
    for hid in candidates:
        el = soup.find(id=hid)
        if el is None:
            continue
        if el.name and re.match(r"^h[1-6]$", el.name):
            return el
        parent = el.find_parent(re.compile(r"^h[1-6]$"))
        if parent is not None:
            return parent
    return None


def collect_subsection_tables(soup, heading_tag, label_prefix):
    """Within heading_tag's section (i.e. up to, but not including, the
    next heading at the same-or-higher level), find every deeper heading
    that has a standings-shaped table following it, and return
    {"label_prefix · sub-heading text": {"rows": [...], "legend": {...}}}.
    Deliberately doesn't care what the sub-headings are called ("Group A",
    "Best fourth-placed team", "Ranking of second-placed teams", ...) - if
    it has a real standings table under it, it gets included."""
    if heading_tag is None:
        return {}
    level = int(heading_tag.name[1])
    result = {}
    seen_ids = set()

    def maybe_add(hid, text):
        if not hid or hid in seen_ids:
            return
        seen_ids.add(hid)
        table = find_table_after_heading(soup, hid)
        if table is None:
            return
        group_result = build_group_result(table)
        if not group_result:
            return
        label = text or hid.replace("_", " ")
        key = f"{label_prefix} \u00b7 {label}" if label_prefix else label
        result[key] = group_result

    for el in heading_tag.find_all_next():
        if el.name and re.match(r"^h[1-6]$", el.name):
            if int(el.name[1]) <= level:
                break
            maybe_add(el.get("id"), el.get_text(" ", strip=True))
        elif el.name == "span" and el.get("class") and "mw-headline" in el.get("class"):
            maybe_add(el.get("id"), el.get_text(" ", strip=True))

    return result


def fetch_phased_standings(soup, phases_cfg, key):
    """Resolve a "phases" standings config (see the FIBA league entries in
    LEAGUES) into {breadcrumb_label: {"rows": [...], "legend": {...}}}."""
    result = {}
    for phase in phases_cfg:
        phase_tag = heading_tag_by_id(soup, phase["heading_id"])
        if phase_tag is None:
            print(
                f"  !! standings: {key} - phase heading {phase['heading_id']!r} not found, "
                f"skipping {phase['label']!r} (heading id may need updating)",
                file=sys.stderr,
            )
            continue
        if "rounds" in phase:
            for rnd in phase["rounds"]:
                round_tag = heading_tag_by_id(soup, rnd["heading_id"])
                if round_tag is None:
                    print(
                        f"  !! standings: {key} - round heading {rnd['heading_id']!r} not found "
                        f"under {phase['label']!r} (heading id may need updating)",
                        file=sys.stderr,
                    )
                    continue
                result.update(
                    collect_subsection_tables(soup, round_tag, f"{phase['label']} \u00b7 {rnd['label']}")
                )
        else:
            result.update(collect_subsection_tables(soup, phase_tag, phase["label"]))
    return result


def fetch_standings(cfg, key):
    """Fetch and parse whatever standings tables a league's config
    describes. Returns {group_label: {"rows": [...], "legend": {...}}};
    an empty dict if the league has no "standings" config, or if none of
    its configured groups could be located on the page (a stderr warning
    is printed per missing group so a stale heading id/page title shows up
    immediately).

    Heading ids and even the page containing the table can drift out of
    sync with a "standings" config over time as Wikipedia editors rename
    sections or restructure articles - this has actually happened to
    nrl-2026, super-league-2026, efa-2026 and cfl-2026 in the wild. To
    stay resilient to that, each group is resolved via
    _resolve_group_table(), which falls back to a page-wide scan for
    standings-shaped tables (by position) when the configured heading_id
    no longer points at one - see find_all_standings_tables()."""
    standings_cfg = cfg.get("standings")
    if not standings_cfg:
        return {}

    page = standings_cfg["page"]
    html = fetch_page_html(page)
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    if "phases" in standings_cfg:
        return fetch_phased_standings(soup, standings_cfg["phases"], key)

    if "auto_groups" in standings_cfg:
        pattern = re.compile(standings_cfg["auto_groups"]["heading_pattern"])
        seen_ids = set()
        # Headings can carry their id directly on the h1-h6 tag (current
        # MediaWiki convention) or on an inner <span class="mw-headline">
        # (older convention, still seen on some page histories) - checking
        # both means this doesn't depend on which one a given page uses.
        candidates = soup.find_all(re.compile(r"^h[1-6]$"), id=True)
        candidates += soup.find_all("span", class_="mw-headline", id=True)
        for heading in candidates:
            heading_id = heading.get("id")
            if not heading_id or heading_id in seen_ids or not pattern.match(heading_id):
                continue
            seen_ids.add(heading_id)
            table = find_table_after_heading(soup, heading_id)
            if table is None:
                continue
            label = heading.get_text(" ", strip=True) or heading_id.replace("_", " ")
            group_result = build_group_result(table)
            if group_result:
                result[label] = group_result
        return result

    # Built lazily (only once, and only if at least one group needs it)
    # since it parses every table on the page.
    fallback_tables = None

    for group in standings_cfg.get("groups", []):
        if fallback_tables is None:
            fallback_tables = find_all_standings_tables(soup)

        table, rows, how = _resolve_group_table(soup, group, fallback_tables)

        if rows is None:
            print(
                f"  !! standings: couldn't find table for {key} / {group['label']!r} "
                f"on page {page!r} - heading id or page title may need updating",
                file=sys.stderr,
            )
            continue

        if how and how.startswith("page-wide fallback"):
            print(
                f"  ?? standings: {key} / {group['label']!r} located via {how} - "
                f"configured heading id(s) didn't match on page {page!r}, "
                f"consider updating the config",
                file=sys.stderr,
            )

        result[group["label"]] = {"rows": rows, "legend": extract_color_legend(table)}

    return result


def parse_attendance_table(table):
    """Turn a rendered <table> from a season page's official attendance
    summary section (e.g. NRL's "==Attendances== -> ===Club figures==="")
    into a list of {"team", "games", "total", "average"} dicts.

    Column order on these pages is Team | Games | Total | <year> Average |
    <prior year> Average | Difference | Highest | Lowest - only the first
    "*Average" column found is used (the current-season one; it's always
    the first of the two "Average" columns on the page), and Difference/
    Highest/Lowest are ignored entirely since the app doesn't show them.

    A trailing "*" on a Games figure (Wikipedia's own footnote marker,
    e.g. "8*" meaning "Magic Round home game not counted") is just a
    footnote symbol - the number itself already excludes that game, which
    is exactly the behavior wanted here, so no extra filtering is needed."""
    grid = table_to_grid(table)
    if not grid:
        return []

    header_idx, col = None, {}
    for idx, row in enumerate(grid):
        lower = [re.sub(r"\s+", " ", c).strip().lower() for c in row]
        if "team" not in lower or not any("game" in c for c in lower):
            continue
        header_idx = idx
        for i, c in enumerate(lower):
            if c == "team":
                col.setdefault("team", i)
            elif "game" in c:
                col.setdefault("games", i)
            elif c == "total":
                col.setdefault("total", i)
            elif re.match(r"^\d{4}\s+average$", c):
                col.setdefault("average", i)
        break
    if header_idx is None or "team" not in col:
        return []

    def get_int(row, role):
        if role not in col or col[role] >= len(row):
            return None
        raw = row[col[role]].replace(",", "")
        m = re.search(r"\d+", raw)
        return int(m.group(0)) if m else None

    rows_out = []
    for row in grid[header_idx + 1:]:
        if col["team"] >= len(row):
            continue
        team = strip_citations(re.sub(r"\s+", " ", row[col["team"]]).strip())
        if not team:
            continue
        games = get_int(row, "games")
        if games is None:
            # Almost certainly a footnote/legend row (e.g. the "* = Magic
            # Round home game not counted" line), not an actual club.
            continue
        rows_out.append({
            "team": team,
            "games": games,
            "total": get_int(row, "total"),
            "average": get_int(row, "average"),
        })
    return rows_out


def fetch_attendance_table(cfg, key):
    """Fetch a league's official per-club attendance summary table, for
    leagues whose config names one via "attendance_table" (currently just
    NRL). Returns a list of {"team","games","total","average"} dicts, or
    an empty list if not configured, or not found on the page.

    Used INSTEAD of computing attendance from individual match records for
    leagues that have a Wikipedia-maintained summary table already
    handling quirks (like NRL's Vegas/Magic Round neutral-venue games)
    that a naive "count every home fixture in fixtures.json" computation
    can't replicate without separately tagging which fixtures were
    neutral-venue - the official table already has this baked in."""
    att_cfg = cfg.get("attendance_table")
    if not att_cfg:
        return []

    html = fetch_page_html(att_cfg["page"])
    soup = BeautifulSoup(html, "html.parser")

    heading_tag = heading_tag_by_id(soup, att_cfg.get("heading_ids") or att_cfg.get("heading_id"))
    table = find_table_after_heading(soup, heading_tag.get("id")) if heading_tag is not None else None

    if table is not None:
        rows = parse_attendance_table(table)
        if rows:
            return rows

    # Configured heading id (or the table right after it) didn't pan out -
    # fall back to a page-wide scan for any table shaped like an
    # attendance summary (has a recognizable Team + Games column pair),
    # same resilience strategy as find_all_standings_tables() uses for
    # standings tables whose heading has drifted.
    for candidate in soup.find_all("table"):
        rows = parse_attendance_table(candidate)
        if rows:
            return rows

    print(
        f"  !! attendance table: couldn't find one for {key} on page "
        f"{att_cfg['page']!r} - heading id or page title may need updating",
        file=sys.stderr,
    )
    return []


# How far ahead/behind "now" a match has to be to have its stored fields
# refreshed on this run. This does NOT control what's displayed - the app
# shows the full season, every run - it only controls which matches are
# worth re-checking against Wikipedia's current text: what's live/
# imminent, or what just finished (attendance figures sometimes land a
# few days after full time). Anything else keeps whatever's already
# stored rather than being re-parsed every run.
SCRAPE_WINDOW_PAST = timedelta(days=3)
SCRAPE_WINDOW_FUTURE = timedelta(days=1)


def _match_instant(m):
    """Best-effort datetime for a match, for scrape-window purposes only
    (display logic in matches.html has its own, separate notion of
    upcoming/live/final). Prefers the true UTC instant; falls back to the
    venue-local date/time treated as if it were UTC, which is close enough
    for a +/- several day window even though it's not precisely correct."""
    if m.get("utc"):
        try:
            return datetime.fromisoformat(m["utc"])
        except ValueError:
            pass
    if m.get("date"):
        try:
            time_part = m.get("time") or "00:00"
            return datetime.strptime(f"{m['date']} {time_part}", "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return None


def within_scrape_window(m, now):
    """True if this match's kickoff is within the last 7 days or the next
    24 hours - i.e. worth refreshing from a fresh parse. Matches with no
    usable date at all are treated as always-eligible, since there's no
    window to check them against and a stray unmatched fixture is better
    than one that silently never updates."""
    instant = _match_instant(m)
    if instant is None:
        return True
    return (now - SCRAPE_WINDOW_PAST) <= instant <= (now + SCRAPE_WINDOW_FUTURE)


def _match_identity(m):
    """Identity used to match a freshly parsed match against one already
    stored from a previous run, so an update can be applied in place
    instead of appended as a duplicate.

    Teams are stored in sorted order so that a match previously stored
    with swapped home/away (e.g. from an older EFA/AFLE table format that
    had inverted column labels) is recognised as the same fixture after the
    swap logic was corrected, rather than producing a second duplicate
    entry."""
    pair = tuple(sorted([m.get("home") or "", m.get("away") or ""]))
    return (pair, m.get("date"))


def merge_league_matches(existing_matches, freshly_parsed_matches, now):
    """Combine what's already stored for a league with a fresh parse of
    the page, so fixtures.json always holds the full season (past and
    future) for the app to browse, while only the matches actually inside
    the scrape window get their fields refreshed:

      - A match already stored AND inside the window: replaced with the
        freshly parsed version (picks up new scores, attendance, or a
        kick-off time change).
      - A match already stored but outside the window: left exactly as
        stored - not re-parsed, not touched.
      - A match that's brand new (wasn't stored before): always added,
        regardless of window, since a newly published fixture should show
        up right away rather than waiting for its own window to arrive.

    Once a match has been scraped into fixtures.json it never disappears
    on a later run - the only two things that ever happen to a match on a
    subsequent run are (a) getting its fields refreshed, if it's inside
    the window, or (b) a brand new one getting appended.

    Known limitation: matches are matched between runs by (home, away,
    date). If Wikipedia moves a fixture to a materially different date -
    a postponement spotted outside the usual next-24h window - this can't
    recognize it as "the same match, new date"; it gets added as a new
    entry and the stale old-dated entry is left behind rather than
    replaced.
    """
    # De-duplicate existing_matches by identity before loading - a league
    # may have ended up with two entries for the same fixture under different
    # home/away orderings (e.g. after the EFA/AFLE column-swap logic was
    # updated).  Keep the entry with the most information (prefer the one
    # that has a score, or if tied, the one that has a date).
    deduped_existing = {}
    for m in existing_matches:
        ident = _match_identity(m)
        if ident not in deduped_existing:
            deduped_existing[ident] = m
        else:
            prev = deduped_existing[ident]
            if (m.get("score") is not None and prev.get("score") is None) or \
               (m.get("date") is not None and prev.get("date") is None):
                deduped_existing[ident] = m
    existing_matches = list(deduped_existing.values())

    merged = {_match_identity(m): m for m in existing_matches}

    # Secondary index: team pairs (without date) for stale entries whose
    # date was None when first stored.  Used below to replace a dateless
    # entry once the page finally publishes a date for that fixture.
    pair_to_dateless_key = {
        ident[0]: ident
        for ident in merged
        if ident[1] is None
    }

    for m in freshly_parsed_matches:
        ident = _match_identity(m)
        # Fresh match has a real date but we have a dateless stale entry for
        # the same team pair: replace the stale entry (always - a newly
        # published date is exactly the kind of backfill we want).
        if ident[1] is not None and ident not in merged:
            stale_key = pair_to_dateless_key.get(ident[0])
            if stale_key is not None:
                del merged[stale_key]
                pair_to_dateless_key.pop(ident[0], None)
        if ident not in merged:
            merged[ident] = m
        elif within_scrape_window(m, now):
            merged[ident] = m
        else:
            # Outside the scrape window, so don't let a fresh parse
            # overwrite anything already stored (that's what the window is
            # for - a finished match's score shouldn't flip-flop based on
            # page-edit noise). But DO backfill any field that's still
            # None on the stored record - e.g. a "group"/"round" bracket
            # tag that a stale record never got (because tag_sections was
            # added after this match was first scraped, or the heading
            # breadcrumb failed to resolve at the time). A missing field
            # getting filled in is never a "flip-flop"; there's nothing to
            # protect by leaving it None forever.
            stored = merged[ident]
            for field, value in m.items():
                if stored.get(field) is None and value is not None:
                    stored[field] = value
    return list(merged.values())


def load_existing():
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("standings", {})
        return data
    return {"updated": None, "leagues": {}, "matches": [], "standings": {}}


def save(data):
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_and_parse(cfg, key, cached=None, now=None):
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

    if parser_type == "rugbybox2":
        matches = []
        for page in pages:
            wikitext = fetch_page_wikitext(page)
            matches.extend(parse_rugbybox2_matches(wikitext, key, cfg))
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
        # Unlike every other parser here, each CFL team has its own,
        # independent page - so unlike a single shared results page (which
        # always has to be fetched in full to check anything at all), a
        # team whose page has no home game due within the scrape window
        # genuinely doesn't need to be re-fetched this run. This is the
        # one place fetching itself (not just parsing) can actually be
        # skipped based on the window, cutting this league from 9 fetches
        # a run down to typically 1-3.
        home_games_by_team = {}
        for m in (cached or []):
            home_games_by_team.setdefault(m.get("home"), []).append(m)

        matches = []
        seen = set()
        skipped = 0
        for page, team_name in cfg["team_pages"].items():
            team_games = home_games_by_team.get(team_name, [])
            due_soon = any(within_scrape_window(m, now) for m in team_games) if now else True
            if team_games and not due_soon:
                # We've fetched this team before and nothing of theirs
                # (home game) is due soon - reuse what's stored instead of
                # re-fetching. (An empty team_games list means we've never
                # successfully parsed this team at all, e.g. first run or
                # a prior fetch failure - always fetch in that case.)
                skipped += 1
                matches.extend(team_games)
                continue
            wikitext = fetch_page_wikitext(page)
            for m in parse_cfl_schedule(wikitext, key, cfg, team_name):
                # Safety net alongside the home-row-only filter in
                # parse_cfl_schedule, in case a page ever double-lists a game.
                sig = (m["date"], m["home"], m["away"])
                if sig in seen:
                    continue
                seen.add(sig)
                matches.append(m)
        if skipped:
            print(f"  -> CFL: skipped re-fetching {skipped}/{len(cfg['team_pages'])} "
                  f"team pages (no home game due soon, reused stored data)")
        return matches

    raise ValueError(f"Unknown parser type '{parser_type}' for league '{key}'")


def run(league_keys, force=False):
    data = load_existing()
    data.setdefault("leagues", {})
    data.setdefault("standings", {})
    data.setdefault("attendance_tables", {})
    now = datetime.now(timezone.utc)

    existing_by_league = {}
    for m in data.get("matches", []):
        existing_by_league.setdefault(m["league"], []).append(m)

    # Matches for leagues being processed this run get re-added below -
    # either freshly merged, or (if skipped) carried forward unchanged -
    # so nothing is dropped here. Leagues NOT in league_keys are untouched.
    data["matches"] = [m for m in data.get("matches", []) if m["league"] not in league_keys]

    for key in league_keys:
        cfg = LEAGUES[key]
        cached = existing_by_league.get(key, [])

        # Nothing-due-soon check, straight from fixtures.json (this
        # script's own prior output - the only place with per-match dates
        # for every league). If we've successfully fetched this league
        # before (cached is non-empty) and none of what we found then
        # falls inside the current scrape window, there's nothing to
        # check on Wikipedia right now - skip the fetch (and standings
        # fetch) entirely and just keep what's stored. An empty `cached`
        # means we've never successfully fetched this league (first run,
        # or every prior attempt failed) - always fetch in that case.
        # --force bypasses this, for an occasional full run to catch a
        # newly-published fixture or postponement this check can't see
        # (it only knows about matches already on record).
        if not force and cached and not any(within_scrape_window(m, now) for m in cached):
            print(f"Skipping {cfg['name']} fixtures - nothing in its {len(cached)} stored "
                  f"matches falls within the scrape window; reusing as-is "
                  f"(use --force to check anyway)")
            data["matches"].extend(cached)
            data["leagues"].setdefault(key, {"name": cfg["name"], "sport": cfg["sport"]})
            # Standings are NOT tied to the fixture scrape window above - a
            # ladder/table can change every time a match is played regardless
            # of whether any of THIS league's remaining fixtures happen to
            # fall inside the fixture-refresh window right now. Skipping this
            # unconditionally on the fixtures-skip path (as a previous
            # version of this function did) meant any league whose standings
            # config had a mismatched heading/page the first time it ran
            # would silently never get another chance to pick up standings
            # again - which is exactly what happened to nrl-2026,
            # super-league-2026, efa-2026, cfl-2026, u20-jwc-2026, and
            # nations-cup-2026 in the wild. So this always re-fetches
            # standings, even when fixtures themselves are skipped.
            try:
                standings = fetch_standings(cfg, key)
                if standings:
                    data["standings"][key] = standings
                    print(f"  -> standings: {len(standings)} table(s) for {cfg['name']}")
            except Exception as e:
                print(f"  !! standings failed: {e}", file=sys.stderr)
            # Same reasoning as standings above: not tied to the fixture
            # scrape window, always re-checked even when fixtures are
            # skipped, so a mismatched heading gets another chance next
            # run instead of silently never picking up attendance data.
            try:
                attendance_rows = fetch_attendance_table(cfg, key)
                if attendance_rows:
                    data["attendance_tables"][key] = attendance_rows
                    print(f"  -> attendance table: {len(attendance_rows)} club row(s) for {cfg['name']}")
            except Exception as e:
                print(f"  !! attendance table failed: {e}", file=sys.stderr)
            continue

        if "team_pages" in cfg:
            source_desc = f"{len(cfg['team_pages'])} team pages"
        else:
            pages = cfg.get("pages") or ([cfg["page"]] if "page" in cfg else [])
            source_desc = ", ".join(pages)
        print(f"Fetching {cfg['name']} ({source_desc}) ...")
        try:
            fresh_matches = fetch_and_parse(cfg, key, cached=cached, now=now)
            if not fresh_matches:
                print("  !! no matches parsed - the page's match-box markup may differ, "
                      "check LEAGUES config / page name", file=sys.stderr)

            fresh_by_ident = {_match_identity(m): m for m in fresh_matches}
            stored_idents = {_match_identity(m) for m in cached}
            refreshed = sum(
                1 for ident, m in fresh_by_ident.items()
                if ident in stored_idents and within_scrape_window(m, now)
            )
            added = sum(1 for ident in fresh_by_ident if ident not in stored_idents)

            merged = merge_league_matches(cached, fresh_matches, now)
            print(f"  -> parsed {len(fresh_matches)} matches on the page: "
                  f"{refreshed} refreshed (in scrape window), {added} newly added, "
                  f"{len(merged)} total now stored (was {len(cached)})")

            data["matches"].extend(merged)
            data["leagues"][key] = {"name": cfg["name"], "sport": cfg["sport"]}
        except Exception as e:
            print(f"  !! failed: {e}", file=sys.stderr)
            # Fetch failed outright - keep whatever was already stored
            # rather than losing the league's fixtures for this run.
            # data["leagues"][key] already holds the prior entry (if any)
            # from load_existing(), since it's only ever overwritten on a
            # successful parse above.
            data["matches"].extend(cached)

        try:
            standings = fetch_standings(cfg, key)
            if standings:
                data["standings"][key] = standings
                group_count = len(standings)
                print(f"  -> standings: {group_count} table(s) for {cfg['name']}")
            # An empty/failed standings fetch leaves data["standings"][key]
            # exactly as loaded, rather than wiping previously-known
            # standings just because this particular run didn't refresh them.
        except Exception as e:
            print(f"  !! standings failed: {e}", file=sys.stderr)

        try:
            attendance_rows = fetch_attendance_table(cfg, key)
            if attendance_rows:
                data["attendance_tables"][key] = attendance_rows
                print(f"  -> attendance table: {len(attendance_rows)} club row(s) for {cfg['name']}")
        except Exception as e:
            print(f"  !! attendance table failed: {e}", file=sys.stderr)

    data["matches"].sort(key=lambda m: (m["date"] or "9999-99-99", m["time"] or "99:99"))
    save(data)
    print(f"Wrote {len(data['matches'])} total matches to {OUTPUT_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Fetch sports fixtures from Wikipedia.")
    parser.add_argument("leagues", nargs="*", help="league keys to fetch (default: all)")
    parser.add_argument("--list", action="store_true", help="list configured leagues and exit")
    parser.add_argument(
        "--force", action="store_true",
        help="check every league even if nothing in its stored matches falls within "
             "the scrape window (by default those leagues are skipped entirely - use "
             "this occasionally to catch newly-published fixtures or postponements)",
    )
    args = parser.parse_args()

    if args.list:
        for key, cfg in LEAGUES.items():
            print(f"  {key:20s} {cfg['name']}")
        return

    # Default (no leagues named) skips anything marked "completed" - those
    # tournaments are over and their stored data (matches + standings) is
    # left untouched rather than re-fetched every run. Naming a completed
    # league explicitly still works, for a manual one-off re-check.
    keys = args.leagues or [k for k, cfg in LEAGUES.items() if not cfg.get("completed")]
    unknown = [k for k in keys if k not in LEAGUES]
    if unknown:
        print(f"Unknown league key(s): {', '.join(unknown)}", file=sys.stderr)
        print("Use --list to see configured leagues.", file=sys.stderr)
        sys.exit(1)

    run(keys, force=args.force)


if __name__ == "__main__":
    main()