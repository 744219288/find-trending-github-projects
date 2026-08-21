#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_trend_scout as scout  # noqa: E402


NOW = dt.datetime(2026, 8, 10, 8, 0, tzinfo=dt.timezone.utc)


def project(name: str, stars: int = 100, forks: int = 10, created_days: int = 100, pushed_days: int = 2) -> dict:
    owner, repo_name = name.split("/", 1)
    return {
        "repo": {
            "id": abs(hash(name)),
            "full_name": name,
            "name": repo_name,
            "owner": owner,
            "url": f"https://github.com/{name}",
            "description": "AI agent framework for useful automation",
            "topics": ["ai-agent", "automation"],
            "primary_language": "Python",
            "created_at": scout.iso_z(NOW - dt.timedelta(days=created_days)),
            "updated_at": scout.iso_z(NOW - dt.timedelta(days=pushed_days)),
            "pushed_at": scout.iso_z(NOW - dt.timedelta(days=pushed_days)),
            "stars": stars,
            "forks": forks,
            "subscribers": 4,
            "open_issues": 6,
            "archived": False,
            "disabled": False,
            "is_fork": False,
            "is_mirror": False,
            "upstream": None,
            "license": {"spdx_id": "MIT", "name": "MIT License"},
            "homepage": None,
            "size_kb": 200,
            "default_branch": "main",
        },
        "candidate_pools": ["relevance"],
        "matched_terms": [{"term": "AI Agent", "weight": 1.0, "query": "test"}],
        "relevance_score": 90.0,
        "recent_activity": {"window_days": 30, "issues_updated": 3, "pull_requests_updated": 2, "sample_capped": False},
        "latest_release": {"tag": "v1", "published_at": scout.iso_z(NOW - dt.timedelta(days=15)), "url": f"https://github.com/{name}/releases/tag/v1"},
        "readme": {"source_url": f"https://github.com/{name}#readme", "content_excerpt": "README"},
        "trend": {},
        "scores": {},
        "analysis": None,
        "flags": [],
    }


