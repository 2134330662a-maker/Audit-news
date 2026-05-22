#!/usr/bin/env python3
"""
审计新闻每日推送工具
====================
全球与国内审计行业新闻自动抓取 → 翻译/摘要 → 飞书 & 钉钉推送
部署于 GitHub Actions，北京时间每早 8:30 定时运行。

新闻源:
  国际: Accounting Today, Journal of Accountancy, The CPA Journal（直接 RSS）
  国内: 中国注册会计师协会（社会团体，非政府机关）、中国会计视野（专业媒体）
       RSSHub 优先，不可用时回退到直接 HTML 抓取

推送目标:
  飞书自定义机器人（富文本 Post 格式 + 签名校验）
  钉钉自定义机器人（Markdown 格式 + 签名校验）
"""

import os
import sys
import json
import hmac
import hashlib
import base64
import time
import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

# ============================================================================
# Logging
# ============================================================================
LOG_LEVEL = logging.DEBUG if os.getenv("DEBUG", "") == "1" else logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("audit_news")

# ============================================================================
# Third-party imports (graceful fallback)
# ============================================================================

try:
    import requests
except ImportError:
    logger.critical("缺少依赖 requests。请运行: pip install requests")
    sys.exit(1)

try:
    import feedparser as fp
except ImportError:
    fp = None
    logger.warning("feedparser 未安装，将使用 xml.etree.ElementTree 手动解析 RSS/Atom")

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from deep_translator import GoogleTranslator as GT
except ImportError:
    GT = None
    logger.warning("deep-translator 未安装，翻译功能将不可用（英文新闻将保留原文）")

# ============================================================================
# Configuration
# ============================================================================

