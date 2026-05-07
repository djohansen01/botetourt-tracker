#!/usr/bin/env python3
"""
Botetourt County Government Transparency Scraper
Monitors botetourtva.gov for budget PDFs, meeting agendas/minutes,
news alerts, and the Virginia APA audit page.
Outputs data.json and changelog.json.
"""

from __future__ import annotations

import json
import hashlib
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

URLS = {
    "finance":   "https://www.botetourtva.gov/348/Finance",
    "agendas":   "https://www.botetourtva.gov/AgendaCenter",
    "home":      "https://www.botetourtva.gov",
    "apa":       "https://www.apa.virginia.gov/Audits/PublishedAudits.aspx",
    "apa_search": "https://www.apa.virginia.gov/Audits/PublishedAudits.aspx?search=botetourt",
    "calendar":   "https://www.botetourtva.gov/Calendar.aspx",
    "legal_notices": "https://www.botetourtva.gov/814/Legal-Public-Notices",
    "financial_reports": "https://www.botetourtva.gov/992/Financial-Reports",
    "bids":       "https://www.botetourtva.gov/Bids.aspx",
}

OUTPUT_FILE    = Path("data.json")
CHANGELOG_FILE = Path("changelog.json")
HASHES_FILE    = Path(".scraper_hashes.json")

OFFICIALS = [
    {"name": "Steve Clinton",   "district": "Amsterdam",   "role": "Board of Supervisors"},
    {"name": "Walter Michael",  "district": "Blue Ridge",  "role": "Board of Supervisors"},
    {"name": "Amy White",       "district": "Buchanan",    "role": "Board of Supervisors"},
    {"name": "Brandon Nicely",  "district": "Fincastle",   "role": "Board of Supervisors"},
    {"name": "Mac Scothorn",    "district": "Valley",      "role": "Board of Supervisors"},
]

BUDGET = {
    "fiscal_year": "FY2026",
    "total_budget_millions": 98.8,
    "education_millions": 32.4,
    "tax_rate_increase": False,
    "note": "No tax rate increase. Education + Public Safety ≈ 64% of total budget.",
    "breakdown": [
        {"category": "Education",          "millions": 32.4,  "percent": 32.8},
        {"category": "Public Safety",      "millions": 30.8,  "percent": 31.2},
        {"category": "General Government", "millions": 14.0,  "percent": 14.2},
        {"category": "Public Works",       "millions": 10.2,  "percent": 10.3},
        {"category": "Health & Welfare",   "millions":  6.5,  "percent":  6.6},
        {"category": "Other",              "millions":  4.9,  "percent":  5.0},
    ],
    "source_url": "https://www.botetourtva.gov/348/Finance",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BotetourtTransparencyBot/1.0; "
        "+https://github.com/botetourt-tracker)"
    )
}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  URL error fetching {url}: {e.reason}", file=sys.stderr)
    except Exception as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
    return None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

class LinkParser(HTMLParser):
    """Collects (href, text) tuples from anchor tags."""

    def __init__(self, base_url: str = ""):
        super().__init__()
        self.links: list[dict] = []
        self.base_url = base_url
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href:
                if href.startswith("http"):
                    self._current_href = href
                elif self.base_url:
                    self._current_href = urllib.parse.urljoin(self.base_url, href)
                else:
                    self._current_href = href
                self._current_text = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            text = " ".join(self._current_text).strip()
            if text:
                self.links.append({"url": self._current_href, "text": text})
            self._current_href = None
            self._current_text = []


class MetaParser(HTMLParser):
    """Extracts <title> and <meta name="description"> content."""

    def __init__(self):
        super().__init__()
        self.title: str = ""
        self.description: str = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            d = dict(attrs)
            if d.get("name", "").lower() == "description":
                self.description = d.get("content", "")

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

