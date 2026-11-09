#!/usr/bin/env python3
"""Build the static AI NEWS CORE feed from public RSS/Atom sources."""

import argparse
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "news.json"
EDITORIAL = ROOT / "scripts" / "editorial.json"
FEEDS = (
    ("OpenAI", "https://openai.com/news/rss.xml", "製品", True),
    ("Google AI", "https://blog.google/innovation-and-ai/technology/ai/rss/", "製品", True),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "オープンソース", True),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/", "業界", False),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "業界", False),
)
WINDOW = timedelta(days=7)
MAX_ITEMS = 14
MAX_PER_SOURCE = 4
MAX_PER_CATEGORY = 5
ATOM = "{http://www.w3.org/2005/Atom}"
ISSUED = "{http://purl.org/dc/terms/}issued"
AI_TERMS = (
    "ai", "artificial intelligence", "llm", "language model", "machine learning",
    "neural network", "chatgpt", "openai", "gpt", "anthropic", "claude", "gemini",
    "deepmind", "copilot", "hugging face", "qwen", "deepseek", "agentic",
    "ai agent", "robot", "robotics", "humanoid", "generative", "text-to-speech", "tts",
    "生成ai", "人工知能", "言語モデル", "ロボット",
)
SIGNALS = {
    "オープンソース": (
        "open source", "open-source", "open weight", "open-weight", "model weights",
        "github", "dataset", "apache 2.0", "公開モデル", "データセット",
    ),
    "研究": (
        "benchmark", "study", "researchers", "paper", "preprint", "evaluation",
        "tested", "experiment", "benchmarking", "benchmarks", "論文", "ベンチマーク", "実験",
    ),
    "製品": (
        "launch", "launches", "release", "releases", "introducing", "introduces",
        "available", "rollout", "new model", "update", "adds", "feature",
        "assistant", "model", "launching", "building", "tools", "リリース", "新機能",
    ),
    "業界": (
        "regulation", "policy", "lawsuit", "funding", "acquire", "acquisition",
        "ipo", "valued", "security", "safety", "vulnerability", "data center",
        "data centers", "data centre", "copyright", "partnership", "regulate", "規制", "安全性",
    ),
}
LOW_VALUE = (
    "the download", "vergecast", "podcast", "newsletter", "roundup", "deals",
    "buying guide", "sponsored", "partner content", "week in review",
    "chief corporate affairs officer", "case study", "customer story", "sex life", "innovating with ai in",
)
STOP_WORDS = set("a an and are as at by can for from has how in is it new of on or the to with why your".split())
JAPANESE = re.compile(r"[ぁ-んァ-ヶ一-龯]")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?")


class FeedText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.ignored:
            self.ignored -= 1
        elif tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def clean_html(value):
    parser = FeedText()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def text_of(element, *tags):
    for tag in tags:
        node = element.find(tag)
        if node is not None:
            value = "".join(node.itertext()).strip()
            if value:
                return value
    return ""


def parse_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        if ISO_DATE.fullmatch(value):
            date = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        else:
            date = parsedate_to_datetime(value)
        return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def recent(published, now):
    date = parse_date(published)
    return date is not None and now - WINDOW <= date <= now


def canonical_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
            return ""
    except (TypeError, ValueError):
        return ""
    params = [
        (key, val) for key, val in urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in ("gclid", "fbclid", "mc_cid", "mc_eid")
    ]
    return urllib.parse.urlunsplit((
        url.scheme, url.netloc.lower(), url.path.rstrip("/") or "/",
        urllib.parse.urlencode(sorted(params)), "",
    ))


def read_entries(raw, source, default_category, focused, now):
    root = ET.fromstring(raw)
    entries = root.findall(".//item") or root.findall(".//" + ATOM + "entry")
    result = []
    for entry in entries:
        published = next((date for tag in ("pubDate", "published", ATOM + "published", ISSUED)
                          if (date := parse_date(text_of(entry, tag))) is not None), None)
        if published is None or not now - WINDOW <= published <= now:
            continue
        link = text_of(entry, "link")
        if not link:
            link = next((node.get("href") for node in entry.findall(ATOM + "link")
                         if node.get("rel", "alternate") == "alternate" and node.get("href")), "")
        url = canonical_url(link)
        title = clean_html(text_of(entry, "title", ATOM + "title"))
        if not url or not title:
            continue
        summary = clean_html(text_of(entry, "description", "summary", ATOM + "summary", ATOM + "content"))
        result.append({
            "title": title, "summary": summary, "url": url, "source": source,
            "published": published.isoformat(), "category": default_category, "focused": focused,
        })
    return result


