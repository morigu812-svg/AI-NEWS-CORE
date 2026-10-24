#!/usr/bin/env python3
"""Collect, rank, translate, and publish a curated set of public AI-news RSS items."""
import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse
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
MAX_PER_FEED = 18
MAX_FETCHED = 90
MAX_ITEMS = 28
MIN_SCORE = 3
TIMEOUT = 20
TRANSLATE_TIMEOUT = 8
TRANSLATE_DELAY = 0.12

AI_TERMS = (
    "ai", "artificial intelligence", "llm", "large language model", "chatgpt",
    "openai", "anthropic", "claude", "gemini", "deepmind", "copilot", "gpt",
    "neural network", "machine learning", "generative", "robot", "agent",
    "inference", "training", "reasoning", "benchmark", "hugging face",
    "open source model", "ai model", "language model", "diffusion", "multimodal",
    "人工知能", "生成ai", "生成ＡＩ", "大規模言語モデル", "機械学習", "ロボット",
)
HIGH_VALUE_TERMS = (
    "launch", "released", "release", "announces", "announced", "introducing",
    "breakthrough", "new model", "open source", "open-source", "benchmark",
    "research", "paper", "safety", "security", "reasoning", "coding", "agent",
    "available", "重大", "発表", "公開", "新モデル", "研究", "論文", "無料",
    "オープンソース", "安全性", "性能", "推論", "開発",
)
LOW_VALUE_TERMS = (
    "podcast", "vergecast", "review score", "week in review", "opinion",
    "iphone duo", "smartphone review", "best phones", "buying guide",
    "ポッドキャスト", "レビュー評価", "購入ガイド",
)
CATEGORY_TERMS = {
    "研究": ("research", "paper", "study", "benchmark", "science", "model weights", "研究", "論文", "実験", "性能評価"),
    "製品": ("launch", "release", "introducing", "available", "announces", "app", "tool", "product", "発表", "公開", "リリース", "新機能", "サービス"),
    "オープンソース": ("open source", "open-source", "github", "hugging face", "weights", "opensource", "オープンソース", "公開モデル"),
    "業界": ("funding", "acquisition", "lawsuit", "policy", "security", "safety", "company", "企業", "資金調達", "規制", "セキュリティ", "安全性"),
}

def translate_to_japanese(value):
    """Use a free keyless translation endpoint, falling back to the original text."""
    value = value or ""
    if not value.strip():
        return value
    japanese_chars = len(re.findall(r"[ぁ-んァ-ン一-龯]", value))
    visible_chars = max(1, len(re.sub(r"\s", "", value)))
    if japanese_chars / visible_chars >= 0.25:
        return value
    endpoint = "https://translate.googleapis.com/translate_a/single"
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "auto", "tl": "ja", "dt": "t", "q": value[:1800]
    })
    request = urllib.request.Request(
        endpoint + "?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AI-NEWS-CORE/1.0)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TRANSLATE_TIMEOUT) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8"))
        translated = "".join(
            part[0] for part in (data[0] or [])
            if isinstance(part, list) and part and isinstance(part[0], str)
        ).strip()
        return translated or value
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, IndexError) as exc:
        print("[WARN] Translation skipped: " + type(exc).__name__)
        return value

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