PDF_PATTERN     = re.compile(r"\.pdf", re.IGNORECASE)
BUDGET_PATTERN  = re.compile(r"budget|fy\s*20\d\d|fiscal|financial.?report|acfr|cafr|annual.?report", re.IGNORECASE)
AGENDA_PATTERN  = re.compile(r"agenda|minute|meeting|board", re.IGNORECASE)
AUDIT_PATTERN   = re.compile(r"botetourt", re.IGNORECASE)
NEWS_PATTERN    = re.compile(r"alert|news|notice|announcement|press.?release", re.IGNORECASE)
NOTICE_PATTERN  = re.compile(r"notice|public.?hearing|legal.?ad|ordinance|rezoning|variance|petition|zoning.?map", re.IGNORECASE)
BID_PATTERN     = re.compile(r"[?&][Bb][Ii][Dd]=\d+|bid|rfp|rfq|proposal|procurement|solicitation", re.IGNORECASE)

# Exclude clearly recreational/social content from government news feed
SOCIAL_RE = re.compile(
    r"\b(party|bash|splash|festival|concert|5k|fun\s+run|ski\s+lesson"
    r"|snowboard|basketball|softball|baseball|soccer|football|volleyball"
    r"|tennis|rec\s+nights?|registration\s+details?|restaurant\s+week"
    r"|art\s+contest|trivia|bingo|movie\s+night|coaches\s+cup|boco\s+wild"
    r"|adventure\s+awaits|love\s+is\s+all|fall\s+in\s+love|spring\s+kickoff"
    r"|splash\s+bash|adoption\s+event|free\s+ski|winter\s+rec|spring\s+reg)\b",
    re.IGNORECASE,
)