def fetch_feed(feed, now):
    source, url, category, focused = feed
    request = urllib.request.Request(url, headers={
        "User-Agent": "AI-NEWS-CORE/2.0 (+https://github.com/morigu812-svg/AI-NEWS-CORE)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("RSS response is too large")
            return read_entries(raw, source, category, focused, now)
        except urllib.error.HTTPError as exc:
            if attempt or exc.code < 500:
                raise
        except (OSError, ET.ParseError):
            if attempt:
                raise


def compile_terms(terms):
    parts = []
    for term in terms:
        part = re.escape(term)
        if term.isascii():
            part = r"(?<![a-z0-9])" + part + r"(?![a-z0-9])"
        parts.append(part)
    return re.compile("|".join(parts))


AI_PATTERN = compile_terms(AI_TERMS)
LOW_PATTERN = compile_terms(LOW_VALUE)
CATEGORY_PATTERNS = {name: compile_terms(terms) for name, terms in SIGNALS.items()}
BUSINESS_PROMOTION = re.compile(r"^(?:how .{0,55} (?:uses|uses? ai|builds?|delivers?)\b|"
                                r".{0,65}\b(?:uses|builds|delivers)\b.{0,60}\b(?:openai|chatgpt|gpt)\b)")
GOOGLE_PRODUCT = re.compile(r"\b(?:gemini|veo|imagen|lyria)\b")


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().replace("’", "'").replace("‑", "-")


def score_item(item, now):
    title = normalized(item["title"])
    summary = normalized(item["summary"][:1400])
    if LOW_PATTERN.search(title) or (item["source"] == "OpenAI" and BUSINESS_PROMOTION.search(title)):
        return None
    title_ai = set(AI_PATTERN.findall(title))
    summary_ai = set(AI_PATTERN.findall(summary))
    if not (item["focused"] or title_ai or summary_ai):
        return None
    signals = {
        name: (len(set(pattern.findall(title))), len(set(pattern.findall(summary))))
        for name, pattern in CATEGORY_PATTERNS.items()
    }
    title_signals = sum(count for count, _ in signals.values())
    summary_signals = sum(count for _, count in signals.values())
    google_product = item["source"] == "Google AI" and GOOGLE_PRODUCT.search(title)
    if not (title_signals or summary_signals or google_product):
        return None
    if not item["focused"] and not title_ai and (not title_signals or len(summary_ai) < 2):
        return None

    weights = {name: min(in_title, 2) * 4 + min(in_summary, 3) for name, (in_title, in_summary) in signals.items()}
    category = max(weights, key=weights.get)
    if google_product and not signals["研究"][0]:
        category = "製品"
    elif weights[item["category"]] == weights[category]:
        category = item["category"]
    age = (now - parse_date(item["published"])).total_seconds() / 3600
    score = 2 + min(len(title_ai), 2) * 2 + min(title_signals, 3) * 1.8
    score += min(summary_signals, 4) * 0.55 + min(len(summary_ai - title_ai), 2) * 0.4
    score += max(0, 1.5 - age / 112)
    if not item["focused"] and not title_ai:
        score -= 1.5
    if google_product:
        score += 1.5
    return round(score, 2), category


def title_words(value):
    return set(re.findall(r"[a-z0-9]+|[ぁ-んァ-ヶ一-龯]+", normalized(value))) - STOP_WORDS


def select_items(candidates, editorial, now):
    ranked = []
    for item in candidates:
        if not recent(item["published"], now):
            continue
        result = score_item(item, now)
        note = editorial.get(item["url"], {})
        if result is None and not note:
            continue
        score, category = result or (0, item["category"])
        if note or score >= 5.2:
            ranked.append({**item, "score": score, "category": note.get("category", category)})
    ranked.sort(key=lambda item: (
        editorial.get(item["url"], {}).get("priority", 0), item["url"] in editorial,
        item["score"], item["published"],
    ), reverse=True)

    selected, titles, urls = [], [], set()
    sources, categories = Counter(), Counter()
    for item in ranked:
        source, category = item["source"], item["category"]
        if sources[source] >= MAX_PER_SOURCE or categories[category] >= MAX_PER_CATEGORY:
            continue
        url_key = urllib.parse.urlsplit(item["url"])._replace(scheme="").geturl()
        words = title_words(item["title"])
        if url_key in urls or any(words == other or (
            len(words & other) >= 4 and len(words & other) / len(words | other) >= 0.6
        ) for other in titles):
            continue
        selected.append(item)
        titles.append(words)
        urls.add(url_key)
        sources[source] += 1
        categories[category] += 1
        if len(selected) == MAX_ITEMS:
            break
    return selected


def excerpt(value, limit):
    if len(value) <= limit:
        return value
    boundaries = [match.end() for match in re.finditer(r"[。！？]|[.!?](?=\s)", value[:limit])]
    if boundaries and boundaries[-1] >= limit * 0.45:
        return value[:boundaries[-1]].strip()
    clipped = value[:limit - 1]
    if not JAPANESE.search(value) and " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip() + "…"


def translate(value):
    if not value or re.search(r"[ぁ-んァ-ヶ]", value):
        return value
    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "ja", "dt": "t", "q": value})
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AI-NEWS-CORE/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read(200_000))
        return "".join(part[0] for part in data[0] if isinstance(part[0], str)).strip()
    except (OSError, ValueError, TypeError, IndexError, KeyError) as exc:
        print(f"::warning::Translation unavailable ({type(exc).__name__})", file=sys.stderr)
        return ""