class ScoutUnitTests(unittest.TestCase):
    def test_terms_are_deduplicated_and_bounded(self) -> None:
        terms = scout.normalize_terms("AI Agent", ["ai agent", "autonomous agent", "LLM agent", "agentic ai", "ignored"])
        self.assertEqual(terms, ["AI Agent", "autonomous agent", "LLM agent", "agentic ai"])

    def test_query_plan_has_four_pools_and_preserves_original_weight(self) -> None:
        plan = scout.build_query_plan("AI Agent", ["AI Agent", "agentic ai"], days=7, language=None, now=NOW)
        self.assertEqual({item["pool"] for item in plan}, set(scout.POOL_QUOTAS))
        self.assertEqual(len(plan), 8)
        self.assertTrue(all("in:name,description,topics" in item["query"] for item in plan))
        self.assertTrue(all("readme" not in item["query"] for item in plan))
        originals = [item for item in plan if item["term"] == "AI Agent"]
        self.assertTrue(all(item["term_weight"] == 1.0 for item in originals))

    def test_relevance_rejects_incidental_query_match(self) -> None:
        item = project("org/public-apis", stars=10000)
        item["repo"]["description"] = "A collective list of public APIs"
        item["repo"]["topics"] = ["api", "resources"]
        item["matched_terms"] = [{"term": "AI Agent", "weight": 1.0, "query": "legacy-readme-match"}]
        self.assertLess(scout.relevance_score(item, "AI Agent"), 40)

    def test_dynamic_threshold_and_filters(self) -> None:
        good = project("org/good", stars=25)
        narrow = project("org/narrow", stars=8)
        archived = project("org/archived", stars=500)
        archived["repo"]["archived"] = True
        awesome = project("org/awesome-ai", stars=900)
        awesome["repo"]["name"] = "awesome-ai"
        kept, rejected, threshold, low_sample = scout.filter_candidates([good, narrow, archived, awesome], count=5)
        self.assertEqual(threshold, 5)
        self.assertTrue(low_sample)
        self.assertEqual({p["repo"]["full_name"] for p in kept}, {"org/good", "org/narrow"})
        self.assertIn("archived", {item["reason"] for item in rejected})
        self.assertIn("list_only", {item["reason"] for item in rejected})

    def test_percentiles_handle_ties(self) -> None:
        scores = scout.percentile_scores([1, 1, 3])
        self.assertEqual(scores[0], scores[1])
        self.assertEqual(scores[2], 100.0)

    def test_cold_start_never_populates_real_delta(self) -> None:
        projects = [project("org/a", 100, 10, 100), project("org/b", 80, 12, 20)]
        mode = scout.score_projects(projects, baseline=None, now=NOW, days=7)
        self.assertEqual(mode, "cold_start_proxy")
        for item in projects:
            self.assertIsNone(item["trend"]["stars_delta"])
            self.assertIsNone(item["trend"]["stars_growth_rate"])
            self.assertIn("age_normalized_star_proxy", item["scores"]["breakdown"])

    def test_snapshot_mode_uses_real_interval(self) -> None:
        projects = [project("org/a", 130, 12), project("org/b", 90, 8)]
        baseline = {
            "captured_at": scout.iso_z(NOW - dt.timedelta(days=6, hours=12)),
            "is_valid_snapshot": True,
            "projects": [
                {"repo": {"full_name": "org/a", "stars": 100, "forks": 10}},
                {"repo": {"full_name": "org/b", "stars": 80, "forks": 8}},
            ],
        }
        mode = scout.score_projects(projects, baseline=baseline, now=NOW, days=7)
        self.assertEqual(mode, "snapshot_delta")
        self.assertEqual(projects[0]["trend"]["stars_delta"], 30)
        self.assertEqual(projects[0]["trend"]["window_actual_days"], 6.5)

    def test_credibility_flags_do_not_exclude(self) -> None:
        item = project("org/suspicious", stars=1000, forks=1)
        item["repo"]["open_issues"] = 0
        flags = scout.score_star_credibility(item)
        self.assertIn("needs_verification:low_fork_and_issue_signal", flags)

    def test_analysis_requires_one_liner_details_and_fact_urls(self) -> None:
        expected = {"org/a"}
        valid = {
            "projects": [
                {
                    "full_name": "org/a",
                    "one_liner": "让 AI 像人一样操作网页。",
                    "details": {"explain": "说明", "suitable": "开发者", "cautions": "偶尔点错", "business": "封装 RPA 服务"},
                    "facts": [{"claim": "事实", "source_url": "https://github.com/org/a"}],
                }
            ]
        }
        mapped = scout.validate_analysis(valid, expected)
        self.assertEqual(mapped["org/a"]["one_liner"], "让 AI 像人一样操作网页。")
        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["projects"][0]["facts"][0]["source_url"] = "not-a-url"
        with self.assertRaises(scout.ScoutError):
            scout.validate_analysis(invalid, expected)
        no_liner = json.loads(json.dumps(valid, ensure_ascii=False))
        no_liner["projects"][0]["one_liner"] = ""
        with self.assertRaises(scout.ScoutError):
            scout.validate_analysis(no_liner, expected)
        empty_detail = json.loads(json.dumps(valid, ensure_ascii=False))
        empty_detail["projects"][0]["details"]["business"] = " "
        with self.assertRaises(scout.ScoutError):
            scout.validate_analysis(empty_detail, expected)

    def test_analysis_reports_all_problems_at_once(self) -> None:
        expected = {"org/a", "org/b"}
        analysis = {
            "projects": [
                {
                    "full_name": "org/a",
                    "one_liner": "长" * 90,
                    "details": {"explain": "说明", "suitable": "开发者", "cautions": "", "business": "封装服务"},
                    "facts": [{"claim": "事实", "source_url": "not-a-url"}],
                },
                {"full_name": "org/unknown", "one_liner": "x", "details": {"explain": "x", "suitable": "x", "cautions": "x", "business": "x"}},
            ]
        }
        with self.assertRaises(scout.ScoutError) as ctx:
            scout.validate_analysis(analysis, expected)
        message = str(ctx.exception)
        # 一次性列出全部问题：one_liner 超长、空板块、facts 无效、未知项目、缺少 org/b
        self.assertIn("90 字", message)
        self.assertIn("cautions", message)
        self.assertIn("第 1 项无效", message)
        self.assertIn("org/unknown", message)
        self.assertIn("org/b", message)
        self.assertIn("共 5 处问题", message)

    def test_prune_runs_keeps_newest_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs_dir = Path(temp) / "runs"
            for i in range(12):
                (runs_dir / f"2026010{i:02d}T000000Z").mkdir(parents=True)
            removed = scout.prune_runs(runs_dir, keep=10)
            self.assertEqual(len(removed), 2)
            remaining = sorted(d.name for d in runs_dir.iterdir())
            self.assertEqual(len(remaining), 10)
            self.assertNotIn(removed[0], remaining)
            # keep 至少为 1
            removed_again = scout.prune_runs(runs_dir, keep=0)
            self.assertEqual(len(remaining) - len(removed_again), 1)
            self.assertEqual(len(list(runs_dir.iterdir())), 1)

    def test_latest_run_reports_freshness_and_skips_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ai-agent"
            runs = root / "runs"
            real_now = dt.datetime.now(dt.timezone.utc)

            def write_run(name: str, completed: dt.datetime, mode: str = "cold_start_proxy") -> None:
                run_dir = runs / name
                run_dir.mkdir(parents=True)
                scout.atomic_write_json(run_dir / "result.json", {
                    "run": {"completed_at": scout.iso_z(completed), "mode": mode},
                    "projects": [{"repo": {"full_name": "org/a"}}],
                })

            ns = argparse.Namespace(keyword="AI Agent", max_age_minutes=120, output_root=str(temp))

            # 没有任何 run：退出码 1
            self.assertEqual(scout.latest_run_command(ns), 1)

            # 只有降级 run（stale_snapshot_fallback）：跳过，视为无可用 run
            write_run("20200101T000000Z", real_now - dt.timedelta(minutes=10), mode="stale_snapshot_fallback")
            self.assertEqual(scout.latest_run_command(ns), 1)

            # 存在新鲜 run：退出码 0
            write_run("20260820T000000Z", real_now - dt.timedelta(minutes=30))
            self.assertEqual(scout.latest_run_command(ns), 0)

            # 新鲜 run 消失后只剩过期 run（降级 run 更旧且仍被跳过）：退出码 1
            shutil.rmtree(runs / "20260820T000000Z")
            write_run("20260101T000000Z", real_now - dt.timedelta(days=30))
            self.assertEqual(scout.latest_run_command(ns), 1)

    def test_finalize_default_emits_html_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            item = project("org/a")
            scout.score_projects([item], baseline=None, now=NOW, days=7)
            result = {
                "schema_version": scout.SCHEMA_VERSION,
                "run": {"mode": "cold_start_proxy", "data_as_of": scout.iso_z(NOW)},
                "input": {"keyword": "AI Agent", "time_range_days": 7},
                "queries": [],
                "collection": {"candidate_count": 1, "dynamic_star_threshold": 20},
                "rankings": {"heat": ["org/a"], "recommendation": []},
                "projects": [item],
                "rejected_candidates": [],
                "limitations": ["测试限制"],
                "errors": [],
            }
            analysis = {
                "projects": [
                    {
                        "full_name": "org/a",
                        "one_liner": "让 AI 像人一样操作网页。",
                        "details": {"explain": "把浏览器变成 AI 的手。", "suitable": "自动化开发者。", "cautions": "重要操作加确认。", "business": "封装 RPA 按次收费。"},
                        "facts": [{"claim": "仓库存在", "source_url": "https://github.com/org/a"}],
                    }
                ],
            }
            scout.atomic_write_json(run_dir / "result.json", result)
            scout.atomic_write_json(run_dir / "analysis.json", analysis)
            code = scout.finalize_command(argparse.Namespace(run_dir=str(run_dir), analysis_file=str(run_dir / "analysis.json")))
            self.assertEqual(code, 0)
            final = scout.load_json(run_dir / "result.json")
            self.assertEqual(final["rankings"]["recommendation"], ["org/a"])
            self.assertEqual(final["projects"][0]["analysis"]["one_liner"], "让 AI 像人一样操作网页。")
            report_html = (run_dir / "report.html").read_text(encoding="utf-8")
            self.assertIn("让 AI 像人一样操作网页。", report_html)
            self.assertIn("https://github.com/org/a", report_html)
            self.assertIn("data-count", report_html)
            # 冷启动模式：报告顶部必须出现首跑提示条
            self.assertIn("首次运行，还没有历史快照", report_html)
            self.assertIn("class=\"notice\"", report_html)
            # 默认只出卡片 HTML：report.md / rankings.csv 不应生成
            self.assertFalse((run_dir / "report.md").exists())
            self.assertFalse((run_dir / "rankings.csv").exists())

    def test_finalize_keep_extra_emits_md_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            item = project("org/a")
            scout.score_projects([item], baseline=None, now=NOW, days=7)
            result = {
                "schema_version": scout.SCHEMA_VERSION,
                "run": {"mode": "cold_start_proxy", "data_as_of": scout.iso_z(NOW)},
                "input": {"keyword": "AI Agent", "time_range_days": 7},
                "queries": [],
                "collection": {"candidate_count": 1, "dynamic_star_threshold": 20},
                "rankings": {"heat": ["org/a"], "recommendation": []},
                "projects": [item],
                "rejected_candidates": [],
                "limitations": ["测试限制"],
                "errors": [],
            }
            analysis = {
                "projects": [
                    {
                        "full_name": "org/a",
                        "one_liner": "让 AI 像人一样操作网页。",
                        "details": {"explain": "把浏览器变成 AI 的手。", "suitable": "自动化开发者。", "cautions": "重要操作加确认。", "business": "封装 RPA 按次收费。"},
                        "facts": [{"claim": "仓库存在", "source_url": "https://github.com/org/a"}],
                    }
                ],
            }
            scout.atomic_write_json(run_dir / "result.json", result)
            scout.atomic_write_json(run_dir / "analysis.json", analysis)
            code = scout.finalize_command(argparse.Namespace(run_dir=str(run_dir), analysis_file=str(run_dir / "analysis.json"), keep_extra=True))
            self.assertEqual(code, 0)
            self.assertTrue((run_dir / "report.html").exists())
            report_md = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("二次开发 / 商业化", report_md)
            csv_text = (run_dir / "rankings.csv").read_text(encoding="utf-8-sig")
            self.assertIn("watchers", csv_text)
            self.assertIn("one_liner", csv_text)

    def test_renderer_escapes_html_and_hides_missing_badges(self) -> None:
        from render_card_report import render_card_report

        item = project("org/a")
        item["repo"]["subscribers"] = None
        item["repo"]["primary_language"] = None
        item["repo"]["license"] = {"spdx_id": "NOASSERTION", "name": None}
        item["repo"]["description"] = "<script>alert(1)</script>"
        item["analysis"] = {
            "one_liner": "一句话<b>加粗</b>介绍",
            "details": {"explain": "说明", "suitable": "开发者", "cautions": "注意", "business": "商业化"},
        }
        result = {
            "input": {"keyword": "AI Agent"},
            "rankings": {"recommendation": ["org/a"]},
            "projects": [item],
        }
        page = render_card_report(result)
        self.assertNotIn("一句话<b>加粗</b>介绍", page)
        self.assertIn("一句话&lt;b&gt;加粗&lt;/b&gt;介绍", page)
        self.assertNotIn("badge license", page)
        self.assertNotIn("badge lang", page)

    def test_analysis_template_carries_facts_format_hint(self) -> None:
        template = scout.analysis_template([project("org/a")])
        hint = template.get("_填写说明（填完后请删除本键）")
        self.assertIsInstance(hint, dict)
        example = hint.get("facts_示例")
        self.assertTrue(example and example[0].get("claim") and str(example[0].get("source_url")).startswith("https://"))
        self.assertIn("source_url", hint.get("facts", ""))

    def test_string_facts_rejected_with_conversion_example(self) -> None:
        expected = {"org/a"}
        analysis = {
            "projects": [
                {
                    "full_name": "org/a",
                    "one_liner": "一句话。",
                    "details": {"explain": "x", "suitable": "x", "cautions": "x", "business": "x"},
                    "facts": ["该项目月均增 200 星: https://github.com/org/a"],
                }
            ]
        }
        with self.assertRaises(scout.ScoutError) as ctx:
            scout.validate_analysis(analysis, expected)
        message = str(ctx.exception)
        self.assertIn("字符串", message)
        self.assertIn("claim", message)
        self.assertIn("source_url", message)

    def test_offtopic_screenshot_tool_flagged_kept_then_strict_rejected(self) -> None:
        sharex = project("ShareX/ShareX", stars=30000)
        sharex["repo"]["description"] = "Screen capture, file sharing and productivity tool"
        sharex["repo"]["topics"] = ["screenshot", "screen-capture"]
        sharex["matched_terms"] = [{"term": "AI image generation", "weight": 1.0, "query": "stemmed-overmatch"}]
        sharex["relevance_score"] = 90.0  # 模拟 GitHub 词干化搜索放进来的高相关分
        comfyui = project("comfyanonymous/ComfyUI", stars=8000)
        comfyui["repo"]["description"] = "Stable diffusion GUI with graph nodes"
        comfyui["matched_terms"] = [{"term": "Stable Diffusion", "weight": 0.8, "query": "q"}]
        comfyui["relevance_score"] = 90.0

        self.assertLess(scout.token_coverage(sharex), 0.5)
        self.assertGreaterEqual(scout.token_coverage(comfyui), 0.5)

        kept, rejected, _, _ = scout.filter_candidates([sharex, comfyui], count=5)
        names = {p["repo"]["full_name"] for p in kept}
        self.assertIn("comfyanonymous/ComfyUI", names)
        self.assertIn("ShareX/ShareX", names)  # 默认保留
        sharex_kept = next(p for p in kept if p["repo"]["full_name"] == "ShareX/ShareX")
        self.assertIn("needs_verification:possible_offtopic", sharex_kept["flags"])

        strict_kept, strict_rejected, _, _ = scout.filter_candidates([sharex, comfyui], count=5, strict_relevance=True)
        self.assertEqual({p["repo"]["full_name"] for p in strict_kept}, {"comfyanonymous/ComfyUI"})
        self.assertIn("offtopic", {item["reason"] for item in strict_rejected})

    def test_apply_excludes_drops_matching_candidates(self) -> None:
        a = project("org/a")
        b = project("ShareX/ShareX")
        b["repo"]["description"] = "Screenshot capture tool"
        kept, rejected = scout.apply_excludes([a, b], ["screenshot", "sharex"])
        self.assertEqual({p["repo"]["full_name"] for p in kept}, {"org/a"})
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "excluded_by_user:screenshot")
        self.assertEqual(rejected[0]["stage"], "exclude")
        # 空排除词 = 原样返回
        same, none_rejected = scout.apply_excludes([a, b], [])
        self.assertEqual(len(same), 2)
        self.assertEqual(none_rejected, [])

    def test_detail_cache_ttl_and_anonymous_degraded_entries(self) -> None:
        from pathlib import Path
        import tempfile as _tmp

        with _tmp.TemporaryDirectory() as temp:
            cache_path = Path(temp) / "repo-details.json"
            scout._load_detail_cache(cache_path)
            now = scout.utc_now()
            full_entry = {"fetched_at": scout.iso_z(now - dt.timedelta(hours=2)), "repo_updates": {"subscribers": 9}, "latest_release": {"tag": "v2"}, "anonymous": False}
            anon_entry = {"fetched_at": scout.iso_z(now - dt.timedelta(hours=2)), "repo_updates": {"subscribers": 1}, "latest_release": None, "anonymous": True}
            stale_entry = {"fetched_at": scout.iso_z(now - dt.timedelta(hours=30)), "repo_updates": {"subscribers": 5}, "latest_release": None, "anonymous": False}
            scout._DETAIL_CACHE.update({"org/full": full_entry, "org/anon": anon_entry, "org/stale": stale_entry})

            hit = scout._cached_detail("org/full", now)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["repo_updates"]["subscribers"], 9)
            # 认证模式：匿名写入的降级缓存视为未命中
            self.assertIsNone(scout._cached_detail("org/anon", now, allow_degraded=False))
            # 匿名模式：可用
            self.assertIsNotNone(scout._cached_detail("org/anon", now, allow_degraded=True))
            # 超过 TTL：未命中
            self.assertIsNone(scout._cached_detail("org/stale", now))
            # 保存后重新加载仍在
            scout._save_detail_cache()
            scout._load_detail_cache(cache_path)
            self.assertIsNotNone(scout._cached_detail("org/full", now))

    def test_history_compaction_is_dry_run_by_default_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshots = root / "ai-agent" / "snapshots"
            old_time = NOW - dt.timedelta(days=500)
            payload = {
                "schema_version": "1.0",
                "captured_at": scout.iso_z(old_time),
                "is_valid_snapshot": True,
                "input": {"keyword": "AI Agent"},
                "projects": [{"repo": {"full_name": "org/a", "stars": 10, "forks": 1}}],
            }
            scout.atomic_write_json(snapshots / "old.json", payload)
            original_now = scout.utc_now
            scout.utc_now = lambda: NOW
            try:
                args = argparse.Namespace(output_root=str(root), keyword="AI Agent", retention_days=365, apply=False)
                self.assertEqual(scout.compact_history(args), 0)
                self.assertTrue((snapshots / "old.json").exists())
                args.apply = True
                self.assertEqual(scout.compact_history(args), 0)
                self.assertFalse((snapshots / "old.json").exists())
                self.assertTrue((root / "ai-agent" / "monthly" / "2025-03.json").exists())
            finally:
                scout.utc_now = original_now

    def test_watch_diff_reports_new_dropped_star_moves_and_release_change(self) -> None:
        previous = {
            "projects": [
                {"repo": {"full_name": "org/kept", "stars": 100, "archived": False}, "latest_release": {"tag": "v1.0.0"}},
                {"repo": {"full_name": "org/dropped", "stars": 50, "archived": False}, "latest_release": {"tag": "v2.0.0"}},
                {"repo": {"full_name": "org/archived-now", "stars": 30, "archived": False}},
            ]
        }
        current = {
            "projects": [
                {"repo": {"full_name": "org/kept", "stars": 120, "archived": False}, "latest_release": {"tag": "v1.1.0"}},
                {"repo": {"full_name": "org/newcomer", "stars": 80, "archived": False}},
                {"repo": {"full_name": "org/archived-now", "stars": 30, "archived": True}},
            ]
        }
        diff = scout.watch_diff(previous, current)
        self.assertEqual(diff["new_entries"], ["org/newcomer"])
        self.assertEqual(diff["dropped"], ["org/dropped"])
        self.assertEqual(len(diff["star_moves"]), 1)
        self.assertEqual(diff["star_moves"][0], {"full_name": "org/kept", "from": 100, "to": 120, "delta": 20})
        changes = {(c["full_name"], c["change"]) for c in diff["status_changes"]}
        self.assertIn(("org/kept", "new_release"), changes)
        self.assertIn(("org/archived-now", "archived"), changes)

    def test_watch_diff_empty_when_nothing_changed(self) -> None:
        payload = {"projects": [{"repo": {"full_name": "org/a", "stars": 10, "archived": False}, "latest_release": {"tag": "v1"}}]}
        diff = scout.watch_diff(payload, json.loads(json.dumps(payload)))
        self.assertEqual(diff, {"new_entries": [], "dropped": [], "star_moves": [], "status_changes": []})

    def test_watch_reports_zero_project_run_instead_of_error(self) -> None:
        # 本期 0 项目（如 --fresh 过滤清空）时：明确提示而非误导性的"找不到新 run 目录"
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "kw" / "runs"
            old_run = runs_dir / "20260801T000000Z"
            new_run = runs_dir / "20260802T000000Z"
            old_run.mkdir(parents=True)
            new_run.mkdir(parents=True)
            base_project = project("org/a")
            old_result = {"run": {"mode": "cold_start_proxy", "completed_at": "2026-08-01T00:00:00Z"}, "input": {"keyword": "kw", "fresh_days": None}, "projects": [base_project]}
            new_result = {"run": {"mode": "cold_start_proxy", "completed_at": "2026-08-02T00:00:00Z"}, "input": {"keyword": "kw", "fresh_days": 7}, "projects": []}
            scout.atomic_write_json(old_run / "result.json", old_result)
            scout.atomic_write_json(new_run / "result.json", new_result)
            args = argparse.Namespace(keyword="kw", output_root=tmp, days=7, count=10, transport="api")
            with unittest.mock.patch.object(scout, "collect_command", return_value=0):
                rc = scout.watch_command(args)
            self.assertEqual(rc, 0)
            summary = (new_run / "watch-summary.md").read_text(encoding="utf-8")
            self.assertIn("掉榜", summary)
            self.assertIn("org/a", summary)

    def test_trending_query_windows_languages_and_star_gates(self) -> None:
        now = dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc)
        daily = scout.trending_query("daily", language=None, now=now)
        self.assertIn("stars:>=10", daily)
        self.assertIn("pushed:>=2026-08-09", daily)
        self.assertIn("archived:false", daily)
        monthly = scout.trending_query("monthly", language="Python", now=now)
        self.assertIn("stars:>=200", monthly)
        self.assertIn("pushed:>=2026-07-11", monthly)
        self.assertIn('language:"Python"', monthly)


