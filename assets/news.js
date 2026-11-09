"use strict";

const HOUR = 60 * 60 * 1000;
const WINDOW = 168 * HOUR;
const LOAD_TIMEOUT = 8000;
const STORAGE_KEY = "ai-news-core-saved-v1";
const DATE = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
});
const CATEGORY_LABELS = {
  製品: "製品・ツール", 研究: "研究・技術", オープンソース: "オープンソース", 業界: "業界・安全性",
};
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const searchText = (value) => String(value ?? "").normalize("NFKC").toLowerCase();

function parseTimestamp(value) {
  const match = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return NaN;
  const [, year, month, day] = match;
  if (+day < 1 || +day > new Date(Date.UTC(+year, +month, 0)).getUTCDate()) return NaN;
  return Date.parse(value);
}

function articleURL(value) {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value);
    return (url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password ? url.href : "";
  } catch {
    return "";
  }
}

function normalizeFeed(data, now = Date.now()) {
  const updated = parseTimestamp(data?.updated_at);
  if (!Array.isArray(data?.items) || !Number.isFinite(updated)) {
    throw new Error("Invalid news feed");
  }
  const items = [];
  const seen = new Set();
  for (const entry of data.items) {
    const url = articleURL(entry?.url);
    const timestamp = parseTimestamp(entry?.published);
    if (!url || typeof entry.title !== "string" || !entry.title.trim() || seen.has(url)) continue;
    if (!(timestamp >= now - WINDOW && timestamp <= now)) continue;
    seen.add(url);
    items.push({ ...entry, url, timestamp });
  }
  return {
    items,
    updated,
    failed: Array.isArray(data.feed_failures) ? data.feed_failures.filter((name) => typeof name === "string") : [],
  };
}

function readSaved() {
  try {
    const items = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return new Set(Array.isArray(items) ? items.filter((url) => typeof url === "string") : []);
  } catch {
    return new Set();
  }
}

const state = {
  feed: null,
  saved: readSaved(),
  category: "",
  savedOnly: false,
  loading: false,
  loadError: "",
  expiry: null,
};

function buttonLabel(saved) {
  return saved ? "保存済み" : "あとで読む";
}

function articleCard(article, featured) {
  const e = escapeHTML;
  const href = e(article.url);
  const saved = state.saved.has(article.url);
  const originalTitle = article.title_original !== article.title ? article.title_original : "";
  const showOriginalSummary = !article.summary && article.summary_original;
  const languageBadge = ({
    editorial: "編集済み", translated: "RSSの日本語訳", partial: "見出しを日本語訳", original: "原文",
  })[article.localization];

  return `<article class="article${featured ? " featured" : ""}">
    ${featured ? '<p class="featured-label">✳ 今週の注目</p>' : ""}
    <div class="article-meta">
      <span class="source">${e(article.source || "配信元")}</span>
      <time datetime="${e(article.published)}">${DATE.format(article.timestamp)}</time>
      <span class="tag">${e(CATEGORY_LABELS[article.category] || "AIニュース")}</span>
      ${languageBadge ? `<span>${languageBadge}</span>` : ""}
    </div>
    <h2><a href="${href}" target="_blank" rel="noopener noreferrer">${e(article.title)}</a></h2>
    <p class="summary">${e(article.summary || "詳しい内容は配信元の記事で読めます。")}</p>
    ${article.why ? `<p class="reading-note"><strong>注目ポイント</strong>${e(article.why)}</p>` : ""}
    ${originalTitle || showOriginalSummary ? `<details><summary>配信元の原文</summary>
      ${originalTitle ? `<p>${e(originalTitle)}</p>` : ""}
      ${showOriginalSummary ? `<p>${e(article.summary_original)}</p>` : ""}
    </details>` : ""}
    <div class="article-actions">
      <a href="${href}" target="_blank" rel="noopener noreferrer">配信元で読む ↗</a>
      <button type="button" class="save-button" data-url="${href}" aria-pressed="${saved}">${buttonLabel(saved)}</button>
    </div>
  </article>`;
}

function message(title, description, action, label) {
  return `<div class="message"><h2>${escapeHTML(title)}</h2><p>${escapeHTML(description)}</p>
    ${action ? `<button type="button" data-action="${action}">${label}</button>` : ""}</div>`;
}

