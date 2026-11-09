import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_news as news

NOW = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


def candidate(path, title="Claude launches a new model", source="Feed", **overrides):
    return {
        "title": title,
        "summary": "The AI model is now available.",
        "url": f"https://example.org/{path}",
        "published": NOW.isoformat(),
        "category": "製品",
        "source": source,
        "focused": False,
        **overrides,
    }


class NewsTests(unittest.TestCase):
    def test_rss_filters_by_exact_publication_time(self):
        rss = b"""
        <rss><channel>
          <item><title>Exactly seven days</title><link>https://example.org/boundary</link><pubDate>Tue, 03 Nov 2026 00:00:00 GMT</pubDate></item>
          <item><title>Too old</title><link>https://example.org/old</link><pubDate>Mon, 02 Nov 2026 23:59:59 GMT</pubDate><updated>2026-11-10T00:00:00Z</updated></item>
          <item><title>Future</title><link>https://example.org/future</link><pubDate>Tue, 10 Nov 2026 00:00:01 GMT</pubDate></item>
          <item><title>Missing date</title><link>https://example.org/missing</link></item>
          <item><title>Bad date</title><link>https://example.org/bad</link><pubDate>2026-11-99</pubDate></item>
        </channel></rss>
        """
        items = news.read_entries(rss, "Feed", "業界", False, NOW)
        self.assertEqual([item["url"] for item in items], ["https://example.org/boundary"])

    def test_atom_uses_published_and_alternate_link(self):
        atom = b"""
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Updated only</title><updated>2026-11-09T00:00:00Z</updated><link href="https://example.org/updated"/></entry>
          <entry><title>New model</title><published>2026-11-10T09:00:00+09:00</published>
            <link rel="self" href="https://example.org/entry"/>
            <link rel="alternate" href="https://example.org/model/?utm_source=rss&amp;id=42#news"/>
            <summary>&lt;p&gt;What changed.&lt;/p&gt;&lt;script&gt;discard()&lt;/script&gt;</summary>
          </entry>
        </feed>
        """
        items = news.read_entries(atom, "Feed", "製品", True, NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published"], NOW.isoformat())
        self.assertEqual(items[0]["url"], "https://example.org/model?id=42")
        self.assertEqual(items[0]["summary"], "What changed.")

    def test_ranking_rejects_unrelated_substrings_newsletters_and_duplicates(self):
        good = candidate("model")
        related = candidate("same-story", "A new model launches from Claude", source="Other")
        unrelated = candidate("unrelated", "Railway company launches a new service", summary="The train is available.")
        roundup = candidate("roundup", "The Download: Claude launches an AI model")
        old = candidate("old", published=(NOW - timedelta(days=8)).isoformat())
        selected = news.select_items([good, related, unrelated, roundup, old], {}, NOW)
        self.assertEqual(len(selected), 1)
        self.assertIn(selected[0]["url"], {good["url"], related["url"]})

    def test_ranking_limits_one_source_without_filling_with_noise(self):
        items = [
            candidate(str(i), title, source="Big Feed") for i, title in enumerate((
                "Claude releases a new model for video",
                "Gemini assistant launches audio editing",
                "AI robotics tools are available today",
                "DeepSeek introduces a coding assistant",
                "Copilot adds a search feature",
                "Qwen releases a translation model",
            ))
        ]
        selected = news.select_items(items, {}, NOW)
        self.assertEqual(Counter(item["source"] for item in selected)["Big Feed"], news.MAX_PER_SOURCE)

    def test_localization_reuses_matching_translation_and_keeps_original_on_failure(self):
        item = candidate("model", score=8)
        old = {"title_original": item["title"], "title": "Claudeの新モデル",
               "summary_original": item["summary"], "summary": "新モデルの提供が始まりました。"}
        with patch.object(news, "translate", return_value="") as translate:
            self.assertEqual(news.localize(item, {}, old)["localization"], "translated")
            translate.assert_not_called()
            result = news.localize(item, {}, {})
        self.assertEqual(result["localization"], "original")
        self.assertEqual(result["title"], item["title"])
        self.assertEqual(result["summary_original"], item["summary"])

    def test_publication_cache_uses_only_valid_current_schema_and_newer_snapshot(self):
        seed = {"schema_version": 2, "updated_at": (NOW - timedelta(hours=1)).isoformat(), "items": []}
        published = {"schema_version": 2, "updated_at": NOW.isoformat(), "items": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "previous.json"
            self.assertEqual(news.previous_feed(path, seed), seed)
            path.write_text(json.dumps(published))
            self.assertEqual(news.previous_feed(path, seed), published)
            path.write_text(json.dumps({**published, "updated_at": (NOW - timedelta(hours=2)).isoformat()}))
            self.assertEqual(news.previous_feed(path, seed), seed)
            path.write_text(json.dumps({**published, "items": [{"url": "not a complete article"}]}))
            self.assertEqual(news.previous_feed(path, seed), seed)
            path.write_text("invalid JSON")
            self.assertEqual(news.previous_feed(path, seed), seed)

    def test_outage_keeps_only_recent_verified_entries(self):
        source = news.FEEDS[0][0]
        now = datetime.now(timezone.utc)
        entries = [
            candidate("recent", source=source, published=(now - timedelta(days=2)).isoformat()),
            candidate("expired", source=source, published=(now - timedelta(days=8)).isoformat()),
        ]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "news.json"
            output.write_text(json.dumps({"schema_version": 2, "items": [
                {**item, "title_original": item["title"], "summary_original": item["summary"],
                 "title": "Claudeの新モデル", "summary": "提供開始"}
                for item in entries
            ]}))
            with (
                patch.object(news, "OUTPUT", output),
                patch.object(news, "fetch_feed", side_effect=TimeoutError),
            ):
                news.main()
            data = json.loads(output.read_text())
        self.assertEqual(data["feed_failures"], [feed[0] for feed in news.FEEDS])
        self.assertEqual([item["url"] for item in data["items"]], [entries[0]["url"]])


if __name__ == "__main__":
    unittest.main()
