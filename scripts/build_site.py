#!/usr/bin/env python3
"""Validate the daily Markdown archive and atomically build its static site."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from html import escape, unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bleach
import markdown
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://100apps.github.io/decision-daily"
SITE_TITLE = "决策情报日报"
REQUIRED_FIELDS = ("title", "date", "timezone", "summary", "status")
ALLOWED_TAGS = frozenset(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br", "pre", "div",
    "span", "table", "thead", "tbody", "tr", "th", "td", "del", "sup", "sub",
}
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "id"], "div": ["class"], "span": ["class"],
    "code": ["class"], "h1": ["id"], "h2": ["id"], "h3": ["id"],
    "h4": ["id"], "h5": ["id"], "h6": ["id"],
}


class ValidationError(ValueError):
    """An issue cannot safely be published."""


@dataclass(frozen=True)
class Item:
    title: str
    category: str
    anchor: str
    excerpt: str
    source_count: int


@dataclass(frozen=True)
class Issue:
    path: Path
    date: date
    title: str
    timezone: str
    generated_at: datetime
    summary: str
    body: str
    html: str
    toc: str
    items: tuple[Item, ...]

    @property
    def relative_html(self) -> str:
        return f"daily/{self.date:%Y/%m/%Y-%m-%d}.html"

    @property
    def relative_markdown(self) -> str:
        return f"daily/{self.date:%Y/%m/%Y-%m-%d}.md"


class TextAndLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.h3_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"] or "")
        if tag == "h3":
            self.h3_ids.append(attributes.get("id") or "")
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.text.append(" ")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def plain_text(fragment: str) -> str:
    parser = TextAndLinks()
    parser.feed(fragment)
    return re.sub(r"\s+", " ", "".join(parser.text)).strip()


def slugify(value: str, separator: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"[^\w\s-]", "", value)
    return re.sub(r"[-\s]+", separator, value) or "section"


def clean_html(value: str) -> str:
    return bleach.clean(value, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES,
                        protocols={"http", "https", "mailto"}, strip=True)


def render_markdown(body: str) -> tuple[str, str]:
    renderer = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": slugify, "toc_depth": "2-3"}},
        output_format="html",
    )
    rendered = clean_html(renderer.convert(body))
    return rendered, clean_html(renderer.toc)


def validate_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"https", "http"} or not parsed.netloc
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValidationError("base-url 必须是无查询参数、片段和认证信息的 HTTP(S) 地址")
    return base_url.rstrip("/")


def site_url(base_url: str, relative: str = "") -> str:
    return f"{base_url}/{relative.lstrip('/')}"


def local_url(base_url: str, relative: str = "") -> str:
    """Root-relative URL preserving a GitHub Pages project subpath."""
    root = urlsplit(base_url).path.rstrip("/")
    return f"{root}/{relative.lstrip('/')}"


def rewrite_issue_links(fragment: str, base_url: str) -> str:
    """Resolve the archive's explicit daily-root date links for web readers."""
    def replace(match: re.Match[str]) -> str:
        parsed = urlsplit(unescape(match.group(1)))
        dated_path = re.fullmatch(r"(\d{4})/(\d{2})/(\d{4}-\d{2}-\d{2})\.md", parsed.path)
        if parsed.scheme or parsed.netloc or not dated_path:
            return match.group(0)
        try:
            target_date = date.fromisoformat(dated_path.group(3))
        except ValueError:
            return match.group(0)
        if parsed.path != f"{target_date:%Y/%m/%Y-%m-%d}.md":
            return match.group(0)
        target = local_url(base_url, f"daily/{target_date:%Y/%m/%Y-%m-%d}.html")
        if parsed.query:
            target += "?" + parsed.query
        if parsed.fragment:
            target += "#" + parsed.fragment
        return f'href="{escape(target, quote=True)}"'
    return re.sub(r'href="([^"]*)"', replace, fragment)


def extract_section(fragment: str, title: str) -> str:
    headings = list(re.finditer(r"<h2\b[^>]*>(.*?)</h2>", fragment, re.S))
    for index, heading in enumerate(headings):
        if plain_text(heading.group(1)) == title:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(fragment)
            return fragment[heading.start():end]
    return ""


