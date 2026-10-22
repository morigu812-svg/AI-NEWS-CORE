#!/usr/bin/env python3
"""Collect public AI-news RSS feeds and write data/news.json using only Python stdlib."""
import json
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "news.json"
FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml", "製品"),
    ("Google AI Blog", "https://blog.research.google/feeds/posts/default?alt=rss", "研究"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "オープンソース"),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", "業界"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "業界"),
]
MAX_PER_FEED = 15
MAX_ITEMS = 80
TIMEOUT = 20

def clean_html(value):
    value = unescape(value or "")
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def text_of(element, names):
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""

def parse_date(value):
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return ""

def parse_feed(source, url, default_category):
    request = urllib.request.Request(url, headers={
        "User-Agent": "AI-NEWS-CORE/1.0 (+https://github.com/morigu812-svg/AI-NEWS-CORE)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(5_000_000)
    root = ET.fromstring(raw)
    entries = root.findall(".//item")
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if not entries:
        entries = root.findall(".//" + atom_ns + "entry")
    result = []
    for item in entries[:MAX_PER_FEED]:
        title = text_of(item, ["title", atom_ns + "title"])
        link = text_of(item, ["link"])
        if not link:
            for link_node in item.findall(atom_ns + "link"):
                if link_node.get("href"):
                    link = link_node.get("href")
                    break
        summary = text_of(item, ["description", "summary", atom_ns + "summary", atom_ns + "content"])
        summary = clean_html(summary)
        if not title or not link or not link.startswith(("https://", "http://")):
            continue
        date_raw = text_of(item, ["pubDate", "published", "updated", "dc:date", atom_ns + "published", atom_ns + "updated"])
        category = default_category
        title_lower = title.lower()
        if any(word in title_lower for word in ["research", "paper", "model", "benchmark", "研究", "論文"]):
            category = "研究"
        elif any(word in title_lower for word in ["open source", "github", "hugging face", "オープンソース"]):
            category = "オープンソース"
        elif any(word in title_lower for word in ["launch", "release", "introducing", "available", "発表", "公開", "リリース"]):
            category = "製品"
        result.append({
            "title": clean_html(title),
            "url": link,
            "source": source,
            "published": parse_date(date_raw),
            "summary": summary[:500],
            "category": category,
        })
    return result

def main():
    items = []
    failures = []
    for source, url, category in FEEDS:
        try:
            found = parse_feed(source, url, category)
            items.extend(found)
            print(f"[OK] {source}: {len(found)} articles")
        except Exception as exc:
            failures.append(source)
            print(f"[WARN] {source}: {type(exc).__name__}: {exc}")
        time.sleep(0.25)

    # Do not erase previously collected news if every feed fails.
    if not items and OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if previous.get("items"):
                print("All feeds failed; keeping previous news data.")
                return
        except (OSError, json.JSONDecodeError):
            pass

    unique = {}
    for item in items:
        key = item["url"].split("#", 1)[0]
        unique.setdefault(key, item)
    items = list(unique.values())
    items.sort(key=lambda item: item.get("published") or "", reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items[:MAX_ITEMS],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(payload['items'])} unique articles; feed failures: {len(failures)}")

if __name__ == "__main__":
    main()
