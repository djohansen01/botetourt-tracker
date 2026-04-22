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
from datetime import datetime, timezone
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
BUDGET_PATTERN  = re.compile(r"budget|fy\s*20\d\d|fiscal", re.IGNORECASE)
AGENDA_PATTERN  = re.compile(r"agenda|minute|meeting|board", re.IGNORECASE)
AUDIT_PATTERN   = re.compile(r"botetourt", re.IGNORECASE)
NEWS_PATTERN    = re.compile(r"alert|news|notice|announcement|press.?release", re.IGNORECASE)


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
            items.append({
                "title":  text,
                "url":    url,
                "type":   "news_alert",
                "source": "botetourtva.gov",
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
        ("finance", URLS["finance"],    scrape_finance),
        ("agendas", URLS["agendas"],    scrape_agendas),
        ("home",    URLS["home"],       scrape_home_news),
        ("apa",     URLS["apa_search"], scrape_apa),
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
        "sources": {
            "finance":  URLS["finance"],
            "agendas":  URLS["agendas"],
            "home":     URLS["home"],
            "apa":      URLS["apa"],
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