def scrape_finance(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    docs = []
    for link in parser.links:
        url  = link["url"]
        text = link["text"]
        if PDF_PATTERN.search(url) or BUDGET_PATTERN.search(text):
            docs.append({
                "title": text,
                "url":   url,
                "type":  "budget_document",
                "source": "Finance Department",
            })
    return docs


def scrape_agendas(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        url  = link["url"]
        text = link["text"]
        if AGENDA_PATTERN.search(text) or AGENDA_PATTERN.search(url):
            doc_type = "minutes" if re.search(r"minute", text, re.IGNORECASE) else "agenda"
            items.append({
                "title":  text,
                "url":    url,
                "type":   doc_type,
                "source": "Agenda Center",
            })
    return items


def scrape_home_news(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        text = link["text"]
        url  = link["url"]
        if NEWS_PATTERN.search(text) or NEWS_PATTERN.search(url):
            if SOCIAL_RE.search(text):
                continue
            items.append({
                "title":  text,
                "url":    url,
                "type":   "news_alert",
                "source": "botetourtva.gov",
            })
    return items


def scrape_legal_notices(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        url  = link["url"]
        text = link["text"]
        if NOTICE_PATTERN.search(text) or NOTICE_PATTERN.search(url) or PDF_PATTERN.search(url):
            if len(text) > 4:  # skip bare navigation links
                items.append({
                    "title":  text,
                    "url":    url,
                    "type":   "public_notice",
                    "source": "Legal & Public Notices",
                })
    return items


def scrape_bids(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        url  = link["url"]
        text = link["text"]
        if re.search(r"[?&][Bb][Ii][Dd]=\d+", url) or BID_PATTERN.search(text):
            if len(text) > 4:
                items.append({
                    "title":  text,
                    "url":    url,
                    "type":   "bid",
                    "source": "Procurement / Bids",
                })
    return items


def scrape_financial_reports(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        url  = link["url"]
        text = link["text"]
        if PDF_PATTERN.search(url) or BUDGET_PATTERN.search(text):
            if len(text) > 4:
                items.append({
                    "title":  text,
                    "url":    url,
                    "type":   "budget_document",
                    "source": "Financial Reports",
                })
    return items


def scrape_apa(html: str, base: str) -> list[dict]:
    parser = LinkParser(base)
    parser.feed(html)
    items = []
    for link in parser.links:
        text = link["text"]
        url  = link["url"]
        if AUDIT_PATTERN.search(text) or AUDIT_PATTERN.search(url):
            items.append({
                "title":  text,
                "url":    url,
                "type":   "audit_report",
                "source": "Virginia APA",
            })
    return items


# ---------------------------------------------------------------------------
# Government meetings calendar
# ---------------------------------------------------------------------------

# AgendaCenter URLs encode meeting date as _MMDDYYYY-NNN  (e.g. _05042026-677)
AGENDA_DATE_RE = re.compile(r"/_(\d{2})(\d{2})(\d{4})-\d+")

BODY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"board\s+of\s+supervisors",                    re.IGNORECASE), "Board of Supervisors"),
    (re.compile(r"planning\s+commission",                       re.IGNORECASE), "Planning Commission"),
    (re.compile(r"economic\s+development\s+authority|(?<!\w)eda(?!\w)", re.IGNORECASE), "Economic Development Authority"),
    (re.compile(r"parks?\s+(?:and\s+)?rec(?:reation)?\s+commission", re.IGNORECASE), "Parks & Recreation Commission"),
    (re.compile(r"library\s+board|board\s+of\s+trustees",       re.IGNORECASE), "Library Board of Trustees"),
    (re.compile(r"social\s+services\s+board|dss\s+board",       re.IGNORECASE), "Social Services Board"),
    (re.compile(r"electoral\s+board",                           re.IGNORECASE), "Electoral Board"),
    (re.compile(r"board\s+of\s+zoning|zoning\s+appeals",        re.IGNORECASE), "Board of Zoning Appeals"),
    (re.compile(r"historic\s+greenfield",                       re.IGNORECASE), "Historic Greenfield Advisory Council"),
    (re.compile(r"(?<!\w)bccc(?!\w)",                           re.IGNORECASE), "BCCC"),
    (re.compile(r"juneteenth",                                  re.IGNORECASE), "Juneteenth Committee"),
    (re.compile(r"budget\s+sub.?committee",                     re.IGNORECASE), "Budget Subcommittee"),
]

NOISE_TITLE_RE = re.compile(
    r"^(previous versions|skip to|rss|notify me|select a"
    r"|agendas\s*&?\s*minutes|view all|pdf|packet"
    r"|application[s]? for|information package)\b",
    re.IGNORECASE,
)


def extract_meeting_body(title: str) -> str | None:
    for pattern, body_name in BODY_PATTERNS:
        if pattern.search(title):
            return body_name
    return None


def agenda_url_to_date(url: str) -> str | None:
    """Parse ISO date from AgendaCenter URL pattern _MMDDYYYY-NNN."""
    m = AGENDA_DATE_RE.search(url)
    if not m:
        return None
    try:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(yr, mo, dy).isoformat()
    except ValueError:
        return None


class CalendarParser(HTMLParser):
    """Extract meeting events from CivicPlus Calendar.aspx month-grid HTML.

    Each day cell (<td>) contains a day number (1–31) followed by zero or
    more event links of the form Calendar.aspx?EID=NNN.  The parser tracks
    the outermost <td> at a given nesting depth so nested <table>/<td>
    elements inside a day cell don't confuse date attribution.
    """

    def __init__(self, base_url: str, year: int, month: int) -> None:
        super().__init__()
        self._base   = base_url
        self._year   = year
        self._month  = month
        self.events: list[dict] = []

        self._depth                  = 0
        self._cell_depth: int | None = None
        self._day_num:    int | None = None
        self._cell_events: list[dict] = []

        self._in_link   = False
        self._link_url: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self._depth += 1
        d = dict(attrs)

        if tag == "td" and self._cell_depth is None:
            self._cell_depth  = self._depth
            self._day_num     = None
            self._cell_events = []

        elif tag == "a" and not self._in_link:
            href = d.get("href", "")
            if re.search(r"[?&][Ee][Ii][Dd]=\d+", href):
                url = href if href.startswith("http") else urllib.parse.urljoin(self._base, href)
                self._link_url  = url
                self._in_link   = True
                self._link_text = []

    def handle_data(self, data: str) -> None:
        s = data.strip()
        if not s:
            return
        if self._in_link:
            self._link_text.append(s)
        elif self._cell_depth is not None and s.isdigit():
            n = int(s)
            if 1 <= n <= 31 and self._day_num is None:
                self._day_num = n

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = " ".join(self._link_text).strip()
            if title and self._link_url:
                self._cell_events.append({"title": title, "url": self._link_url})
            self._in_link  = False
            self._link_url = None

        if tag == "td" and self._cell_depth == self._depth:
            if self._day_num and self._cell_events:
                try:
                    date_str = date(self._year, self._month, self._day_num).isoformat()
                    for ev in self._cell_events:
                        ev["date"]   = date_str
                        ev["source"] = "botetourtva.gov Calendar"
                        ev["body"]   = extract_meeting_body(ev["title"])
                    self.events.extend(self._cell_events)
                except ValueError:
                    pass
            self._cell_depth  = None
            self._day_num     = None
            self._cell_events = []

        self._depth -= 1


def scrape_calendar(base_url: str, months_ahead: int = 3) -> list[dict]:
    """Fetch upcoming government meetings from Calendar.aspx for multiple months."""
    now_utc = datetime.now(timezone.utc)
    today   = now_utc.date().isoformat()
    events: list[dict] = []
    seen:   set[str]   = set()

    for offset in range(months_ahead):
        total = now_utc.month - 1 + offset
        yr    = now_utc.year + total // 12
        mo    = total % 12 + 1
        url   = f"{base_url}?month={mo}&year={yr}"
        html  = fetch(url)
        if not html:
            continue
        parser = CalendarParser(base_url, yr, mo)
        parser.feed(html)
        added = 0
        for ev in parser.events:
            if ev.get("date", "") >= today and ev["url"] not in seen:
                seen.add(ev["url"])
                events.append(ev)
                added += 1
        print(f"    calendar {mo}/{yr}: {len(parser.events)} total, {added} upcoming")

    events.sort(key=lambda e: e.get("date", ""))
    return events


def events_from_agendas(docs: list[dict]) -> list[dict]:
    """Derive upcoming meeting events from AgendaCenter documents via URL date encoding."""
    today = datetime.now(timezone.utc).date().isoformat()
    events: list[dict] = []
    seen:   set[str]   = set()

    for doc in docs:
        if doc.get("type") != "agenda":
            continue
        title = doc["title"]
        if NOISE_TITLE_RE.match(title):
            continue
        date_str = agenda_url_to_date(doc["url"])
        if not date_str or date_str < today:
            continue
        key = f"{date_str}\x00{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "title":  title,
            "date":   date_str,
            "url":    doc["url"],
            "body":   extract_meeting_body(title),
            "source": "AgendaCenter",
        })

    events.sort(key=lambda e: e.get("date", ""))
    return events


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def detect_changes(
    new_docs: list[dict],
    old_hashes: dict,
    section: str,
) -> tuple[list[dict], dict, list[dict]]:
    """Returns (all_docs_with_status, updated_hashes, new_items)."""
    new_hashes = dict(old_hashes)
    new_items: list[dict] = []

    for doc in new_docs:
        key  = sha256(doc["url"])
        prev = old_hashes.get(key)
        if prev is None:
            doc["_status"] = "new"
            new_items.append(doc)
        else:
            doc["_status"] = "existing"
        new_hashes[key] = sha256(doc["url"] + doc["title"])

    return new_docs, new_hashes, new_items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"[{now_iso()}] Starting Botetourt tracker scrape…")

    old_hashes: dict = load_json(HASHES_FILE)  # type: ignore[assignment]
    changelog: list  = load_json(CHANGELOG_FILE)  # type: ignore[assignment]
    if not isinstance(changelog, list):
        changelog = []

    all_documents: list[dict] = []
    all_new_items: list[dict] = []
    current_hashes: dict      = dict(old_hashes)

    sections = [
        ("finance",           URLS["finance"],           scrape_finance),
        ("agendas",           URLS["agendas"],           scrape_agendas),
        ("home",              URLS["home"],              scrape_home_news),
        ("apa",               URLS["apa_search"],        scrape_apa),
        ("legal_notices",     URLS["legal_notices"],     scrape_legal_notices),
        ("bids",              URLS["bids"],              scrape_bids),
        ("financial_reports", URLS["financial_reports"], scrape_financial_reports),
    ]

    for section, url, scrape_fn in sections:
        print(f"  Fetching {section}: {url}")
        html = fetch(url)
        if not html:
            print(f"  ⚠ Skipping {section} (no response)")
            continue

        raw_docs = scrape_fn(html, url)
        dedupe_urls = set()
        docs: list[dict] = []
        for d in raw_docs:
            if d["url"] not in dedupe_urls:
                dedupe_urls.add(d["url"])
                docs.append(d)

        docs, current_hashes, new_items = detect_changes(
            docs, {k: v for k, v in current_hashes.items()}, section
        )
        all_documents.extend(docs)
        all_new_items.extend(new_items)
        print(f"    Found {len(docs)} items ({len(new_items)} new)")

    # Build government-meetings calendar
    print(f"  Fetching calendar: {URLS['calendar']}")
    cal_events = scrape_calendar(URLS["calendar"], months_ahead=3)

    # Supplement with future meetings extracted from AgendaCenter URL dates
    agenda_ev = events_from_agendas(all_documents)
    cal_keys  = {(e["date"], (e.get("body") or "").lower()) for e in cal_events}
    for ev in agenda_ev:
        key = (ev["date"], (ev.get("body") or "").lower())
        if key not in cal_keys:
            cal_events.append(ev)
            cal_keys.add(key)
    cal_events.sort(key=lambda e: e.get("date", ""))
    print(f"  ✓ {len(cal_events)} upcoming government meeting(s) in calendar")

    # Build changelog entry
    if all_new_items:
        entry = {
            "timestamp": now_iso(),
            "new_count": len(all_new_items),
            "items": [
                {"title": d["title"], "url": d["url"], "type": d["type"]}
                for d in all_new_items
            ],
        }
        changelog.insert(0, entry)
        changelog = changelog[:100]  # keep last 100 runs
        print(f"  ✓ {len(all_new_items)} new item(s) added to changelog")
    else:
        print("  No new items detected")

    # Strip internal _status field before saving
    for doc in all_documents:
        doc.pop("_status", None)

    output = {
        "generated_at": now_iso(),
        "officials":    OFFICIALS,
        "budget":       BUDGET,
        "documents":    all_documents,
        "events":       cal_events,
        "sources": {
            "finance":           URLS["finance"],
            "agendas":           URLS["agendas"],
            "home":              URLS["home"],
            "apa":               URLS["apa"],
            "calendar":          URLS["calendar"],
            "legal_notices":     URLS["legal_notices"],
            "financial_reports": URLS["financial_reports"],
            "bids":              URLS["bids"],
        },
    }

    save_json(OUTPUT_FILE,    output)
    save_json(CHANGELOG_FILE, changelog)
    save_json(HASHES_FILE,    current_hashes)

    print(f"  Saved {OUTPUT_FILE} ({len(all_documents)} documents)")
    print(f"  Saved {CHANGELOG_FILE} ({len(changelog)} changelog entries)")
    print(f"[{now_iso()}] Done.")


if __name__ == "__main__":
    main()