def reading_note(item):
    combined = normalized(item["title"] + " " + item["summary"])
    if re.search(r"\b(?:benchmarks?|evaluation|tested)\b", combined):
        return "どんな課題で測った結果か、比較対象や評価条件もあわせて読むと、数字の意味がわかります。"
    if re.search(r"\b(?:dataset|model weights|open[- ]source|open[- ]weights?)\b", combined):
        return "公開されている範囲とライセンスを確認すると、学習や自分のプロジェクトに使えるか判断できます。"
    if re.search(r"\b(?:reportedly|rumou?rs?|plans to|racing to)\b", combined):
        return "報道で示された計画と、公式に発表・提供された機能を分けて読むと、現在地がつかめます。"
    if re.search(r"\b(?:security|vulnerabilit(?:y|ies)|safety)\b", combined):
        return "どの利用環境が対象か、対策が実際に使える段階かが確認ポイントです。"
    if item["category"] == "製品":
        return "利用できる地域・プラン・提供時期は、配信元の案内で確認できます。"
    return ""


def localize(item, note, previous):
    title, summary = item["title"], excerpt(item["summary"], 500)
    result = {key: item[key] for key in ("url", "source", "published", "category", "score")}
    result.update(title_original=title, summary_original=summary)
    if note:
        result.update(title=note["title"], summary=note["summary"], why=note["why"], localization="editorial")
        return result

    if previous.get("localization") == "editorial":
        previous = {}
    old_title = previous.get("title", "") if title == previous.get("title_original") else ""
    old_summary = previous.get("summary", "") if summary == previous.get("summary_original") else ""
    translated_title = old_title if JAPANESE.search(old_title) else translate(title)
    translated_summary = old_summary if JAPANESE.search(old_summary) else translate(summary)
    has_title = bool(JAPANESE.search(translated_title))
    has_summary = bool(JAPANESE.search(translated_summary))
    result.update(
        title=excerpt(translated_title, 125) if has_title else title,
        summary=excerpt(translated_summary, 260) if has_summary else "",
        why=reading_note(item) if has_title else "",
        localization="translated" if has_title and (has_summary or not summary) else "partial" if has_title else "original",
    )
    return result


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def previous_feed(path, seed):
    if path is None:
        return seed
    try:
        payload = load_json(path, {})
    except (OSError, ValueError):
        return seed
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return seed
    updated = parse_date(payload.get("updated_at"))
    seed_updated = parse_date(seed.get("updated_at"))
    if not updated or (seed_updated and updated < seed_updated):
        return seed

    items = payload.get("items")
    fields = ("url", "title", "summary", "title_original", "summary_original", "source", "published")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and all(isinstance(item.get(key), str) for key in fields)
        for item in items
    ):
        return seed
    return payload


def main(previous_path=None):
    now = datetime.now(timezone.utc)
    editorial = load_json(EDITORIAL, {})
    previous = previous_feed(previous_path, load_json(OUTPUT, {}))
    old_by_url = {canonical_url(item["url"]): item for item in previous.get("items", [])}
    items, failures = [], []
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as workers:
        requests = [(feed, workers.submit(fetch_feed, feed, now)) for feed in FEEDS]
        for (source, _, _, _), job in requests:
            try:
                found = job.result()
                items.extend(found)
                print(f"[OK] {source}: {len(found)} recent articles")
            except (OSError, ValueError, ET.ParseError) as exc:
                failures.append(source)
                print(f"::warning::{source}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if previous.get("schema_version") == 2:
        current_urls = {item["url"] for item in items}
        sources = {name: (category, focused) for name, _, category, focused in FEEDS}
        for url, old in old_by_url.items():
            source = old["source"]
            if url in current_urls or source not in sources or not recent(old["published"], now):
                continue
            category, focused = sources[source]
            items.append({
                "url": url, "title": old["title_original"], "summary": old["summary_original"],
                "source": source, "published": old["published"], "category": category, "focused": focused,
            })

    selected = select_items(items, editorial, now)
    with ThreadPoolExecutor(max_workers=3) as workers:
        translated = list(workers.map(
            lambda item: localize(item, editorial.get(item["url"]), old_by_url.get(item["url"], {})),
            selected,
        ))
    finished = datetime.now(timezone.utc)
    translated = [item for item in translated if recent(item["published"], finished)]
    payload = {
        "schema_version": 2,
        "updated_at": finished.isoformat(),
        "items": translated,
        "total_collected": len(items),
        "published_count": len(translated),
        "feed_failures": failures,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(OUTPUT)
    print(f"Wrote {len(translated)} curated articles ({len(failures)} failed feeds)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, help="optional previously published news JSON")
    main(parser.parse_args().previous)