class ScoutFreshnessTests(unittest.TestCase):
    def test_fresh_days_keeps_new_and_rejects_old(self) -> None:
        new = project("org/fresh", stars=200, created_days=3)
        old = project("org/old", stars=99999, created_days=400)
        kept, rejected, _, _ = scout.filter_candidates([new, old], count=5, fresh_days=30)
        self.assertEqual({p["repo"]["full_name"] for p in kept}, {"org/fresh"})
        self.assertEqual(rejected[0]["reason"], "too_old_for_fresh")

    def test_fresh_days_none_keeps_old_established(self) -> None:
        old = project("org/old", stars=99999, created_days=400)
        kept, _, _, _ = scout.filter_candidates([old], count=5)
        self.assertEqual({p["repo"]["full_name"] for p in kept}, {"org/old"})

    def test_fresh_days_relaxes_star_threshold_for_new_repos(self) -> None:
        # 新发布项目短期攒不到 20 星：fresh 模式星数门槛 20/5 降至 5/1
        tiny_new = project("org/tinynew", stars=3, created_days=5)
        kept, rejected, threshold, _ = scout.filter_candidates([tiny_new], count=5, fresh_days=30)
        self.assertEqual({p["repo"]["full_name"] for p in kept}, {"org/tinynew"})
        self.assertEqual(threshold, 1)
        # 非 fresh 模式：同样 3 星项目会被 20/5 门槛挡掉
        kept2, rejected2, threshold2, _ = scout.filter_candidates([tiny_new], count=5)
        self.assertEqual(kept2, [])
        self.assertEqual(rejected2[-1]["reason"], "below_dynamic_threshold")
        self.assertEqual(threshold2, 5)

    def test_query_plan_injects_fresh_cutoff_into_all_pools(self) -> None:
        plan = scout.build_query_plan("AI Agent", ["AI Agent"], days=7, language=None, now=NOW, fresh_days=7)
        by_pool = {item["pool"]: item["query"] for item in plan}
        fresh_cutoff = (NOW - dt.timedelta(days=7)).date().isoformat()
        for pool in scout.POOL_QUOTAS:
            self.assertIn(f"created:>={fresh_cutoff}", by_pool[pool], f"{pool} 池应注入 created:>={fresh_cutoff}")
        # popular 池的星数门槛在 fresh 模式放宽为 5
        self.assertIn("stars:>=5", by_pool["popular"])
        self.assertNotIn("stars:>=20", by_pool["popular"])
        # 非 fresh 模式：只有 new 池带 created 约束，popular 池保持 stars:>=20
        plan_normal = scout.build_query_plan("AI Agent", ["AI Agent"], days=7, language=None, now=NOW)
        by_pool_normal = {item["pool"]: item["query"] for item in plan_normal}
        self.assertNotIn(f"created:>={fresh_cutoff}", by_pool_normal["relevance"])
        self.assertIn("stars:>=20", by_pool_normal["popular"])


