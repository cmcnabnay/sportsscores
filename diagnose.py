#!/usr/bin/env python3
"""
diagnose.py

Fetches the raw HTML for the configured Wikipedia page and prints out
clues about how matches are actually marked up on this specific page,
so we can fix fetch_fixtures.py's parser to match reality instead of
guessing.

Usage:
    python3 diagnose.py                                    # defaults to the JWC page
    python3 diagnose.py 2026_Nations_Championship           # or check any other page
    python3 diagnose.py "2026_FIVB_Men's_Volleyball_Nations_League"
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

PAGE = sys.argv[1] if len(sys.argv) > 1 else "2026_World_Rugby_Junior_World_Championship"
API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "fixtures-fetcher/1.0 (personal project; contact: cmcnabnay)"}


def fetch_page_html(page_title: str) -> str:
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
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["parse"]["text"]


def main():
    html = fetch_page_html(PAGE)

    # Save the raw HTML so we can inspect it directly if needed
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", PAGE)
    raw_path = Path(f"page_raw_{safe_name}.html")
    raw_path.write_text(html, encoding="utf-8")
    print(f"Saved raw HTML to {raw_path.resolve()} ({len(html)} chars)\n")

    soup = BeautifulSoup(html, "html.parser")

    print("=== itemtype attributes found on the page ===")
    itemtypes = {}
    for el in soup.select("[itemtype]"):
        t = el["itemtype"]
        itemtypes[t] = itemtypes.get(t, 0) + 1
    if itemtypes:
        for t, count in itemtypes.items():
            print(f"  {count:4d}  {t}")
    else:
        print("  (none found - this page does not use schema.org microdata for matches)")

    print("\n=== itemprop attributes found on the page ===")
    itemprops = {}
    for el in soup.select("[itemprop]"):
        p = el["itemprop"]
        itemprops[p] = itemprops.get(p, 0) + 1
    if itemprops:
        for p, count in itemprops.items():
            print(f"  {count:4d}  {p}")
    else:
        print("  (none found)")

    print("\n=== table/div class names containing 'match', 'fixture', 'score', 'rugby', 'football', 'vs', 'event' ===")
    keywords = ["match", "fixture", "score", "rugby", "football", "vevent", "event", "team"]
    classes = {}
    for el in soup.find_all(class_=True):
        for c in el.get("class", []):
            lc = c.lower()
            if any(k in lc for k in keywords):
                classes[c] = classes.get(c, 0) + 1
    if classes:
        for c, count in sorted(classes.items(), key=lambda x: -x[1]):
            print(f"  {count:4d}  class=\"{c}\"")
    else:
        print("  (none found)")

    print("\n=== First match box: text pulled out by class name ===")
    events = soup.select('[itemtype="http://schema.org/SportsEvent"]')
    print(f"  (found {len(events)} total)\n")
    if events:
        ev = events[0]
        print("Full text content of this box:")
        print(" ", ev.get_text(" | ", strip=True))
        print()
        for cls in ["fn", "org", "vcard", "location", "attendee", "summary", "dtstart", "dt-start"]:
            found = ev.find_all(class_=cls)
            if found:
                print(f"elements with class=\"{cls}\":")
                for f in found:
                    print(f"    -> {f.get_text(' ', strip=True)!r}")
        print()
        print("Outer tag of the match box itself:")
        print(" ", ev.name, dict(ev.attrs))

    print("\n=== Second match box, same breakdown (in case box #1 is atypical) ===")
    if len(events) > 1:
        ev = events[1]
        print("Full text content of this box:")
        print(" ", ev.get_text(" | ", strip=True))
        print()
        for cls in ["fn", "org", "vcard", "location", "attendee", "summary", "dtstart", "dt-start"]:
            found = ev.find_all(class_=cls)
            if found:
                print(f"elements with class=\"{cls}\":")
                for f in found:
                    print(f"    -> {f.get_text(' ', strip=True)!r}")

    if not events:
        print("\n=== No vevent/SportsEvent boxes found - this page likely uses plain wikitables instead ===")
        tables = soup.find_all("table", class_=re.compile("wikitable"))
        print(f"  (found {len(tables)} wikitable(s) on the page)\n")
        for i, table in enumerate(tables[:3]):
            rows = table.find_all("tr")
            print(f"--- wikitable #{i+1}: {len(rows)} rows, classes={table.get('class')} ---")
            for row in rows[:4]:
                cells = row.find_all(["th", "td"])
                cell_texts = [c.get_text(" ", strip=True) for c in cells]
                print("   ", cell_texts)
            print()


if __name__ == "__main__":
    main()
