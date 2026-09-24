# -*- coding: utf-8 -*-
"""考公时政卡片自动生成系统。

运行：
  cp config.example.yaml config.yaml
  export LLM_API_KEY='你的兼容接口 Key'
  python bot.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from openai import OpenAI

try:
    import trafilatura
except ImportError:  # 可选依赖不可用时使用 BeautifulSoup 兜底
    trafilatura = None

from prompt import SYSTEM_PROMPT, build_user_prompt

UTC = timezone.utc
DEFAULT_KEYWORDS = ["会议", "决定", "通知", "讲话", "发布", "规划", "意见", "报告", "政策"]
LOG = logging.getLogger("politics_bot")


def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(output_dir / "bot.log", encoding="utf-8")],
    )


def load_config() -> dict[str, Any]:
    path = Path("config.yaml")
    if not path.exists():
        path = Path("config.example.yaml")
        LOG.warning("未找到 config.yaml，当前使用 config.example.yaml；实际运行建议复制并修改配置。")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if hasattr(value, "tm_year"):
        return datetime(*value[:6], tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace("/", "-")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            pass
    return None


def clean_text(text: str) -> str:
    return re.sub(r"\\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)).strip()


def extract_content(url: str, html: str) -> str:
    """优先使用 trafilatura 提取正文，失败后用页面文本兜底。"""
    if trafilatura:
        try:
            result = trafilatura.extract(html, include_comments=False, include_tables=False)
            if result and len(result.strip()) >= 80:
                return clean_text(result)
        except Exception:
            LOG.debug("trafilatura 提取失败", exc_info=True)
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        node.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return clean_text(main.get_text(" ", strip=True))


def fetch_url(session: requests.Session, url: str, interval: float) -> str:
    last_error = None
    for attempt in range(3):
        try:
            time.sleep(max(0.0, interval))
            response = session.get(url, timeout=15, headers={"User-Agent": "KaogongPoliticsBot/1.0 (public-news-reader)"})
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except (requests.RequestException, UnicodeError) as exc:
            last_error = exc
            if attempt < 2:
                LOG.warning("请求失败，将重试（%d/2）[%s]: %s", attempt + 1, url, exc)
                time.sleep(2 ** attempt)
    raise RuntimeError(f"请求重试后仍失败: {url}: {last_error}")


def fetch_source(source: dict[str, Any], session: requests.Session, interval: float) -> list[dict[str, Any]]:
    """抓取 RSS 或普通列表页；单个源失败不会中断其他源。"""
    url = source.get("url", "")
    name, source_type = source.get("name", url), source.get("type", "web").lower()
    if not url:
        return []
    try:
        if source_type == "rss":
            time.sleep(max(0.0, interval))
            parsed = feedparser.parse(fetch_url(session, url, 0))
            if getattr(parsed, "bozo", False) and not parsed.entries:
                raise RuntimeError("RSS 解析失败")
            rows = []
            for entry in parsed.entries:
                link = entry.get("link", "")
                if link:
                    rows.append({"source": name, "title": clean_text(entry.get("title", "")), "url": link,
                                 "published": entry.get("published_parsed") or entry.get("updated_parsed"),
                                 "content": clean_text(entry.get("summary", ""))})
            return rows
        html = fetch_url(session, url, interval)
        soup = BeautifulSoup(html, "html.parser")
        rows, seen = [], set()
        for a in soup.select("a[href]"):
            title = clean_text(a.get_text(" ", strip=True))
            link = urljoin(url, a.get("href", ""))
            if len(title) < 8 or not link.startswith(("http://", "https://")) or link in seen:
                continue
            seen.add(link)
            rows.append({"source": name, "title": title, "url": link, "published": None, "content": ""})
            if len(rows) >= 30:
                break
        # 列表页的链接通常只是摘要，逐篇取正文；失败时保留标题以便日志追踪。
        for row in rows:
            try:
                article_html = fetch_url(session, row["url"], interval)
                row["content"] = extract_content(row["url"], article_html)
                article_soup = BeautifulSoup(article_html, "html.parser")
                time_node = article_soup.find("time") or article_soup.find(attrs={"class": re.compile("date|time", re.I)})
                row["published"] = time_node.get("datetime") if time_node else (time_node.get_text(strip=True) if time_node else None)
            except Exception as exc:
                LOG.warning("正文抓取失败 %s: %s", row["url"], exc)
        return rows
    except Exception as exc:
        LOG.error("源抓取失败 [%s] %s: %s", name, url, exc)
        return []


class ArticleDB:
    """SQLite 去重库：URL MD5 精确去重 + 标题相似度二次去重。"""
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS articles (url_md5 TEXT PRIMARY KEY, url TEXT, title TEXT, processed_at TEXT)")
        self.conn.commit()

    @staticmethod
    def url_md5(url: str) -> str:
        return hashlib.md5(url.strip().encode("utf-8")).hexdigest()

    def is_duplicate(self, article: dict[str, Any], threshold: float) -> bool:
        digest = self.url_md5(article["url"])
        if self.conn.execute("SELECT 1 FROM articles WHERE url_md5=?", (digest,)).fetchone():
            return True
        titles = self.conn.execute("SELECT title FROM articles ORDER BY processed_at DESC LIMIT 500").fetchall()
        return any(SequenceMatcher(None, article["title"], row[0]).ratio() >= threshold for row in titles)

    def mark(self, article: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR IGNORE INTO articles VALUES (?, ?, ?, ?)",
                          (self.url_md5(article["url"]), article["url"], article["title"], datetime.now(UTC).isoformat()))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def relevant(article: dict[str, Any], cfg: dict[str, Any]) -> bool:
    dt = parse_datetime(article.get("published"))
    hours = float(cfg.get("hours_window", 48))
    if dt and datetime.now(UTC) - dt > timedelta(hours=hours):
        return False
    text = f'{article.get("title", "")} {article.get("content", "")}'
    return any(word in text for word in cfg.get("keywords", DEFAULT_KEYWORDS))


def call_llm(article: dict[str, Any], llm_cfg: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv(llm_cfg.get("api_key_env", "LLM_API_KEY"), "")
    if not api_key:
        LOG.error("未设置 %s，跳过 LLM：%s", llm_cfg.get("api_key_env", "LLM_API_KEY"), article["title"])
        return None
    content = article.get("content", "")[: int(llm_cfg.get("max_chars", 12000))]
    client = OpenAI(api_key=api_key, base_url=llm_cfg.get("base_url"))
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=llm_cfg.get("model", "gpt-4o-mini"), temperature=float(llm_cfg.get("temperature", 0.2)),
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": build_user_prompt(article["title"], article["url"], str(article.get("published", "")), content)}],
            )
            raw = response.choices[0].message.content or ""
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 兼容不支持 response_format 的服务，清理可能出现的代码围栏后再试一次。
                raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I).strip()
                data = json.loads(raw)
            data["source_title"], data["source_url"] = article["title"], article["url"]
            return data
        except Exception as exc:
            LOG.warning("LLM 第 %d 次失败 [%s]: %s", attempt + 1, article["title"], exc)
            if attempt == 1:
                Path("output").mkdir(exist_ok=True)
                (Path("output") / "llm_failures.log").open("a", encoding="utf-8").write(f"{article['title']}\n{exc}\n\n")
    return None


def compact_result(data: dict[str, Any], filt: dict[str, Any]) -> dict[str, Any]:
    """对模型结果做程序级限量，避免模型偶尔输出过多内容。"""
    card_limit = int(filt.get("cards_per_article", 2))
    single_limit = int(filt.get("quiz_single_per_article", 1))
    fill_limit = int(filt.get("quiz_fill_per_article", 1))
    result = dict(data)
    result["summary"] = " ".join(str(data.get("summary", "")).split())[:240]
    result["points"] = [str(x).strip() for x in data.get("points", [])[:2] if str(x).strip()]
    result["cards"] = data.get("cards", [])[:card_limit]
    quiz = data.get("quiz", {}) or {}
    result["quiz"] = {
        "single": (quiz.get("single", []) or [])[:single_limit],
        "multiple": [],
        "fill": (quiz.get("fill", []) or [])[:fill_limit],
    }
    result["confusions"] = data.get("confusions", [])[:2]
    return result


def render_markdown(cards: list[dict[str, Any]]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 考公时政卡片｜{today}", "", f"> 共生成 {len(cards)} 篇材料卡片。"]
    for i, item in enumerate(cards, 1):
        lines += [f"\n## {i}. {item.get('source_title', '未命名材料')}", f"来源：[查看原文]({item.get('source_url', '')})", "", f"**摘要**：{item.get('summary', '')}", "", "### 考点", *[f"- {x}" for x in item.get("points", [])]]
        if item.get("exam_focus") or item.get("theory_topic"):
            lines.append(f"**考纲模块**：{item.get('exam_focus', '政治理论')}｜{item.get('theory_topic', '待核对')}")
        lines += ["\n### 卡片", "| 正面 | 背面 | 标签 | 重要性 | 易错点 |", "|---|---|---|---|---|"]
        for card in item.get("cards", []):
            vals = [card.get(k, "").replace("|", "\\|").replace("\n", " ") for k in ("front", "back", "tag", "importance", "trap")]
            lines.append("| " + " | ".join(vals) + " |")
        quiz = item.get("quiz", {})
        if quiz.get("single") or quiz.get("multiple") or quiz.get("fill"):
            lines += ["\n### 自测题"]
            for kind in ("single", "multiple", "fill"):
                for q in quiz.get(kind, []):
                    lines += [f"- **{q.get('q', '')}**", f"  - 答案：{q.get('answer', '')}；解析：{q.get('explain', '')}"]
    return "\n".join(lines) + "\n"


def render_push_markdown(cards: list[dict[str, Any]]) -> str:
    """微信/PushPlus 专用短版：不用宽表格，按记忆顺序分层展示。"""
    today = datetime.now().strftime("%m月%d日")
    lines = [f"# 考公时政速记｜{today}", f"> 今日精选 {len(cards)} 篇｜先背考点，再做自测"]
    for i, item in enumerate(cards, 1):
        title = str(item.get("source_title", "未命名材料")).replace("***", "").strip()
        lines += [f"\n## {i}. {title}"]
        summary = item.get("summary", "")
        if summary:
            lines.append(f"**一句话**：{summary}")
        if item.get("exam_focus") or item.get("theory_topic"):
            lines.append(f"**考纲**：{item.get('exam_focus', '政治理论')}｜{item.get('theory_topic', '待核对')}")
        for n, card in enumerate(item.get("cards", [])[:2], 1):
            front = str(card.get("front", "")).strip()
            back = str(card.get("back", "")).strip()
            trap = str(card.get("trap", "")).strip()
            lines += [f"**记忆{n}｜问** {front}", f"**答** {back}"]
            if trap and trap != "待核对":
                lines.append(f"**易错** {trap}")
        quiz = item.get("quiz", {}) or {}
        single = (quiz.get("single", []) or [])[:1]
        fill = (quiz.get("fill", []) or [])[:1]
        if single or fill:
            lines.append("**自测**")
            for q in single:
                options = " / ".join(str(x) for x in q.get("options", []))
                lines.append(f"1. {q.get('q', '')}（{options}）")
                lines.append(f"答案：{q.get('answer', '')}｜{q.get('explain', '')}")
            for q in fill:
                lines.append(f"2. {q.get('q', '')}")
                lines.append(f"答案：{q.get('answer', '')}｜{q.get('explain', '')}")
        lines.append(f"[原文]({item.get('source_url', '')})")
    return "\n".join(lines) + "\n"


def write_outputs(cards: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{datetime.now():%Y-%m-%d}.md").write_text(render_markdown(cards), encoding="utf-8")
    (output_dir / "cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "anki.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["Front", "Back", "Tag", "Source"])
        for item in cards:
            source = item.get("source_title", "") + " | " + item.get("source_url", "")
            for card in item.get("cards", []):
                back = f"{card.get('back', '')}<br>易错点：{card.get('trap', '')}<br>申论：{card.get('shenlun', '')}<br>口诀：{card.get('mnemonic', '')}"
                writer.writerow([card.get("front", ""), back, card.get("tag", ""), source])


def push(cfg: dict[str, Any], markdown: str) -> None:
    output = cfg.get("output", {})
    token = output.get("pushplus_token") or os.getenv("PUSHPLUS_TOKEN", "")
    if token:
        try:
            r = requests.post("https://www.pushplus.plus/send", json={"token": token, "title": "考公时政速记", "content": markdown, "template": "markdown"}, timeout=15)
            r.raise_for_status(); LOG.info("PushPlus 推送完成")
        except Exception as exc:
            LOG.error("PushPlus 推送失败：%s", exc)
    webhook = output.get("feishu_webhook") or os.getenv("FEISHU_WEBHOOK", "")
    if webhook:
        try:
            r = requests.post(webhook, json={"msg_type": "text", "content": {"text": markdown[:30000]}}, timeout=15)
            r.raise_for_status(); LOG.info("飞书推送完成")
        except Exception as exc:
            LOG.error("飞书推送失败：%s", exc)


def main() -> int:
    cfg = load_config()
    output_dir = Path(cfg.get("output", {}).get("dir", "output"))
    setup_logging(output_dir)
    db = ArticleDB(output_dir / "articles.sqlite3")
    session = requests.Session()
    articles, per_source = [], {}
    filt = cfg.get("filter", {})
    try:
        for source in cfg.get("sources", []):
            rows = fetch_source(source, session, float(filt.get("request_interval_seconds", 1.0)))
            count = 0
            for article in rows:
                if count >= int(filt.get("max_per_source", 5)) or not relevant(article, filt):
                    continue
                if db.is_duplicate(article, float(filt.get("title_similarity_threshold", 0.88))):
                    continue
                articles.append(article); count += 1
                if len(articles) >= int(filt.get("max_articles_total", 5)):
                    break
            per_source[source.get("name", "unknown")] = count
            if len(articles) >= int(filt.get("max_articles_total", 5)):
                break
        LOG.info("筛选完成：%d 篇，分源统计：%s", len(articles), per_source)
        generated = []
        for article in articles:
            result = call_llm(article, cfg.get("llm", {}))
            db.mark(article)  # 无论成功与否都记录，避免重复消耗；失败原文已写日志
            if result:
                generated.append(compact_result(result, filt))
        write_outputs(generated, output_dir)
        push(cfg, render_push_markdown(generated[: int(cfg.get("output", {}).get("push_max_articles", 5))]))
        LOG.info("完成：Markdown、anki.csv、cards.json 已写入 %s", output_dir)
    finally:
        db.close(); session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