class ScoutFreshBiasTests(unittest.TestCase):
    """方案A「默认偏新」软偏置：趋势类请求默认对近 30 天新项目加权，老牌高星不再稳赢。"""

    def test_fresh_bias_adds_recency_boost_and_lifts_new_repo(self) -> None:
        import copy

        established = project("org/established", stars=5000, forks=500, created_days=270)
        just_released = project("org/just-released", stars=120, forks=15, created_days=5)
        biased = [copy.deepcopy(established), copy.deepcopy(just_released)]
        unbiased = [copy.deepcopy(established), copy.deepcopy(just_released)]
        scout.score_projects(biased, baseline=None, now=NOW, days=7, fresh_bias=True)
        scout.score_projects(unbiased, baseline=None, now=NOW, days=7, fresh_bias=False)
        biased_map = {p["repo"]["full_name"]: p["scores"] for p in biased}
        unbiased_map = {p["repo"]["full_name"]: p["scores"] for p in unbiased}
        # 偏置开启时 breakdown 记录 recency_boost，关闭时不记录
        self.assertIn("recency_boost", biased_map["org/just-released"]["breakdown"])
        self.assertNotIn("recency_boost", unbiased_map["org/just-released"]["breakdown"])
        self.assertGreater(biased_map["org/just-released"]["breakdown"]["recency_boost"], 80)
        self.assertLess(biased_map["org/established"]["breakdown"]["recency_boost"], 1)
        # 新项目热度被抬升、老项目热度被压低，且新项目实现反超
        self.assertGreater(biased_map["org/just-released"]["heat"], unbiased_map["org/just-released"]["heat"])
        self.assertLess(biased_map["org/established"]["heat"], unbiased_map["org/established"]["heat"])
        self.assertGreater(biased_map["org/just-released"]["heat"], biased_map["org/established"]["heat"])

    def test_no_fresh_bias_flag_parses_for_collect_and_watch(self) -> None:
        parser = scout.build_parser()
        self.assertFalse(parser.parse_args(["collect", "--keyword", "ai agent"]).no_fresh_bias)
        self.assertTrue(parser.parse_args(["collect", "--keyword", "ai agent", "--no-fresh-bias"]).no_fresh_bias)
        self.assertTrue(parser.parse_args(["watch", "--keyword", "ai agent", "--no-fresh-bias"]).no_fresh_bias)