function render() {
  const news = $("news");
  const count = $("count");
  const now = Date.now();
  clearTimeout(state.expiry);
  $("refresh").disabled = state.loading;
  $("refresh").textContent = state.loading ? "取得中…" : "更新を確認";
  news.setAttribute("aria-busy", String(state.loading && !state.feed));

  if (!state.feed) {
    count.textContent = state.loading ? "ニュースを取得中…" : "取得できませんでした";
    news.innerHTML = state.loading
      ? message("ニュースを読み込んでいます", "配信データを確認しています。")
      : message("ニュースを読み込めませんでした", state.loadError, "refresh", "もう一度試す");
    return;
  }

  const query = searchText($("search").value).trim().split(/\s+/).filter(Boolean);
  const period = Number($("period").value) * HOUR;
  const source = $("source").value;
  const latest = $("sort").value === "latest";
  const narrowed = query.length || state.category || source || state.savedOnly || period !== WINDOW;
  const recent = state.feed.items.filter((item) => item.timestamp >= now - WINDOW && item.timestamp <= now);
  const matching = recent.filter((item) => {
    if (item.timestamp < now - period || (source && source !== item.source)) return false;
    if (state.category && state.category !== item.category) return false;
    if (state.savedOnly && !state.saved.has(item.url)) return false;
    const haystack = searchText([item.title, item.summary, item.why, item.title_original, item.summary_original,
      item.source, item.category, CATEGORY_LABELS[item.category]].join(" "));
    return query.every((word) => haystack.includes(word));
  });
  if (latest) matching.sort((left, right) => right.timestamp - left.timestamp);

  const nextExpiry = Math.min(
    ...recent.map((item) => item.timestamp + WINDOW),
    ...matching.map((item) => item.timestamp + period),
  );
  if (Number.isFinite(nextExpiry)) state.expiry = setTimeout(render, Math.max(1, nextExpiry - now + 1));

  count.textContent = `表示 ${matching.length} 件 / 7日以内 ${recent.length} 件`;
  $("updated").textContent = `最終確認 ${DATE.format(state.feed.updated)} JST`;
  const warnings = [];
  if (state.loadError) warnings.push(state.loadError + " 直近に取得できた記事を表示しています。");
  if (state.feed.failed.length) warnings.push(`取得できなかった配信元（${state.feed.failed.join("、")}）。`);
  if (now - state.feed.updated > 12 * HOUR) warnings.push("データ更新から12時間以上経過しています。");
  if (recent.some((item) => ["original", "partial"].includes(item.localization))) {
    warnings.push("一部の記事では配信元の原文を表示しています。");
  }
  $("notice").hidden = !warnings.length;
  $("notice").textContent = warnings.join(" ");

  if (!recent.length) {
    news.innerHTML = message("7日以内のニュースがありません", "データの更新後にもう一度確認してください。",
      state.loading ? "" : "refresh", "更新を確認");
  } else if (!matching.length) {
    const description = state.savedOnly
      ? "直近7日以内の保存済み記事が、この条件では見つかりませんでした。"
      : "検索語を短くするか、期間やカテゴリを変えてみてください。";
    news.innerHTML = message("該当する記事がありません", description, "reset", "絞り込みを解除");
  } else {
    const featured = !narrowed && !latest ? matching.shift() : null;
    news.innerHTML = `${featured ? articleCard(featured, true) : ""}
      ${matching.length ? `<div class="grid">${matching.map((item) => articleCard(item, false)).join("")}</div>` : ""}`;
  }
}

async function load() {
  if (state.loading) return;
  state.loading = true;
  state.loadError = "";
  render();
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), LOAD_TIMEOUT);
  try {
    const response = await fetch("./data/news.json", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.feed = normalizeFeed(await response.json());
    const source = $("source");
    const selected = source.value;
    const names = [...new Set(state.feed.items.map((item) => item.source).filter((name) => typeof name === "string" && name))].sort();
    source.replaceChildren(new Option("すべて", ""), ...names.map((name) => new Option(name, name)));
    if (names.includes(selected)) source.value = selected;
  } catch {
    state.loadError = controller.signal.aborted
      ? "ニュースデータの応答に時間がかかっています。"
      : "ニュースデータを取得できませんでした。";
  } finally {
    clearTimeout(deadline);
    state.loading = false;
    render();
  }
}

function setCategory(category) {
  state.category = category;
  $("categories").querySelectorAll("button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.category === category));
  });
}

function resetFilters() {
  $("search").value = "";
  $("period").value = "168";
  $("source").value = "";
  $("sort").value = "recommended";
  state.savedOnly = false;
  $("saved").setAttribute("aria-pressed", "false");
  setCategory("");
  render();
}

$("search").addEventListener("input", render);
for (const id of ["period", "source", "sort"]) $(id).addEventListener("change", render);
$("categories").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-category]");
  if (!button) return;
  setCategory(button.dataset.category);
  render();
});
$("saved").addEventListener("click", (event) => {
  state.savedOnly = !state.savedOnly;
  event.currentTarget.setAttribute("aria-pressed", String(state.savedOnly));
  render();
});
$("news").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-url]");
  if (!button) return;
  const url = button.dataset.url;
  const saved = !state.saved.has(url);
  if (saved) state.saved.add(url);
  else state.saved.delete(url);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...state.saved]));
  } catch {
    // Saving remains available in this tab if browser storage is unavailable.
  }
  if (state.savedOnly && !saved) {
    render();
    $("saved").focus();
  } else {
    button.textContent = buttonLabel(saved);
    button.setAttribute("aria-pressed", String(saved));
  }
});
document.addEventListener("click", (event) => {
  const action = event.target.closest("button[data-action]")?.dataset.action;
  if (action === "refresh") load();
  if (action === "reset") {
    resetFilters();
    $("search").focus();
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.feed) render();
});
load();