TZ_BEIJING = timezone(timedelta(hours=8))
RSSHUB_BASE = os.getenv("RSSHUB_BASE_URL", "https://rsshub.app")
TARGET_INTERNATIONAL = 10
TARGET_DOMESTIC = 0
NEWS_WINDOW_HOURS = 48
HTTP_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, */*",
    "Accept-Language": "zh-CN,en;q=0.9",
}

# ── 新闻源配置 ────────────────────────────────────────────────────────────
# type: "rss" 表示 RSS/Atom feed；"html" 表示需要 HTML 抓取
# category: "international" 或 "domestic"
# enabled: False 可禁用失效源，不影响整体运行
# ──────────────────────────────────────────────────────────────────────────

NEWS_SOURCES: List[Dict[str, Any]] = [
    # ========================================================================
    # 国际源 — 全部为已验证可用的直接 RSS feeds
    # 涵盖：审计、会计、税务、CFO 视角
    # ========================================================================
    {
        "name": "Journal of Accountancy",
        "url": "https://www.journalofaccountancy.com/feed/",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    {
        "name": "Accounting Today",
        "url": "https://www.accountingtoday.com/feed.rss",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    {
        "name": "The CPA Journal",
        "url": "https://www.cpajournal.com/feed/",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    {
        "name": "Thomson Reuters Tax",
        "url": "https://tax.thomsonreuters.com/news/feed/",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    {
        "name": "Accountancy Age",
        "url": "https://www.accountancyage.com/feed",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    {
        "name": "CFO Dive",
        "url": "https://www.cfodive.com/feeds/news/",
        "type": "rss",
        "category": "international",
        "enabled": True,
    },
    # ========================================================================
    # 国内源 — 默认关闭（HTML 抓取的链接在 DingTalk 中可能无法直接打开）
    # 如需启用，将 enabled 改为 True 即可
    # ========================================================================
    {
        "name": "中国会计视野",
        "url": "https://www.esnai.cn/33/",
        "type": "html",
        "category": "domestic",
        "enabled": False,
    },
    {
        "name": "中国注册会计师协会",
        "url": f"{RSSHUB_BASE}/cicpa/news",
        "type": "rss",
        "category": "domestic",
        "enabled": False,
        "fallback_url": "https://www.cicpa.org.cn/news/",
    },
]

# ============================================================================
# Data model
# ============================================================================

@dataclass
class NewsItem:
    """单条新闻。"""
    title: str
    url: str
    summary: str = ""
    source_name: str = ""
    category: str = ""               # "international" | "domestic"
    published_at: Optional[datetime] = None
    title_cn: str = ""               # 翻译后的中文标题
    summary_cn: str = ""             # 翻译后的中文摘要

    def age_hours(self, now: Optional[datetime] = None) -> float:
        if self.published_at is None:
            return 0.0
        ref = now or datetime.now(timezone.utc)
        dt = self.published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (ref - dt).total_seconds() / 3600.0

    def is_recent(self, window_hours: int = NEWS_WINDOW_HOURS,
                  now: Optional[datetime] = None) -> bool:
        if self.published_at is None:
            return True               # 未知时间 → 保守纳入
        return self.age_hours(now) <= window_hours

    def time_str(self) -> str:
        if self.published_at is None:
            return "时间未知"
        dt = self.published_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_bj = dt.astimezone(TZ_BEIJING)
        return dt_bj.strftime("%Y-%m-%d %H:%M")

    def display_title(self) -> str:
        """返回最佳可用的中文标题。"""
        return self.title_cn or self.title

    def display_summary(self) -> str:
        """返回最佳可用的中文摘要。"""
        return self.summary_cn or self.summary


# ============================================================================
# Date parsing helpers
# ============================================================================

def _parse_rfc822(date_str: str) -> Optional[datetime]:
    """解析 RFC 822 / RFC 2822 日期。"""
    from email.utils import parsedate_to_datetime as _p
    try:
        return _p(date_str)
    except Exception:
        return None

def _parse_iso(date_str: str) -> Optional[datetime]:
    """解析 ISO 8601 日期（含多种变体）。"""
    import re
    # 去掉末尾 Z → +00:00
    s = re.sub(r'Z$', '+00:00', date_str.strip())
    # 去掉亚秒精度 (Python ≤3.10 不兼容)
    s = re.sub(r'(\.\d{1,6})\d*', r'\1', s)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _parse_date_flex(date_str: Optional[str]) -> Optional[datetime]:
    """尝试多种格式解析日期字符串。"""
    if not date_str:
        return None
    for parser in (_parse_rfc822, _parse_iso):
        result = parser(date_str)
        if result:
            return result
    return None


# ============================================================================
# RSS / Atom parsing (primary: feedparser, fallback: xml.etree)
# ============================================================================

def _parse_rss_feedparser(xml_text: str) -> List[Dict[str, Any]]:
    """使用 feedparser 解析 RSS/Atom。"""
    parsed = fp.parse(xml_text)  # type: ignore[name-defined]
    items: List[Dict[str, Any]] = []
    for entry in parsed.entries:
        pub_dt: Optional[datetime] = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_dt = datetime.fromtimestamp(
                    calendar.timegm(entry.published_parsed), tz=timezone.utc
                )
            except Exception:
                pass
        if pub_dt is None and hasattr(entry, "published"):
            pub_dt = _parse_date_flex(entry.get("published", ""))

        summary = ""
        if hasattr(entry, "summary"):
            summary = _strip_html(entry.summary)
        elif hasattr(entry, "description"):
            summary = _strip_html(entry.description)

        items.append({
            "title": getattr(entry, "title", "").strip(),
            "url": getattr(entry, "link", "").strip(),
            "summary": summary[:300],
            "published_at": pub_dt,
        })
    return items


def _parse_rss_elementtree(xml_text: str) -> List[Dict[str, Any]]:
    """使用 xml.etree.ElementTree 手动解析 RSS 2.0 和 Atom。"""
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("XML 解析失败，跳过该 feed")
        return items

    # ── RSS 2.0 ──
    channel = root.find("channel")
    if channel is not None:
        for item_el in channel.findall("item"):
            title = (item_el.findtext("title") or "").strip()
            link = (item_el.findtext("link") or "").strip()
            desc = (item_el.findtext("description") or "").strip()
            pub_str = (item_el.findtext("pubDate") or "").strip()
            pub_dt = _parse_date_flex(pub_str)
            items.append({
                "title": title,
                "url": link,
                "summary": _strip_html(desc)[:300],
                "published_at": pub_dt,
            })
        if items:
            return items

    # ── Atom ──
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns) or root.findall("entry"):
        title_el = entry.find("atom:title", ns) or entry.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""

        link_el = entry.find("atom:link", ns) or entry.find("link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "") or link_el.text or ""
        link = link.strip()

        summary_el = (
            entry.find("atom:summary", ns) or entry.find("summary") or
            entry.find("atom:content", ns) or entry.find("content")
        )
        summary = (summary_el.text or "").strip() if summary_el is not None else ""

        pub_el = entry.find("atom:published", ns) or entry.find("published") or \
                 entry.find("atom:updated", ns) or entry.find("updated")
        pub_str = (pub_el.text or "").strip() if pub_el is not None else ""
        pub_dt = _parse_date_flex(pub_str)

        items.append({
            "title": title,
            "url": link,
            "summary": _strip_html(summary)[:300],
            "published_at": pub_dt,
        })
    return items


def _strip_html(text: str) -> str:
    """去除 HTML 标签，保留纯文本。"""
    if not text:
        return ""
    if BeautifulSoup:
        return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)
    import re
    return re.sub(r"<[^>]*>", " ", text).strip()


# ============================================================================
# News fetching
# ============================================================================

def _create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.timeout = HTTP_TIMEOUT
    return s


def _fetch_single_source(
    session: requests.Session, source: Dict[str, Any]
) -> List[NewsItem]:
    """抓取单个新闻源，返回 NewsItem 列表。任何异常均捕获，返回空列表。"""
    name = source["name"]
    url = source["url"]
    src_type = source.get("type", "rss")
    logger.info("正在抓取: %s (%s)", name, url)

    # ── HTML 类型：直接使用 HTML 抓取 ──
    if src_type == "html":
        return _fetch_html_source(session, source)

    # ── RSS 类型 ──
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.warning("⚠ 超时: %s", name)
        return []
    except requests.exceptions.ConnectionError:
        logger.warning("⚠ 连接失败: %s", name)
        return []
    except requests.exceptions.RequestException as e:
        logger.warning("⚠ HTTP 错误 [%s]: %s", name, e)
        return []

    xml_text = resp.text if resp.encoding else resp.content.decode("utf-8", errors="replace")

    # 优先使用 feedparser
    if fp is not None:
        try:
            raw_items = _parse_rss_feedparser(xml_text)
        except Exception as e:
            logger.warning("feedparser 解析失败 [%s]: %s, 回退到 ElementTree", name, e)
            raw_items = _parse_rss_elementtree(xml_text)
    else:
        raw_items = _parse_rss_elementtree(xml_text)

    if not raw_items:
        logger.info("   └─ 未提取到条目: %s", name)
        return []

    news_items: List[NewsItem] = []
    for ri in raw_items:
        if not ri.get("title") or not ri.get("url"):
            continue
        news_items.append(NewsItem(
            title=ri["title"],
            url=ri["url"],
            summary=ri.get("summary", "")[:300],
            source_name=name,
            category=source.get("category", "international"),
            published_at=ri.get("published_at"),
        ))

    logger.info("   └─ %s: 提取 %d 条", name, len(news_items))
    return news_items


def _fetch_html_source(
    session: requests.Session, source: Dict[str, Any]
) -> List[NewsItem]:
    """抓取 HTML 页面新闻（通用 DOM 策略，适配多种中文新闻列表页）。"""
    if BeautifulSoup is None:
        logger.warning("beautifulsoup4 未安装，无法抓取 HTML 新闻源")
        return []

    name = source["name"]
    url = source["url"]

    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("HTML 抓取失败 [%s]: %s", name, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items: List[NewsItem] = []

    # 策略 1：找 class 含 "news" / "list" / "article" 的 <a> 标签
    candidate_links: List[Any] = []
    for cls_hint in ["news", "list", "article", "title", "headline", "item"]:
        for a in soup.find_all("a", href=True, class_=lambda c: c and cls_hint in str(c).lower()):
            if a not in candidate_links:
                candidate_links.append(a)

    # 策略 2：如果策略 1 无结果，回退到所有正文区域内的 <a>
    if len(candidate_links) < 5:
        for container in soup.find_all(["article", "main", "section"], limit=5):
            for a in container.find_all("a", href=True):
                if a not in candidate_links:
                    candidate_links.append(a)

    # 策略 3：如果仍然不够，取页面所有标题长度的 <a>
    if len(candidate_links) < 3:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) >= 10:
                candidate_links.append(a)

    seen_urls: set = set()
    for a in candidate_links:
        title = a.get_text(strip=True)
        href = (a.get("href") or "").strip()
        if not title or len(title) < 10 or len(title) > 200:
            continue
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue

        # 补全相对 URL
        if not href.startswith("http"):
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                # 提取域名
                parts = url.split("/")
                domain = parts[0] + "//" + parts[2]
                href = domain + href
            else:
                href = url.rstrip("/") + "/" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        # 检查是否为新闻文章（URL 模式：含年月日或数字 ID）
        import re
        is_article_url = bool(
            re.search(r'/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/', href) or
            re.search(r'/(\d{4})(\d{2})(\d{2})/', href) or
            re.search(r'/\d{5,}\.', href) or
            href.endswith((".shtml", ".html", ".htm"))
        )

        items.append(NewsItem(
            title=title,
            url=href,
            source_name=name,
            category=source.get("category", "domestic"),
            published_at=None,
            # 按 URL 日期模式粗略排序（有日期的优先）
            summary="" if is_article_url else "",
        ))

    # 去重 & 限制
    logger.info("   └─ HTML [%s]: 提取 %d 条", name, min(len(items), 30))
    return items[:30]


def _fetch_html_fallback(
    session: requests.Session, source: Dict[str, Any]
) -> List[NewsItem]:
    """HTML 抓取回退：当 RSS/RSSHub 不可用时，直接抓取网页提取新闻标题。"""
    fallback_url = source.get("fallback_url", "")
    if not fallback_url:
        return []
    if BeautifulSoup is None:
        logger.warning("beautifulsoup4 未安装，无法使用 HTML 回退抓取")
        return []

    name = source["name"]
    logger.info("尝试 HTML 回退抓取: %s (%s)", name, fallback_url)

    try:
        resp = session.get(fallback_url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.warning("HTML 回退抓取失败 [%s]: %s", name, e)
        return []

    items: List[NewsItem] = []
    # 通用策略：提取页面中所有 <a> 标签，筛选可能为新闻链接的条目
    base_url = "/".join(fallback_url.rstrip("/").split("/")[:-1])

    for a_tag in soup.find_all("a", href=True):
        title = a_tag.get_text(strip=True)
        href = a_tag["href"].strip()
        if not title or len(title) < 8:
            continue
        # 排除明显的非新闻链接
        skip_patterns = [
            "首页", "关于", "联系", "登录", "注册", "更多", "查看详情",
            "首页", "上一页", "下一页", "首页", "末页", "English", "返回",
        ]
        if any(p in title for p in skip_patterns):
            continue

        # 补全相对 URL
        if not href.startswith("http"):
            if href.startswith("/"):
                domain = fallback_url.split("/")[0] + "//" + fallback_url.split("/")[2]
                href = domain + href
            else:
                href = fallback_url.rstrip("/") + "/" + href

        items.append(NewsItem(
            title=title,
            url=href,
            source_name=name,
            category=source.get("category", "domestic"),
            published_at=None,  # HTML 抓取无法精确获取时间
        ))

    # 按标题长度和内容相关性粗略排序，取前 20 条
    items = [it for it in items if any(
        kw in it.title for kw in [
            "审计", "会计", "注册", "CPA", "财务", "税务", "内控",
            "会计准则", "事务所", "鉴证", "年报", "审计师",
        ]
    )] or items  # 如果关键词筛选后为空，保留全部

    logger.info("   └─ HTML 回退 [%s]: 提取 %d 条", name, min(len(items), 20))
    return items[:20]


def fetch_all_news(session: Optional[requests.Session] = None) -> List[NewsItem]:
    """遍历所有启用的新闻源，抓取新闻。RSS 失败时自动尝试 HTML 回退。"""
    if session is None:
        session = _create_session()

    all_items: List[NewsItem] = []
    for src in NEWS_SOURCES:
        if not src.get("enabled", True):
            continue
        try:
            items = _fetch_single_source(session, src)
            # RSS 失败时尝试 HTML 回退
            if not items and src.get("fallback_url"):
                items = _fetch_html_fallback(session, src)
            all_items.extend(items)
        except Exception as e:
            logger.error("未预期的错误 [%s]: %s", src["name"], e, exc_info=True)
            # 即使异常也尝试 HTML 回退
            try:
                if src.get("fallback_url"):
                    items = _fetch_html_fallback(session, src)
                    all_items.extend(items)
            except Exception:
                pass
    return all_items


# ============================================================================
# Filtering & selection
# ============================================================================

def filter_recent(items: List[NewsItem],
                  window_hours: int = NEWS_WINDOW_HOURS) -> List[NewsItem]:
    """仅保留过去 N 小时内的新闻。"""
    now = datetime.now(timezone.utc)
    filtered = [item for item in items if item.is_recent(window_hours, now)]
    removed = len(items) - len(filtered)
    if removed > 0:
        logger.info("过滤掉 %d 条过期新闻（>%dh）", removed, window_hours)
    return filtered


# ── 会计/审计相关性关键词（过滤不相关的税务、泛商业新闻）──
RELEVANCE_KEYWORDS = [
    "audit", "auditing", "auditor",
    "account", "accounting", "accountant",
    "CPA", "CA ", "chartered accountant",
    "IFRS", "GAAP", "FASB", "IASB", "PCAOB",
    "financial report", "financial statement",
    "internal control", "SOX", "Sarbanes",
    "assurance", "attest",
    "tax prepar", "tax fraud", "tax professional",
    "bookkeep", "payroll",
    "Big Four", "Big 4", "Deloitte", "PwC",
    "EY ", "Ernst", "KPMG",
    "accounting firm", "audit firm", "CPA firm",
    "finance transformation", "digital finance",
]


def _is_accounting_relevant(item: NewsItem) -> bool:
    """判断新闻是否与会计/审计行业相关。"""
    text = (item.title + " " + item.summary).lower()
    for kw in RELEVANCE_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


def select_top(items: List[NewsItem],
               target_count: int,
               category: str,
               filter_relevant: bool = True) -> List[NewsItem]:
    """从指定 category 中按时间倒序选取至多 target_count 条。
    如果 filter_relevant=True，只保留会计/审计相关条目。"""
    subset = [it for it in items if it.category == category]

    # 相关性过滤（仅对国际英文源生效）
    if filter_relevant and category == "international":
        before = len(subset)
        subset = [it for it in subset if _is_accounting_relevant(it)]
        logger.info("相关性过滤: %d → %d 条", before, len(subset))

    # 按发布时间降序（None 排到最后）
    subset.sort(
        key=lambda x: (x.published_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    selected = subset[:target_count]
    logger.info("选取 %s 新闻: %d 条", category, len(selected))
    return selected


# ============================================================================
# Translation
# ============================================================================

def _translator_available() -> bool:
    return GT is not None


def _translate_single(text: str, source: str = "en", target: str = "zh-CN") -> str:
    """翻译单条文本。失败返回空字符串。"""
    if not text or not text.strip():
        return ""
    if not _translator_available():
        return ""
    try:
        result = GT(source=source, target=target).translate(text[:500])
        return result.strip() if result else ""
    except Exception as e:
        logger.debug("翻译失败 (%s): %s", text[:40], e)
        return ""


def translate_items(items: List[NewsItem]) -> List[NewsItem]:
    """为所有新闻条目翻译标题和摘要。"""
    if not _translator_available():
        logger.info("翻译库不可用，跳过翻译")
        return items

    logger.info("开始翻译 %d 条新闻...", len(items))
    for item in items:
        if item.category == "domestic":
            continue  # 国内新闻本身是中文，无需翻译

        # 翻译标题
        item.title_cn = _translate_single(item.title)
        # 翻译摘要
        if item.summary:
            item.summary_cn = _translate_single(item.summary)

        if item.title_cn:
            logger.debug("  ✓ 翻译成功: %s → %s", item.title[:30], item.title_cn[:30])
        else:
            logger.debug("  ✗ 翻译跳过: %s", item.title[:30])

    return items


def _llm_translate(text: str) -> str:
    """（可选）使用 OpenAI 兼容 API 进行翻译。需配置 LLM_API_KEY 等环境变量。"""
    api_key = os.getenv("LLM_API_KEY", "")
    api_base = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
    model = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
    if not api_key:
        return ""

    try:
        resp = requests.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一个专业翻译，将英文新闻翻译为简洁的中文。只输出翻译结果，不要解释。",
                    },
                    {"role": "user", "content": f"翻译为中文：{text}"},
                ],
                "max_tokens": 300,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("LLM 翻译失败: %s", e)
        return ""


# ============================================================================
# Push: Feishu (飞书)
# ============================================================================

def _feishu_sign(secret: str) -> Tuple[str, str]:
    """飞书签名校验：返回 (timestamp, sign)。"""
    ts = str(int(time.time()))
    sign_str = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256)
    sign = base64.b64encode(h.digest()).decode("utf-8")
    return ts, sign


def _build_feishu_post(
    intl_items: List[NewsItem],
    dom_items: List[NewsItem],
) -> Dict[str, Any]:
    """构建飞书富文本 Post 消息体。"""
    today_str = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
    content_blocks: List[List[Dict[str, Any]]] = []

    # ── 标题行 ──
    content_blocks.append([
        {"tag": "text", "text": f"📰 国际审计/会计日报 | {today_str}\n"}
    ])

    # ── 国际新闻 ──
    if intl_items:
        content_blocks.append([
            {"tag": "text", "text": "\n🌍 国际审计新闻\n"}
        ])
        for i, item in enumerate(intl_items, 1):
            blocks: List[Dict[str, Any]] = [
                {"tag": "text", "text": f"{i}. "},
                {"tag": "a", "text": item.display_title(), "href": item.url},
            ]
            if item.display_summary():
                summary = item.display_summary()
                if len(summary) > 150:
                    summary = summary[:150] + "..."
                blocks.append({"tag": "text", "text": f"\n   {summary}"})
            blocks.append({"tag": "text", "text": f"\n   📎 {item.source_name} · {item.time_str()}\n"})
            content_blocks.append(blocks)

    # ── 国内新闻 ──
    if dom_items:
        content_blocks.append([
            {"tag": "text", "text": "\n🇨🇳 国内行业动态\n"}
        ])
        for i, item in enumerate(dom_items, 1):
            blocks: List[Dict[str, Any]] = [
                {"tag": "text", "text": f"{i}. "},
                {"tag": "a", "text": item.display_title(), "href": item.url},
            ]
            if item.display_summary():
                summary = item.display_summary()
                if len(summary) > 150:
                    summary = summary[:150] + "..."
                blocks.append({"tag": "text", "text": f"\n   {summary}"})
            blocks.append({"tag": "text", "text": f"\n   📎 {item.source_name} · {item.time_str()}\n"})
            content_blocks.append(blocks)

    # ── 页脚 ──
    push_time = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M")
    content_blocks.append([
        {"tag": "text", "text": f"\n🤖 自动推送 · {push_time} · 仅供学习参考"}
    ])

    post = {
        "zh_cn": {
            "title": f"📰 国际审计/会计日报 | {today_str}",
            "content": content_blocks,
        }
    }

    return {
        "msg_type": "post",
        "content": {"post": post},
    }


def push_feishu(intl_items: List[NewsItem], dom_items: List[NewsItem]) -> bool:
    """推送到飞书机器人。"""
    webhook = os.getenv("FEISHU_WEBHOOK", "")
    if not webhook:
        logger.info("未配置 FEISHU_WEBHOOK，跳过飞书推送")
        return False

    secret = os.getenv("FEISHU_SECRET", "")
    body = _build_feishu_post(intl_items, dom_items)

    if secret:
        ts, sign = _feishu_sign(secret)
        body["timestamp"] = ts
        body["sign"] = sign
        logger.debug("飞书签名: ts=%s", ts)

    try:
        resp = requests.post(
            webhook,
            json=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0 or result.get("errcode") == 0:
            logger.info("✓ 飞书推送成功")
            return True
        else:
            logger.error("✗ 飞书推送失败: %s", result)
            return False
    except Exception as e:
        logger.error("✗ 飞书推送异常: %s", e)
        return False


# ============================================================================
# Push: DingTalk (钉钉)
# ============================================================================

def _dingtalk_sign(secret: str) -> Tuple[str, str]:
    """钉钉签名校验：返回 (timestamp_ms, sign_url_encoded)。"""
    ts = str(round(time.time() * 1000))
    sign_str = f"{ts}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256)
    sign = quote_plus(base64.b64encode(h.digest()).decode("utf-8"))
    return ts, sign


def _build_dingtalk_markdown(
    intl_items: List[NewsItem],
    dom_items: List[NewsItem],
) -> Dict[str, Any]:
    """构建钉钉 Markdown 消息体。"""
    today_str = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
    lines: List[str] = []

    lines.append(f"## 📰 国际审计/会计日报 | {today_str}")
    lines.append("")

    # ── 国际新闻 ──
    if intl_items:
        lines.append("### 🌍 国际审计新闻")
        lines.append("")
        for i, item in enumerate(intl_items, 1):
            lines.append(f"**{i}. [{item.display_title()}]({item.url})**")
            lines.append("")
            if item.display_summary():
                s = item.display_summary()
                if len(s) > 200:
                    s = s[:200] + "..."
                lines.append(s)
                lines.append("")
            lines.append(f"📎 {item.source_name} · {item.time_str()}")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── 国内新闻 ──
    if dom_items:
        lines.append("### 🇨🇳 国内行业动态")
        lines.append("")
        for i, item in enumerate(dom_items, 1):
            lines.append(f"**{i}. [{item.display_title()}]({item.url})**")
            lines.append("")
            if item.display_summary():
                s = item.display_summary()
                if len(s) > 200:
                    s = s[:200] + "..."
                lines.append(s)
                lines.append("")
            lines.append(f"📎 {item.source_name} · {item.time_str()}")
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── 页脚 ──
    push_time = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M")
    lines.append(f"> 🤖 自动推送 · {push_time} · 仅供学习参考")

    markdown_text = "\n".join(lines)

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": f"📰 国际审计/会计日报 | {today_str}",
            "text": markdown_text,
        },
    }


def push_dingtalk(intl_items: List[NewsItem], dom_items: List[NewsItem]) -> bool:
    """推送到钉钉机器人。"""
    webhook = os.getenv("DINGTALK_WEBHOOK", "")
    if not webhook:
        logger.info("未配置 DINGTALK_WEBHOOK，跳过钉钉推送")
        return False

    secret = os.getenv("DINGTALK_SECRET", "")
    url = webhook

    if secret:
        ts, sign = _dingtalk_sign(secret)
        url = f"{webhook}&timestamp={ts}&sign={sign}"
        logger.debug("钉钉签名: ts=%s", ts)

    body = _build_dingtalk_markdown(intl_items, dom_items)

    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("✓ 钉钉推送成功")
            return True
        else:
            logger.error("✗ 钉钉推送失败: %s", result)
            return False
    except Exception as e:
        logger.error("✗ 钉钉推送异常: %s", e)
        return False


# ============================================================================
# Main
# ============================================================================

def _check_environment() -> List[str]:
    """检查环境变量配置，返回缺失项列表。"""
    warnings: List[str] = []
    if not os.getenv("FEISHU_WEBHOOK") and not os.getenv("DINGTALK_WEBHOOK"):
        warnings.append(
            "FEISHU_WEBHOOK 和 DINGTALK_WEBHOOK 均未配置 —— 至少需要配置一个 Webhook 地址"
        )
    # signing secret 是可选的（取决于机器人安全设置）
    return warnings


def main() -> None:
    """主入口。"""
    logger.info("=" *  60)
    logger.info("审计新闻每日推送工具 启动")
    logger.info("当前时间 (UTC): %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("当前时间 (BJ):  %s", datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" *  60)

    # ── 环境检查 ──
    warnings = _check_environment()
    for w in warnings:
        logger.warning("⚠ %s", w)
    if warnings and not os.getenv("FEISHU_WEBHOOK") and not os.getenv("DINGTALK_WEBHOOK"):
        logger.error("无可用推送目标，退出")
        sys.exit(0)  # 不视为失败，避免 GitHub Actions 报红

    # ── 1. 抓取新闻 ──
    logger.info("── 第 1 步：抓取新闻 ──")
    session = _create_session()
    all_items = fetch_all_news(session)
    logger.info("抓取总计: %d 条（未过滤）", len(all_items))

    if not all_items:
        logger.warning("没有任何新闻被抓取到，可能所有源均不可达")
        sys.exit(0)

    # ── 2. 过滤近期新闻 ──
    logger.info("── 第 2 步：过滤近期新闻（%dh 窗口）──", NEWS_WINDOW_HOURS)
    recent = filter_recent(all_items)
    if not recent:
        logger.warning(
            "过去 %d 小时内没有新闻。如有 FEISHU_WEBHOOK，将推送'今日暂无更新'。",
            NEWS_WINDOW_HOURS,
        )
        # 发送"暂无更新"通知
        _push_empty_notification()
        sys.exit(0)

    # ── 3. 按类别选取 ──
    logger.info("── 第 3 步：选取 Top 新闻 ──")
    intl_items = select_top(recent, TARGET_INTERNATIONAL, "international")
    dom_items = select_top(recent, TARGET_DOMESTIC, "domestic")

    if not intl_items and not dom_items:
        logger.warning("过滤后无可用新闻")
        _push_empty_notification()
        sys.exit(0)

    # ── 4. 翻译 ──
    logger.info("── 第 4 步：翻译 ──")
    if GT is not None:
        intl_items = translate_items(intl_items)
    else:
        logger.info("跳过翻译（deep-translator 未安装）")

    # 如果配置了 LLM API，尝试用 LLM 补充翻译
    if os.getenv("LLM_API_KEY"):
        logger.info("检测到 LLM_API_KEY，尝试 LLM 翻译补充...")
        for item in intl_items:
            if not item.title_cn:
                item.title_cn = _llm_translate(item.title)
            if not item.summary_cn and item.summary:
                item.summary_cn = _llm_translate(item.summary)

    # ── 5. 推送 ──
    logger.info("── 第 5 步：推送 ──")
    feishu_ok = push_feishu(intl_items, dom_items)
    dingtalk_ok = push_dingtalk(intl_items, dom_items)

    # ── 6. 报告 ──
    logger.info("── 完成 ──")
    logger.info("飞书: %s", "✓" if feishu_ok else "✗ (跳过或失败)")
    logger.info("钉钉: %s", "✓" if dingtalk_ok else "✗ (跳过或失败)")
    logger.info("国际新闻 %d 条 | 国内新闻 %d 条", len(intl_items), len(dom_items))


def _push_empty_notification() -> None:
    """当日无新闻时发送简短通知。"""
    today_str = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")

    # Feishu
    feishu_url = os.getenv("FEISHU_WEBHOOK", "")
    feishu_secret = os.getenv("FEISHU_SECRET", "")
    if feishu_url:
        body = {
            "msg_type": "text",
            "content": {
                "text": f"📰 国际审计/会计日报 | {today_str}\n\n今日暂无更新，请稍后再试。\n\n🤖 自动推送 · {datetime.now(TZ_BEIJING).strftime('%Y-%m-%d %H:%M')}"
            },
        }
        if feishu_secret:
            ts, sign = _feishu_sign(feishu_secret)
            body["timestamp"] = ts
            body["sign"] = sign
        try:
            requests.post(feishu_url, json=body, timeout=HTTP_TIMEOUT)
        except Exception:
            pass

    # DingTalk
    dt_url = os.getenv("DINGTALK_WEBHOOK", "")
    dt_secret = os.getenv("DINGTALK_SECRET", "")
    if dt_url:
        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📰 国际审计/会计日报 | {today_str}",
                "text": f"## 📰 国际审计/会计日报 | {today_str}\n\n今日暂无更新，请稍后再试。\n\n> 🤖 自动推送 · {datetime.now(TZ_BEIJING).strftime('%Y-%m-%d %H:%M')}",
            },
        }
        url = dt_url
        if dt_secret:
            ts, sign = _dingtalk_sign(dt_secret)
            url = f"{dt_url}&timestamp={ts}&sign={sign}"
        try:
            requests.post(url, json=body, timeout=HTTP_TIMEOUT)
        except Exception:
            pass


if __name__ == "__main__":
    main()