class ScoutTrendingScrapeTests(unittest.TestCase):
    SAMPLE_HTML = """
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/openclaw/openclaw" data-ga-click="Repository">openclaw / <span class="text-normal">openclaw</span></a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">A popular AI coding agent.</p>
      <span class="d-inline-block mr-3">
        <a href="/openclaw/openclaw/stargazers" data-ga-click="Star"><svg aria-label="star" viewBox="0 0 16 16" width="16" height="16"><path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815"></path></svg>
          12,345</a>
      </span>
      <span class="d-inline-block mr-3">
        <a href="/openclaw/openclaw/forks" data-ga-click="Fork"><svg aria-label="fork" viewBox="0 0 16 16" width="16" height="16"></svg>
          678</a>
      </span>
      <span itemprop="programmingLanguage">TypeScript</span>
      <span class="d-inline-block float-sm-right">1,234 stars today</span>
    </article>
    <article class="Box-row">
      <h2 class="h3 lh-condensed">
        <a href="/acme/newtool" >acme / <span class="text-normal">newtool</span></a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">A brand new dev tool.</p>
      <span class="d-inline-block mr-3"><a href="/acme/newtool/stargazers"><svg aria-label="star" viewBox="0 0 16 16"></svg>432</a></span>
      <span class="d-inline-block mr-3"><a href="/acme/newtool/forks"><svg aria-label="fork" viewBox="0 0 16 16"></svg>12</a></span>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    """

    def test_scrape_parses_repos_from_html(self) -> None:
        # 用 monkeypatch 替换 urlopen 返回值，验证内部解析
        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._data

        resp = _Resp()
        resp._data = self.SAMPLE_HTML.encode("utf-8")
        with unittest.mock.patch.object(urllib.request, "urlopen", return_value=resp):
            repos = scout.scrape_github_trending("daily", language=None)
        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["full_name"], "openclaw/openclaw")
        self.assertEqual(repos[0]["stars"], 12345)
        self.assertEqual(repos[0]["forks"], 678)
        self.assertEqual(repos[0]["primary_language"], "TypeScript")
        self.assertEqual(repos[0]["stars_period"], 1234)
        self.assertEqual(repos[0]["stars_period_label"], "today")
        # 描述必须来自 <p class="col-9 ...">，不能误抓 SVG 的 <path> 标签内容
        self.assertEqual(repos[0]["description"], "A popular AI coding agent.")
        self.assertEqual(repos[1]["full_name"], "acme/newtool")
        self.assertIsNone(repos[1]["stars_period"])

    def test_scrape_uses_programming_language_param(self) -> None:
        # --language 是编程语言维度，必须映射到 ?language= 而非 ?spoken_language_code=（自然语言维度）
        import urllib.request

        captured: dict[str, str] = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._data

        resp = _Resp()
        resp._data = self.SAMPLE_HTML.encode("utf-8")

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return resp

        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            scout.scrape_github_trending("daily", language="python")
        self.assertIn("language=python", captured["url"])
        self.assertNotIn("spoken_language_code", captured["url"])
        self.assertIn("since=daily", captured["url"])


