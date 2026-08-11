#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
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

    def test_analysis_requires_scores_and_fact_urls(self) -> None:
        expected = {"org/a"}
        valid = {
            "projects": [
                {
                    "full_name": "org/a",
                    "technology_score": 70,
                    "community_score": 60,
                    "commercial_score": 80,
                    "confidence": "medium",
                    "facts": [{"claim": "事实", "source_url": "https://github.com/org/a"}],
                }
            ]
        }
        mapped = scout.validate_analysis(valid, expected)
        self.assertEqual(mapped["org/a"]["technology_score"], 70.0)
        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["projects"][0]["facts"][0]["source_url"] = "not-a-url"
        with self.assertRaises(scout.ScoutError):
            scout.validate_analysis(invalid, expected)

    def test_finalize_computes_recommendation_and_report(self) -> None:
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
                "executive_summary": ["摘要"],
                "topic_outlook": "谨慎乐观",
                "projects": [
                    {
                        "full_name": "org/a",
                        "technology_score": 80,
                        "community_score": 70,
                        "commercial_score": 90,
                        "positioning": "测试项目",
                        "why_hot": ["活跃"],
                        "suitable_for": ["开发者"],
                        "secondary_development": ["集成"],
                        "business_models": ["托管服务"],
                        "risks": ["样本少"],
                        "facts": [{"claim": "仓库存在", "source_url": "https://github.com/org/a"}],
                        "inferences": ["可扩展"],
                        "speculations": [],
                        "confidence": "medium",
                    }
                ],
            }
            scout.atomic_write_json(run_dir / "result.json", result)
            scout.atomic_write_json(run_dir / "analysis.json", analysis)
            code = scout.finalize_command(argparse.Namespace(run_dir=str(run_dir), analysis_file=str(run_dir / "analysis.json")))
            self.assertEqual(code, 0)
            final = scout.load_json(run_dir / "result.json")
            expected_score = scout.round1(0.30 * item["scores"]["heat"] + 0.25 * 80 + 0.20 * 70 + 0.25 * 90)
            self.assertEqual(final["projects"][0]["scores"]["recommendation"], expected_score)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("事实热度榜", report)
            self.assertIn("首次运行代理热度", report)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
