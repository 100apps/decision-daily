import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
builder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = builder
spec.loader.exec_module(builder)


def example_issue(day="2026-09-07", *, count=10, source=True, extra="", meta_extra=""):
    metadata = f'''---
title: "今天值得关注的变化"
date: "{day}"
timezone: Asia/Shanghai
summary: "用证据支持判断，以行动改善生活。"
status: published
{meta_extra}---

## 资产与现金流
'''
    for index in range(count):
        link = f"[官方来源](https://example.com/evidence/{index})" if source else "没有来源"
        metadata += f"\n### {index + 1:02d}｜信息 {index + 1}\n\n**论点**：需要查证的变化。\n\n{link}\n\n{extra}\n"
    return metadata


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "daily"
        self.source.mkdir()
        self.output = self.root / "docs"

    def tearDown(self):
        self.temporary.cleanup()

    def write_issue(self, day="2026-09-07", **kwargs):
        path = self.source / day[:4] / day[5:7] / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example_issue(day, **kwargs), encoding="utf-8")
        return path

    def test_cross_month_and_year_archive_links_and_markdown(self):
        for day in ["2025-12-31", "2026-01-01", "2026-09-07"]:
            self.write_issue(day)
        issues = builder.build_site(self.source, self.output)
        self.assertEqual([str(issue.date) for issue in issues], ["2026-09-07", "2026-01-01", "2025-12-31"])
        archive = (self.output / "archive/index.html").read_text()
        self.assertIn('id="month-2025-12"', archive)
        self.assertIn('id="month-2026-01"', archive)
        middle = (self.output / "daily/2026/01/2026-01-01.html").read_text()
        self.assertIn("/decision-daily/daily/2025/12/2025-12-31.html", middle)
        self.assertIn("/decision-daily/daily/2026/09/2026-09-07.html", middle)
        self.assertTrue((self.output / "daily/2026/01/2026-01-01.md").is_file())
        self.assertTrue((self.output / ".nojekyll").exists())
        feed = ET.parse(self.output / "atom.xml")
        self.assertEqual(len(feed.findall("{http://www.w3.org/2005/Atom}entry")), 3)

    def test_subpath_urls_are_preserved_everywhere(self):
        self.write_issue()
        builder.build_site(self.source, self.output, "https://reader.example/nested/intel/")
        for path in self.output.rglob("*.html"):
            html = path.read_text()
            self.assertIn('href="/nested/intel/assets/style.css"', html)
            self.assertIn('src="/nested/intel/assets/app.js"', html)
            self.assertNotIn('href="/daily/', html)
            self.assertNotIn('href="/assets/', html)
        home = (self.output / "index.html").read_text()
        self.assertIn('href="https://reader.example/nested/intel/"', home)
        self.assertIn('data-latest-date="2026-09-07"', home)

    def test_invalid_date_and_mismatched_path_fail(self):
        path = self.write_issue("2026-02-30")
        with self.assertRaisesRegex(builder.ValidationError, "日期或时区无效"):
            builder.load_issue(path, self.source)
        path.write_text(example_issue("2026-03-01"))
        with self.assertRaisesRegex(builder.ValidationError, "目录不匹配"):
            builder.load_issue(path, self.source)

    def test_incomplete_metadata_or_bad_status_fails(self):
        path = self.write_issue()
        text = path.read_text().replace("timezone: Asia/Shanghai\n", "")
        path.write_text(text)
        with self.assertRaisesRegex(builder.ValidationError, "timezone"):
            builder.load_issue(path, self.source)
        path.write_text(example_issue().replace("status: published", "status: unknown"))
        with self.assertRaisesRegex(builder.ValidationError, "status"):
            builder.load_issue(path, self.source)

    def test_drafts_are_ignored(self):
        self.write_issue()
        (self.source / "draft.md").write_text("---\nstatus: draft\n---\n草稿")
        self.assertEqual(len(builder.load_issues(self.source)), 1)

    def test_item_count_and_sources_required(self):
        path = self.write_issue(count=9)
        with self.assertRaisesRegex(builder.ValidationError, "10–20"):
            builder.load_issue(path, self.source)
        path.write_text(example_issue(count=21))
        with self.assertRaisesRegex(builder.ValidationError, "10–20"):
            builder.load_issue(path, self.source)
        path.write_text(example_issue(source=False))
        with self.assertRaisesRegex(builder.ValidationError, "证据链接"):
            builder.load_issue(path, self.source)

    def test_raw_html_and_metadata_cannot_inject_scripts(self):
        path = self.write_issue(extra='<script>alert("x")</script><img src=x onerror="alert(1)"><a href="javascript:alert(1)">bad</a><iframe src="https://evil.example"></iframe>')
        text = path.read_text().replace('title: "今天值得关注的变化"', "title: '<script>bad</script>'")
        path.write_text(text)
        builder.build_site(self.source, self.output)
        html = (self.output / "daily/2026/09/2026-09-07.html").read_text()
        self.assertNotIn('<script>alert', html)
        self.assertNotIn('onerror=', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('<iframe', html)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', html)

    def test_validation_failure_preserves_previous_site(self):
        path = self.write_issue()
        builder.build_site(self.source, self.output)
        original = (self.output / "index.html").read_bytes()
        path.write_text(example_issue(count=2))
        with self.assertRaises(builder.ValidationError):
            builder.build_site(self.source, self.output)
        self.assertEqual((self.output / "index.html").read_bytes(), original)
        self.assertEqual(list(self.root.glob(".docs-*")), [])

    def test_render_failure_preserves_previous_site(self):
        self.write_issue()
        builder.build_site(self.source, self.output)
        original = (self.output / "index.html").read_bytes()
        with mock.patch.object(builder, "build_issue", side_effect=RuntimeError("render failed")):
            with self.assertRaises(RuntimeError):
                builder.build_site(self.source, self.output)
        self.assertEqual((self.output / "index.html").read_bytes(), original)
        self.assertEqual(list(self.root.glob(".docs-*")), [])

    def test_validate_only_writes_nothing(self):
        self.write_issue()
        builder.build_site(self.source, self.output, validate_only=True)
        self.assertFalse(self.output.exists())

    def test_empty_source_preserves_old_output(self):
        self.output.mkdir()
        (self.output / "index.html").write_text("old")
        with self.assertRaisesRegex(builder.ValidationError, "没有通过校验"):
            builder.build_site(self.source, self.output)
        self.assertEqual((self.output / "index.html").read_text(), "old")

    def test_fake_heading_in_fenced_code_not_counted(self):
        path = self.write_issue(extra="```markdown\n### 并非条目\n```")
        issue = builder.load_issue(path, self.source)
        self.assertEqual(len(issue.items), 10)

    def test_history_markdown_links_resolve_with_fragments(self):
        self.write_issue(extra="[上次判断](2025/12/2025-12-31.md#01-信息-1)")
        self.write_issue("2025-12-31")
        builder.build_site(self.source, self.output, "https://reader.example/nested/intel")
        html = (self.output / "daily/2026/09/2026-09-07.html").read_text()
        self.assertIn('href="/nested/intel/daily/2025/12/2025-12-31.html#01-信息-1"', html)
        unchanged = '<a href="https://example.com/2025/12/2025-12-31.md">外部文档</a><a href="notes.md">本地笔记</a>'
        self.assertEqual(builder.rewrite_issue_links(unchanged, builder.DEFAULT_BASE_URL), unchanged)

    def test_home_priorities_and_generated_time(self):
        path = self.write_issue(meta_extra='generated_at: "2026-09-07T23:16:00+08:00"\n')
        content = path.read_text().replace("## 资产与现金流", "## 今日三项优先行动\n\n1. 核查暴露\n2. 整理应收\n3. 记录真实任务\n\n## 资产与现金流", 1)
        path.write_text(content)
        builder.build_site(self.source, self.output)
        home = (self.output / "index.html").read_text()
        self.assertIn('<section class="priority-section prose">', home)
        self.assertIn("核查暴露", home)
        feed = ET.parse(self.output / "atom.xml")
        self.assertEqual(feed.find("{http://www.w3.org/2005/Atom}updated").text, "2026-09-07T23:16:00+08:00")
        self.assertEqual(feed.find("{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}updated").text, "2026-09-07T23:16:00+08:00")

    def test_generated_time_requires_offset_and_falls_back_to_date(self):
        path = self.write_issue(meta_extra='generated_at: "2026-09-07T09:00:00"\n')
        with self.assertRaisesRegex(builder.ValidationError, "必须包含时区"):
            builder.load_issue(path, self.source)
        path.write_text(example_issue())
        issue = builder.load_issue(path, self.source)
        self.assertEqual(issue.generated_at.isoformat(), "2026-09-07T00:00:00+08:00")

    def test_unsafe_link_does_not_count_as_evidence(self):
        path = self.write_issue(source=False, extra='[危险链接](javascript:alert%281%29)')
        with self.assertRaisesRegex(builder.ValidationError, "证据链接"):
            builder.load_issue(path, self.source)

    def test_failed_swap_restores_previous_site(self):
        self.write_issue()
        builder.build_site(self.source, self.output)
        old = (self.output / "index.html").read_bytes()
        replace = builder.os.replace
        def fail_when_installing(source, destination):
            if Path(source).name.startswith(".docs-build-"):
                raise OSError("simulated installation failure")
            return replace(source, destination)
        with mock.patch.object(builder.os, "replace", side_effect=fail_when_installing):
            with self.assertRaises(OSError):
                builder.build_site(self.source, self.output)
        self.assertEqual((self.output / "index.html").read_bytes(), old)
        self.assertEqual(list(self.root.glob(".docs-*")), [])


if __name__ == "__main__":
    unittest.main()