class ScoutTrendingCommandTests(unittest.TestCase):
    def _args(self, tmp: str) -> argparse.Namespace:
        return argparse.Namespace(window="daily", count=5, language=None, transport="api", output_root=tmp)

    def test_trending_command_builds_projects_and_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repos = [
                {"full_name": "openclaw/openclaw", "url": "https://github.com/openclaw/openclaw", "description": "d", "primary_language": "TS", "stars": 12345, "forks": 678, "stars_period": 1234, "stars_period_label": "today"},
                {"full_name": "acme/newtool", "url": "https://github.com/acme/newtool", "description": "d2", "primary_language": "Python", "stars": 432, "forks": 12, "stars_period": None, "stars_period_label": None},
            ]
            detail = {"created_at": scout.iso_z(NOW - dt.timedelta(days=20)), "pushed_at": scout.iso_z(NOW - dt.timedelta(days=1)), "license": {"spdx_id": "MIT", "name": "MIT"}, "subscribers": 1, "open_issues": 2}
            readme = {"source_url": "https://github.com/openclaw/openclaw#readme", "fetched_at": scout.iso_z(NOW), "content_excerpt": "# openclaw", "content_truncated": False, "excerpt_sha256": "x"}
            with unittest.mock.patch.object(scout, "scrape_github_trending", return_value=repos), \
                 unittest.mock.patch.object(scout, "_trending_detail", return_value=(detail, False)), \
                 unittest.mock.patch.object(scout, "_readme_excerpt_for", return_value=readme), \
                 unittest.mock.patch.object(scout, "_is_anonymous", return_value=False):
                rc = scout.trending_command(self._args(tmp))
            self.assertEqual(rc, 0)
            import glob
            run_dirs = glob.glob(os.path.join(tmp, "_trending", "daily", "*"))
            self.assertEqual(len(run_dirs), 1)
            result = json.loads(Path(run_dirs[0], "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["mode"], "trending")
            self.assertEqual(len(result["projects"]), 2)
            self.assertEqual(result["projects"][0]["trending_rank"], 1)
            self.assertEqual(result["projects"][0]["repo"]["full_name"], "openclaw/openclaw")
            self.assertEqual(result["projects"][0]["repo"]["created_at"], scout.iso_z(NOW - dt.timedelta(days=20)))
            # 认证模式：热榜也要抓 README 摘要（分析阶段的素材）
            self.assertEqual(result["projects"][0]["readme"]["content_excerpt"], "# openclaw")
            self.assertEqual(result["collection"]["detail_cache_hits"], 0)
            self.assertTrue(Path(run_dirs[0], "analysis-template.json").is_file())

    def test_trending_command_falls_back_when_scrape_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fallback = [
                {"full_name": "acme/newtool", "url": "https://github.com/acme/newtool", "description": "d", "primary_language": "Python", "stars": 432, "forks": 12, "stars_period": None, "stars_period_label": None},
            ]
            with unittest.mock.patch.object(scout, "scrape_github_trending", side_effect=scout.ScoutError("blocked")), \
                 unittest.mock.patch.object(scout, "_trending_fallback_repos", return_value=fallback), \
                 unittest.mock.patch.object(scout, "_trending_detail", return_value=({}, False)), \
                 unittest.mock.patch.object(scout, "_readme_excerpt_for", return_value=None), \
                 unittest.mock.patch.object(scout, "_is_anonymous", return_value=False):
                rc = scout.trending_command(self._args(tmp))
            self.assertEqual(rc, 0)
            import glob
            run_dirs = glob.glob(os.path.join(tmp, "_trending", "daily", "*"))
            result = json.loads(Path(run_dirs[0], "result.json").read_text(encoding="utf-8"))
            self.assertTrue(result["collection"]["degraded"])
            self.assertEqual(result["projects"][0]["trend"]["mode"], "trending_api_approx")

    def test_trending_detail_hits_cache_within_ttl(self) -> None:
        scout._DETAIL_CACHE.clear()
        try:
            now = scout.utc_now()
            calls: list[str] = []

            def fake_gh_json(endpoint: str, **kwargs):
                calls.append(endpoint)
                return {"created_at": "2026-01-01T00:00:00Z", "license": {"spdx_id": "MIT"}, "subscribers_count": 3, "open_issues_count": 4}

            with unittest.mock.patch.object(scout, "gh_json", side_effect=fake_gh_json):
                detail1, cached1 = scout._trending_detail("org/x", now=now)
                detail2, cached2 = scout._trending_detail("org/x", now=now)
            self.assertFalse(cached1)
            self.assertTrue(cached2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(detail1["created_at"], "2026-01-01T00:00:00Z")
            self.assertEqual(detail2["created_at"], "2026-01-01T00:00:00Z")
        finally:
            scout._DETAIL_CACHE.clear()


class ScoutTrendingRenderTests(unittest.TestCase):
    def _result(self, mode: str, created_days: int, stars_period=None, period_label=None) -> dict:
        return {
            "schema_version": "1.0",
            "run": {"mode": mode},
            "input": {"keyword": "AI Agent" if mode != "trending" else None, "window": "daily" if mode == "trending" else None},
            "rankings": {"recommendation": ["org/a"]},
            "projects": [
                {
                    "repo": {"full_name": "org/a", "url": "https://github.com/org/a", "description": "desc", "primary_language": "Python", "created_at": scout.iso_z(NOW - dt.timedelta(days=created_days)), "pushed_at": scout.iso_z(NOW - dt.timedelta(days=1)), "stars": 100, "forks": 10, "license": {"spdx_id": "MIT"}},
                    "trend": {"mode": "github_trending", "stars_period": stars_period, "stars_period_label": period_label},
                    "analysis": {"one_liner": "一句话", "details": {"explain": "e", "suitable": "s", "cautions": "c", "business": "b"}},
                }
            ],
        }

    def test_render_shows_age_badge_for_new_project(self) -> None:
        html = scout_render(self._result("trending", created_days=5, stars_period=123, period_label="today"))
        self.assertIn("🆕 新项目", html)
        self.assertIn("今日 +123 星", html)
        self.assertIn("热榜", html)

    def test_render_shows_age_badge_for_old_project(self) -> None:
        html = scout_render(self._result("collect", created_days=800))
        self.assertIn("已成立 2 年", html)
        self.assertIn("趋势雷达", html)

    def test_render_report_trending_without_keyword_does_not_crash(self) -> None:
        # 回归：热榜 input 无 keyword，finalize 调 render_report 曾 KeyError 崩溃。
        md = scout.render_report(self._result("trending", created_days=5), {})
        self.assertIn("GitHub 当日热榜", md)  # window 默认 daily
        self.assertNotIn("None", md)

    def test_render_report_collect_uses_keyword(self) -> None:
        md = scout.render_report(self._result("collect", created_days=800), {})
        self.assertIn("AI Agent", md)


def scout_render(result: dict) -> str:
    import importlib
    import render_card_report
    importlib.reload(render_card_report)
    return render_card_report.render_card_report(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)