def score_item(item):
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or "").lower()
    combined = title + " " + summary
    score = 0
    ai_title_hits = sum(1 for term in AI_TERMS if term in title) + int(bool(re.search(r"\bai\b", title)))
    ai_all_hits = sum(1 for term in AI_TERMS if term in combined) + int(bool(re.search(r"\bai\b", combined)))
    score += min(ai_title_hits, 3) * 2.2
    score += min(max(0, ai_all_hits - ai_title_hits), 3) * 0.7
    score += min(sum(1 for term in HIGH_VALUE_TERMS if term in title), 3) * 1.15
    score -= min(sum(1 for term in LOW_VALUE_TERMS if term in title), 3) * 2.4
    if any(term in title for term in ("iphone", "smartphone", "election", "celebrity")) and not any(
        term in title for term in ("ai", "artificial intelligence", "chatgpt", "openai", "robot", "gemini", "claude")
    ):
        score -= 4
    source = item.get("source", "")
    if source in ("OpenAI", "Google AI Blog", "Hugging Face"):
        score += 0.7
    published = item.get("published", "")
    if published:
        try:
            age_hours = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(published.replace("Z", "+00:00"))).total_seconds() / 3600)
            score += max(0, 2 - age_hours / 72)
        except (ValueError, TypeError):
            pass
    return round(score, 2)

def categorize(item, default_category):
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or "").lower()
    combined = title + " " + summary
    for category in ("オープンソース", "研究", "製品", "業界"):
        if any(term in combined for term in CATEGORY_TERMS[category]):
            return category
    return default_category

def parse_feed(source, url, default_category):
    request = urllib.request.Request(url, headers={
        "User-Agent": "AI-NEWS-CORE/1.0 (+https://github.com/morigu812-svg/AI-NEWS-CORE)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(5_000_000)
    root = ET.fromstring(raw)
    atom_ns = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(".//item") or root.findall(".//" + atom_ns + "entry")
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
        result.append({
            "title": clean_html(title),
            "url": link,
            "source": source,
            "published": parse_date(date_raw),
            "summary": summary[:700],
            "category": default_category,
        })
    return result

def curate_items(items):
    unique = {}
    for item in items:
        key = item["url"].split("#", 1)[0].rstrip("/")
        if key not in unique or len(item.get("summary", "")) > len(unique[key].get("summary", "")):
            unique[key] = item
    ranked = list(unique.values())
    for item in ranked:
        item["score"] = score_item(item)
        item["category"] = categorize(item, item.get("category", "業界"))
    ranked.sort(key=lambda item: (item["score"], item.get("published", "")), reverse=True)
    selected = [item for item in ranked if item["score"] >= MIN_SCORE][:MAX_ITEMS]
    # Keep a modest selection on quieter news days without filling the page with irrelevant posts.
    if len(selected) < 12:
        selected = [item for item in ranked if item["score"] >= 1.5][:min(18, MAX_ITEMS)]
    return selected

def translate_items(items):
    for index, item in enumerate(items, start=1):
        original_title = item.get("title", "")
        original_summary = item.get("summary", "")
        item["title_original"] = original_title
        item["summary_original"] = original_summary
        item["title"] = translate_to_japanese(original_title)
        time.sleep(TRANSLATE_DELAY)
        if original_summary:
            item["summary"] = translate_to_japanese(original_summary[:420])
            time.sleep(TRANSLATE_DELAY)
        else:
            item["summary"] = "概要は配信元の記事で確認できます。"
        item["summary"] = item["summary"][:360].strip()
        item["score"] = item.get("score", 0)
        print("[TRANSLATE] {}/{}: {}".format(index, len(items), item.get("source", "news")))
    return items

def main():
    items = []
    failures = []
    for source, url, category in FEEDS:
        try:
            found = parse_feed(source, url, category)
            items.extend(found)
            print("[OK] {}: {} articles".format(source, len(found)))
        except Exception as exc:
            failures.append(source)
            print("[WARN] {}: {}: {}".format(source, type(exc).__name__, exc))
        time.sleep(0.25)

    if not items and OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if previous.get("items"):
                print("All feeds failed; keeping previous news data.")
                return
        except (OSError, json.JSONDecodeError):
            pass

    items = curate_items(items[:MAX_FETCHED])
    items = translate_items(items)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "total_collected": MAX_FETCHED,
        "published_count": len(items),
        "feed_failures": failures,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Wrote {} curated articles; feed failures: {}".format(len(items), len(failures)))

if __name__ == "__main__":
    main()