def load_issue(path: Path, source_root: Path) -> Issue | None:
    path, source_root = Path(path), Path(source_root)
    relative = path.relative_to(source_root)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"{relative}: 无法读取 UTF-8 Markdown: {exc}") from exc
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, re.S)
    if not match:
        raise ValidationError(f"{relative}: 缺少 YAML front matter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValidationError(f"{relative}: YAML 格式错误: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValidationError(f"{relative}: front matter 必须是映射")
    if meta.get("status") == "draft":
        return None
    for field in REQUIRED_FIELDS:
        if field not in meta or not str(meta[field]).strip():
            raise ValidationError(f"{relative}: 缺少必需元数据 {field}")
    if meta["status"] != "published":
        raise ValidationError(f"{relative}: status 必须是 published 或 draft")
    for field in ("title", "summary", "timezone"):
        if not isinstance(meta[field], str):
            raise ValidationError(f"{relative}: {field} 必须是字符串")
    raw_date = str(meta["date"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        raise ValidationError(f"{relative}: date 必须为 YYYY-MM-DD")
    try:
        issue_date = date.fromisoformat(raw_date)
        ZoneInfo(meta["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValidationError(f"{relative}: 日期或时区无效: {exc}") from exc
    expected = Path(f"{issue_date:%Y/%m/%Y-%m-%d}.md")
    if relative != expected:
        raise ValidationError(f"{relative}: 日期与目录不匹配，应为 {expected}")
    generated_at = datetime.combine(issue_date, datetime.min.time(), ZoneInfo(meta["timezone"]))
    if "generated_at" in meta:
        try:
            generated_at = datetime.fromisoformat(str(meta["generated_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{relative}: generated_at 必须是带时区的 ISO 日期时间") from exc
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValidationError(f"{relative}: generated_at 必须包含时区")

    body = match.group(2).strip()
    rendered, toc = render_markdown(body)
    parser = TextAndLinks()
    parser.feed(rendered)
    # Fenced code must not create imaginary headings or evidence.
    outline_body = re.sub(r"(?ms)^(`{3,}|~{3,}).*?\n.*?^\1\s*$", "", body)
    headings = list(re.finditer(r"(?m)^(#{2,3})[ \t]+(.+?)[ \t]*#*[ \t]*$", outline_body))
    item_headings = [heading for heading in headings if heading.group(1) == "###"]
    if not 10 <= len(item_headings) <= 20:
        raise ValidationError(f"{relative}: 信息条目须为 10–20 条，目前为 {len(item_headings)} 条")
    if len(parser.h3_ids) != len(item_headings):
        raise ValidationError(f"{relative}: 请统一使用 Markdown 三级标题组织信息条目")

    items: list[Item] = []
    category = ""
    for index, heading in enumerate(headings):
        title = plain_text(markdown.markdown(heading.group(2)))
        if heading.group(1) == "##":
            category = title
            continue
        if not category:
            raise ValidationError(f"{relative}: 条目 {title} 缺少二级模块标题")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(outline_body)
        fragment, _ = render_markdown(outline_body[heading.end():end])
        evidence = TextAndLinks()
        evidence.feed(fragment)
        source_links = {
            link for link in evidence.links
            if urlsplit(link).scheme in {"http", "https"} and urlsplit(link).netloc
        }
        if not source_links:
            raise ValidationError(f"{relative}: 条目「{title}」缺少可点击的 HTTP(S) 证据链接")
        argument = re.search(r"<p><strong>论点[：:]?</strong>(.*?)</p>", fragment, re.S)
        text_excerpt = plain_text(argument.group(1) if argument else fragment).lstrip("：: ")
        excerpt = text_excerpt[:112] + ("…" if len(text_excerpt) > 112 else "")
        items.append(Item(title, category, parser.h3_ids[len(items)], excerpt, len(source_links)))
    return Issue(path, issue_date, meta["title"].strip(), meta["timezone"].strip(),
                 generated_at, meta["summary"].strip(), body, rendered, toc, tuple(items))


def load_issues(source_root: Path) -> list[Issue]:
    source_root = Path(source_root)
    if not source_root.is_dir():
        raise ValidationError(f"日报目录不存在：{source_root}")
    issues: list[Issue] = []
    dates: set[date] = set()
    for path in sorted(source_root.rglob("*.md")):
        if path.is_symlink():
            raise ValidationError(f"不发布符号链接文件：{path}")
        issue = load_issue(path, source_root)
        if issue is not None:
            if issue.date in dates:
                raise ValidationError(f"日期重复：{issue.date}")
            dates.add(issue.date)
            issues.append(issue)
    if not issues:
        raise ValidationError("没有通过校验的已发布日报，保留现有站点")
    return sorted(issues, key=lambda issue: issue.date, reverse=True)


def page_shell(title: str, description: str, content: str, *, base_url: str,
               relative: str, active: str = "", body_class: str = "") -> str:
    url = lambda path="": escape(local_url(base_url, path), quote=True)
    canonical = escape(site_url(base_url, relative), quote=True)
    nav_current = lambda key: ' aria-current="page"' if active == key else ""
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{escape(title)} · {SITE_TITLE}</title>
  <meta name="description" content="{escape(description, quote=True)}">
  <link rel="canonical" href="{canonical}">
  <link rel="alternate" type="application/atom+xml" title="{SITE_TITLE}" href="{url('atom.xml')}">
  <link rel="stylesheet" href="{url('assets/style.css')}">
  <script src="{url('assets/app.js')}" defer></script>
</head>
<body class="{escape(body_class, quote=True)}">
  <a class="skip-link" href="#main">跳到正文</a>
  <header class="site-header"><div class="header-inner">
    <a class="brand" href="{url()}"><span class="brand-mark" aria-hidden="true">知</span><span>{SITE_TITLE}<small>让信息进入决策</small></span></a>
    <nav aria-label="主导航"><a href="{url()}"{nav_current('latest')}>最新一期</a><a href="{url('archive/')}"{nav_current('archive')}>往期归档</a><a class="feed-link" href="{url('atom.xml')}">订阅</a></nav>
  </div></header>
  <main id="main">{content}</main>
  <footer class="site-footer"><div><strong>{SITE_TITLE}</strong><p>事实有出处，推断有边界，行动有复盘。</p></div><p>每天 09:00 开始编写 · 北京时间<br>完成核查后发布</p><a href="{url('archive/')}">按年月浏览 <span aria-hidden="true">↗</span></a></footer>
</body>
</html>'''


def issue_card(issue: Issue, base_url: str, *, archive: bool = False) -> str:
    search = f"{issue.date} {issue.title} {issue.summary} " + " ".join(item.title for item in issue.items)
    return f'''<article class="issue-card" data-search="{escape(search, quote=True)}">
<div class="card-date"><time datetime="{issue.date}">{issue.date:%m.%d}</time><span>{issue.date.year}</span></div>
<div><p class="eyebrow">{len(issue.items)} 条关键信息</p><h3><a href="{escape(local_url(base_url, issue.relative_html), quote=True)}">{escape(issue.title)}</a></h3><p>{escape(issue.summary)}</p></div><span class="card-arrow" aria-hidden="true">↗</span></article>'''


def build_home(issues: list[Issue], base_url: str) -> str:
    latest = issues[0]
    grouped: dict[str, list[Item]] = defaultdict(list)
    for item in latest.items:
        grouped[item.category].append(item)
    issue_link = escape(local_url(base_url, latest.relative_html), quote=True)
    preview = ""
    for category, items in grouped.items():
        preview += f'<section class="topic-section"><div class="section-label"><h2>{escape(category)}</h2><span>{len(items):02d}</span></div><div class="topic-grid">'
        for item in items:
            preview += f'''<article class="topic-card"><h3><a href="{issue_link}#{quote(item.anchor)}">{escape(item.title)}</a></h3><p>{escape(item.excerpt)}</p><span class="source-count">{item.source_count} 个证据来源 <span aria-hidden="true">↗</span></span></article>'''
        preview += "</div></section>"
    recent = "".join(issue_card(issue, base_url) for issue in issues[1:5])
    history = f'<section class="recent-section"><div class="section-heading"><h2>继续阅读</h2><a href="{escape(local_url(base_url, "archive/"))}">全部归档 →</a></div>{recent}</section>' if recent else ""
    priorities = extract_section(latest.html, "今日三项优先行动")
    if priorities:
        priorities = '<section class="priority-section prose">' + rewrite_issue_links(priorities, base_url) + '</section>'
    content = f'''<div class="page-width">
<section class="hero"><div class="hero-copy"><p class="eyebrow">DAILY INTELLIGENCE / 每日决策参考</p><h1>看清变化。<br><span>做出行动。</span></h1><p class="hero-intro">从纷杂信息中，找到与你的资产、职业、经营和生活有关的变化。</p></div><div class="edition-stamp"><span>最新一期</span><time datetime="{latest.date}">{latest.date:%m.%d}</time><span>{latest.date.year} · {len(latest.items)} 条信息</span></div></section>
<p class="freshness-note" id="freshness-note" data-latest-date="{latest.date}" hidden>今日一期尚未发布，当前为最近一期。每日 09:00 开始编写，核查完成后更新。</p>
<section class="latest-feature"><div><p class="eyebrow">今日阅读 · {latest.date:%Y 年 %m 月 %d 日}</p><h2><a href="{issue_link}">{escape(latest.title)}</a></h2><p>{escape(latest.summary)}</p></div><a class="primary-link" href="{issue_link}">阅读完整日报 <span aria-hidden="true">→</span></a></section>
<div class="reading-note"><span>每条信息</span><p>论点 → 证据 → 影响路径 → 行动 → 成本与收益 → 反证</p></div>
{priorities}{preview}{history}</div>'''
    return page_shell("最新一期", latest.summary, content, base_url=base_url, relative="", active="latest")


def build_issue(issue: Issue, *, base_url: str, newer: Issue | None, older: Issue | None) -> str:
    def navigation(target: Issue | None, label: str) -> str:
        if target is None:
            return f'<div class="adjacent-empty"><span>{label}</span><p>已到归档边界</p></div>'
        return f'<a href="{escape(local_url(base_url, target.relative_html), quote=True)}"><span>{label} · {target.date}</span><strong>{escape(target.title)}</strong></a>'
    content = f'''<div class="page-width issue-layout">
<header class="article-header"><a class="back-link" href="{escape(local_url(base_url, 'archive/'))}">← 所有日报</a><p class="eyebrow">{issue.date:%Y 年 %m 月 %d 日} · {len(issue.items)} 条关键信息</p><h1>{escape(issue.title)}</h1><p class="article-summary">{escape(issue.summary)}</p><div class="article-tools"><span>日期按北京时间</span><a href="{escape(local_url(base_url, issue.relative_markdown))}" download>下载 Markdown</a><button type="button" data-print>打印 / 存为 PDF</button></div></header>
<aside class="article-sidebar"><details class="toc-panel" open><summary>本期目录</summary><nav aria-label="本期目录">{issue.toc}</nav></details></aside>
<article class="prose">{rewrite_issue_links(issue.html, base_url)}</article>
<nav class="adjacent-issues" aria-label="相邻日报">{navigation(older, '上一篇')}{navigation(newer, '下一篇')}</nav>
</div>'''
    return page_shell(issue.title, issue.summary, content, base_url=base_url,
                      relative=issue.relative_html, body_class="issue-page")


def build_archive(issues: list[Issue], base_url: str) -> str:
    grouped: dict[str, list[Issue]] = defaultdict(list)
    for issue in issues:
        grouped[f"{issue.date:%Y-%m}"].append(issue)
    jumps = "".join(f'<a href="#month-{month}">{month.replace("-", ".")} <span>{len(group)}</span></a>' for month, group in grouped.items())
    sections = ""
    for month, group in grouped.items():
        year, number = month.split("-")
        sections += f'<section class="archive-month" id="month-{month}"><div class="section-heading"><h2>{year} 年 {number} 月</h2><span>{len(group)} 期</span></div>'
        sections += "".join(issue_card(issue, base_url, archive=True) for issue in group)
        sections += "</section>"
    content = f'''<div class="page-width archive-page"><header class="archive-header"><p class="eyebrow">THE ARCHIVE / 长期积累</p><h1>信息会过去，<br>判断值得留下。</h1><p>共 {len(issues)} 期 · 从 {issues[-1].date} 到 {issues[0].date}</p></header><div class="archive-controls"><label for="archive-search">搜索日期、议题或关键词</label><input type="search" id="archive-search" placeholder="例如：2026-09、现金流、AI" autocomplete="off"><p id="search-status" class="search-status" role="status" aria-live="polite"></p></div><nav class="month-nav" aria-label="按年月浏览">{jumps}</nav><div id="archive-results">{sections}</div><p id="search-empty" hidden>没有找到匹配的日报，试试更简短的关键词。</p></div>'''
    return page_shell("往期归档", "按年月浏览、搜索历期决策情报日报。", content,
                      base_url=base_url, relative="archive/", active="archive")


def build_feed(issues: list[Issue], base_url: str) -> bytes:
    namespace = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", namespace)
    def child(parent: ET.Element, tag: str, value: str = "", **attrs: str) -> ET.Element:
        node = ET.SubElement(parent, f"{{{namespace}}}{tag}", attrs)
        node.text = value or None
        return node
    feed = ET.Element(f"{{{namespace}}}feed")
    child(feed, "title", SITE_TITLE)
    child(feed, "id", site_url(base_url))
    child(feed, "link", href=site_url(base_url))
    child(feed, "link", href=site_url(base_url, "atom.xml"), rel="self")
    child(feed, "updated", max(issue.generated_at for issue in issues).isoformat())
    author = child(feed, "author")
    child(author, "name", SITE_TITLE)
    for issue in issues[:30]:
        entry = child(feed, "entry")
        child(entry, "title", issue.title)
        child(entry, "id", site_url(base_url, issue.relative_html))
        child(entry, "link", href=site_url(base_url, issue.relative_html))
        child(entry, "updated", issue.generated_at.isoformat())
        child(entry, "summary", issue.summary)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def build_site(source_root: Path, output: Path, base_url: str = DEFAULT_BASE_URL,
               *, validate_only: bool = False) -> list[Issue]:
    source_root, output = Path(source_root).resolve(), Path(output).absolute()
    base_url = validate_base_url(base_url)
    resolved_output = output.resolve()
    if source_root == resolved_output or source_root in resolved_output.parents or resolved_output in source_root.parents:
        raise ValidationError("源目录与输出目录不能相同或相互包含")
    if output.is_symlink():
        raise ValidationError("输出目录不能是符号链接")
    issues = load_issues(source_root)
    if validate_only:
        return issues
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-build-", dir=output.parent))
    backup: Path | None = None
    try:
        (staging / "assets").mkdir()
        for name in ("style.css", "app.js"):
            shutil.copyfile(PROJECT_ROOT / "site" / "assets" / name, staging / "assets" / name)
        (staging / "index.html").write_text(build_home(issues, base_url), encoding="utf-8")
        (staging / "archive").mkdir()
        (staging / "archive" / "index.html").write_text(build_archive(issues, base_url), encoding="utf-8")
        for index, issue in enumerate(issues):
            destination = staging / issue.relative_html
            destination.parent.mkdir(parents=True, exist_ok=True)
            newer = issues[index - 1] if index else None
            older = issues[index + 1] if index + 1 < len(issues) else None
            destination.write_text(build_issue(issue, base_url=base_url, newer=newer, older=older), encoding="utf-8")
            shutil.copyfile(issue.path, staging / issue.relative_markdown)
        (staging / "atom.xml").write_bytes(build_feed(issues, base_url))
        (staging / ".nojekyll").touch()
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{output.name}-previous-", dir=output.parent))
            backup.rmdir()
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except BaseException:
            if backup is not None and backup.exists():
                os.replace(backup, output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "daily", help="日报 Markdown 根目录")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs", help="静态站输出目录")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="站点完整 URL，支持项目子路径")
    parser.add_argument("--validate-only", action="store_true", help="只校验所有已发布日报，不改动输出目录")
    args = parser.parse_args(argv)
    try:
        issues = build_site(args.source, args.output, args.base_url, validate_only=args.validate_only)
    except (ValidationError, OSError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    action = "校验通过" if args.validate_only else f"已生成 {args.output}"
    print(f"{action}：{len(issues)} 期，最新 {issues[0].date}，{len(issues[0].items)} 条信息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
