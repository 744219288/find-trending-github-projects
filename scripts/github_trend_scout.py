#!/usr/bin/env python3
"""Collect, score, snapshot, and finalize keyword-driven GitHub trend reports.

Uses only the Python standard library and GitHub CLI. Repository content is read as
untrusted data and is never executed.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
DEFAULT_COUNT = 10
DEFAULT_DAYS = 7
DEFAULT_CANDIDATE_TARGET = 80
MAX_TERMS = 4
POOL_QUOTAS = {"relevance": 24, "new": 20, "active": 20, "popular": 16}
LIST_PATTERNS = (
    "awesome-",
    "awesome_",
    "curated-list",
    "resource-list",
    "learning-notes",
    "interview-notes",
)


class ScoutError(RuntimeError):
    pass


class GhError(ScoutError):
    def __init__(self, message: str, *, command: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def days_since(value: str | None, now: dt.datetime) -> float:
    parsed = parse_time(value)
    if not parsed:
        return 99999.0
    return max((now - parsed).total_seconds() / 86400.0, 0.0)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def round1(value: float) -> float:
    return round(float(value) + 1e-10, 1)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if slug:
        return slug[:64]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"keyword-{digest}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run_process(command: list[str], *, timeout: int = 60, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GhError("未找到 gh CLI。请先安装 GitHub CLI。", command=command) from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError("GitHub CLI 请求超时。", command=command, stderr=str(exc)) from exc
    if result.returncode != 0 and not allow_failure:
        raise GhError("GitHub CLI 请求失败。", command=command, stderr=result.stderr.strip())
    return result


def check_gh_auth() -> None:
    if shutil.which("gh") is None:
        raise GhError("未找到 gh CLI。请先安装 GitHub CLI。")
    result = run_process(["gh", "auth", "status"], timeout=20, allow_failure=True)
    if result.returncode != 0:
        raise GhError("gh 尚未认证。请在本机运行 gh auth login。", command=["gh", "auth", "status"], stderr=result.stderr.strip())


def gh_json(endpoint: str, *, fields: dict[str, Any] | None = None, timeout: int = 60, allow_404: bool = False) -> Any:
    command = ["gh", "api", "--method", "GET", endpoint, "-H", "Accept: application/vnd.github+json"]
    for key, value in (fields or {}).items():
        command.extend(["-f", f"{key}={value}"])
    result = run_process(command, timeout=timeout, allow_failure=allow_404)
    if allow_404 and result.returncode != 0:
        stderr = result.stderr.lower()
        if "404" in stderr or "not found" in stderr:
            return None
        raise GhError("GitHub CLI 请求失败。", command=command, stderr=result.stderr.strip())
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhError("GitHub API 返回的不是有效 JSON。", command=command, stderr=result.stdout[:500]) from exc


def gh_raw(endpoint: str, *, timeout: int = 60, allow_404: bool = False) -> str | None:
    command = ["gh", "api", "--method", "GET", endpoint, "-H", "Accept: application/vnd.github.raw+json"]
    result = run_process(command, timeout=timeout, allow_failure=allow_404)
    if allow_404 and result.returncode != 0:
        stderr = result.stderr.lower()
        if "404" in stderr or "not found" in stderr:
            return None
        raise GhError("GitHub CLI 请求失败。", command=command, stderr=result.stderr.strip())
    return result.stdout


def quote_term(term: str) -> str:
    cleaned = re.sub(r"\s+", " ", term.strip()).replace('"', "")
    return f'"{cleaned}"' if " " in cleaned else cleaned


def normalize_terms(keyword: str, extra_terms: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in [keyword, *extra_terms]:
        term = re.sub(r"\s+", " ", raw.strip())
        key = term.casefold()
        if term and key not in seen:
            result.append(term)
            seen.add(key)
        if len(result) >= MAX_TERMS:
            break
    return result


def build_query_plan(
    keyword: str,
    terms: list[str],
    *,
    days: int,
    language: str | None,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    new_cutoff = (now - dt.timedelta(days=90)).date().isoformat()
    active_cutoff = (now - dt.timedelta(days=max(30, days))).date().isoformat()
    for pool, quota in POOL_QUOTAS.items():
        per_term = max(5, math.ceil(quota / max(len(terms), 1)))
        for index, term in enumerate(terms):
            pieces = [quote_term(term), "in:name,description,topics", "archived:false", "fork:false"]
            sort = "stars"
            if pool == "new":
                pieces.append(f"created:>={new_cutoff}")
            elif pool == "active":
                pieces.append(f"pushed:>={active_cutoff}")
                sort = "updated"
            elif pool == "popular":
                pieces.append("stars:>=20")
            if language:
                pieces.append(f'language:"{language.replace(chr(34), "")}"')
            plan.append(
                {
                    "pool": pool,
                    "term": term,
                    "term_weight": 1.0 if index == 0 else round(max(0.55, 0.8 - 0.08 * (index - 1)), 2),
                    "query": " ".join(pieces),
                    "sort": sort,
                    "order": "desc",
                    "limit": min(per_term, 100),
                }
            )
    return plan


def normalize_repo(item: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    license_obj = item.get("license") or {}
    repo = {
        "id": item.get("id"),
        "full_name": item.get("full_name") or "",
        "name": item.get("name") or "",
        "owner": ((item.get("owner") or {}).get("login") or ""),
        "url": item.get("html_url") or "",
        "description": item.get("description") or "",
        "topics": item.get("topics") or [],
        "primary_language": item.get("language"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "pushed_at": item.get("pushed_at"),
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "subscribers": None,
        "open_issues": int(item.get("open_issues_count") or 0),
        "archived": bool(item.get("archived")),
        "disabled": bool(item.get("disabled")),
        "is_fork": bool(item.get("fork")),
        "is_mirror": bool(item.get("mirror_url")),
        "upstream": None,
        "license": {"spdx_id": license_obj.get("spdx_id") or "NOASSERTION", "name": license_obj.get("name")},
        "homepage": item.get("homepage"),
        "size_kb": item.get("size"),
        "default_branch": item.get("default_branch"),
    }
    return {
        "repo": repo,
        "candidate_pools": [query["pool"]],
        "matched_terms": [{"term": query["term"], "weight": query["term_weight"], "query": query["query"]}],
        "relevance_score": 0.0,
        "recent_activity": {},
        "latest_release": None,
        "readme": None,
        "trend": {},
        "scores": {},
        "analysis": None,
        "flags": [],
    }


def merge_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    existing["candidate_pools"] = sorted(set(existing["candidate_pools"] + incoming["candidate_pools"]))
    known = {(x["term"].casefold(), x["query"]) for x in existing["matched_terms"]}
    for match in incoming["matched_terms"]:
        key = (match["term"].casefold(), match["query"])
        if key not in known:
            existing["matched_terms"].append(match)
            known.add(key)


def relevance_score(project: dict[str, Any], keyword: str) -> float:
    repo = project["repo"]
    canonical = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    name = canonical(repo["name"])
    description = canonical(repo["description"])
    topics = canonical(" ".join(repo["topics"]))
    original = canonical(keyword)
    score = 10.0
    for match in project["matched_terms"]:
        term = canonical(match["term"])
        tokens = [token for token in term.split() if token]
        weight = float(match["weight"])
        local = 0.0
        if term and (term in name or (tokens and all(token in name.split() for token in tokens))):
            local = 100.0
        elif term and (term in topics or (tokens and all(token in topics.split() for token in tokens))):
            local = 92.0
        elif term and (term in description or (tokens and all(token in description.split() for token in tokens))):
            local = 82.0
        elif tokens and all(token in f"{name} {description} {topics}".split() for token in tokens):
            local = 68.0
        if term == original and local:
            local = max(local, 68.0)
        score = max(score, local * weight)
    pool_bonus = min(6.0, 2.0 * len(project["candidate_pools"]))
    return round1(clamp(score + pool_bonus))


def list_only_reason(repo: dict[str, Any]) -> str | None:
    name = repo["name"].casefold()
    desc = repo["description"].casefold()
    topics = {str(x).casefold() for x in repo["topics"]}
    if "awesome-list" in topics or any(name.startswith(pattern) for pattern in LIST_PATTERNS):
        return "list_only"
    phrases = ("curated list", "collection of links", "awesome list", "learning notes", "resource list")
    if any(phrase in desc for phrase in phrases):
        return "list_only"
    return None


def filter_candidates(candidates: list[dict[str, Any]], *, count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for project in candidates:
        repo = project["repo"]
        reason = None
        if repo["archived"]:
            reason = "archived"
        elif repo["disabled"]:
            reason = "disabled"
        elif repo["is_mirror"]:
            reason = "mirror"
        elif repo["is_fork"]:
            reason = "plain_fork"
        else:
            reason = list_only_reason(repo)
        if project["relevance_score"] < 40:
            reason = reason or "low_relevance"
        if reason:
            rejected.append({"full_name": repo["full_name"], "reason": reason, "stage": "filter"})
        else:
            kept.append(project)

    normal = [p for p in kept if p["repo"]["stars"] >= 20]
    low = [p for p in kept if 5 <= p["repo"]["stars"] < 20]
    threshold = 20
    low_sample = False
    if len(normal) < count:
        threshold = 5
        low_sample = True
        normal.extend(low)
    selected_ids = {p["repo"]["full_name"].casefold() for p in normal}
    for project in kept:
        if project["repo"]["full_name"].casefold() not in selected_ids:
            rejected.append({"full_name": project["repo"]["full_name"], "reason": "below_dynamic_threshold", "stage": "threshold"})
    return normal, rejected, threshold, low_sample


def percentile_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    indexed = sorted(enumerate(values), key=lambda pair: pair[1])
    if len(values) == 1:
        return [100.0]
    result = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[position][1]:
            end += 1
        average_rank = (position + end) / 2.0
        score = 100.0 * average_rank / (len(values) - 1)
        for offset in range(position, end + 1):
            result[indexed[offset][0]] = score
        position = end + 1
    return result


def recency_score(age_days: float, half_life_days: float) -> float:
    if age_days >= 99999:
        return 0.0
    return clamp(100.0 * math.exp(-math.log(2.0) * age_days / half_life_days))


def activity_score(project: dict[str, Any], now: dt.datetime) -> float:
    repo = project["repo"]
    push = recency_score(days_since(repo.get("pushed_at"), now), 30.0)
    release = project.get("latest_release") or {}
    release_score = recency_score(days_since(release.get("published_at"), now), 90.0) if release else 30.0
    activity = project.get("recent_activity") or {}
    issues = float(activity.get("issues_updated") or 0)
    prs = float(activity.get("pull_requests_updated") or 0)
    collaboration = clamp(100.0 * math.log1p(issues + 2.0 * prs) / math.log1p(40.0))
    if not activity:
        collaboration = 35.0
    return round1(0.55 * push + 0.20 * release_score + 0.25 * collaboration)


def freshness_completeness(project: dict[str, Any], now: dt.datetime) -> float:
    repo = project["repo"]
    age = days_since(repo.get("created_at"), now)
    newness = recency_score(age, 180.0)
    fields = [repo.get("description"), repo.get("topics"), repo.get("license", {}).get("spdx_id") not in (None, "NOASSERTION"), repo.get("primary_language")]
    completeness = 100.0 * sum(bool(x) for x in fields) / len(fields)
    return round1(0.55 * newness + 0.45 * completeness)


def snapshot_project_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not snapshot:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("projects", []):
        repo = item.get("repo", item)
        full_name = repo.get("full_name")
        if full_name:
            result[full_name.casefold()] = repo
    return result


def score_projects(projects: list[dict[str, Any]], *, baseline: dict[str, Any] | None, now: dt.datetime, days: int) -> str:
    baseline_map = snapshot_project_map(baseline)
    baseline_at = parse_time((baseline or {}).get("captured_at") or ((baseline or {}).get("run") or {}).get("data_as_of"))
    coverage = sum(1 for p in projects if p["repo"]["full_name"].casefold() in baseline_map) / max(len(projects), 1)
    historical_mode = bool(baseline_at and coverage >= 0.4)

    metrics: list[dict[str, float]] = []
    for project in projects:
        repo = project["repo"]
        activity = activity_score(project, now)
        freshness = freshness_completeness(project, now)
        base = baseline_map.get(repo["full_name"].casefold()) if historical_mode else None
        if base:
            star_delta = max(repo["stars"] - int(base.get("stars") or 0), 0)
            fork_delta = max(repo["forks"] - int(base.get("forks") or 0), 0)
            relative = star_delta / max(int(base.get("stars") or 0), 20)
            actual_days = (now - baseline_at).total_seconds() / 86400.0 if baseline_at else None
            project["trend"] = {
                "mode": "snapshot_delta",
                "window_requested_days": days,
                "window_actual_days": round(actual_days, 2) if actual_days is not None else None,
                "baseline_at": iso_z(baseline_at) if baseline_at else None,
                "stars_delta": star_delta,
                "stars_growth_rate": round(relative, 6),
                "forks_delta": fork_delta,
            }
            metrics.append({"abs": float(star_delta), "rel": relative, "fork": float(fork_delta), "activity": activity, "freshness": freshness})
        else:
            age = max(days_since(repo.get("created_at"), now), 1.0)
            project["trend"] = {
                "mode": "cold_start_proxy",
                "window_requested_days": days,
                "window_actual_days": None,
                "baseline_at": None,
                "stars_delta": None,
                "stars_growth_rate": None,
                "forks_delta": None,
                "proxy": {"stars_per_age_day": round(repo["stars"] / age, 6)},
            }
            metrics.append({"maturity": math.log1p(repo["stars"]), "speed": math.log1p(repo["stars"] / age), "fork": math.log1p(repo["forks"]), "activity": activity, "freshness": freshness})

    def pct(key: str) -> list[float]:
        return percentile_scores([float(metric.get(key, 0.0)) for metric in metrics])

    pct_maps = {key: pct(key) for key in {key for metric in metrics for key in metric}}
    for index, project in enumerate(projects):
        metric = metrics[index]
        if project["trend"]["mode"] == "snapshot_delta":
            breakdown = {
                "absolute_star_growth": round1(pct_maps["abs"][index]),
                "relative_star_growth": round1(pct_maps["rel"][index]),
                "fork_growth": round1(pct_maps["fork"][index]),
                "maintenance_activity": round1(metric["activity"]),
                "freshness_completeness": round1(metric["freshness"]),
            }
            heat = 0.35 * breakdown["absolute_star_growth"] + 0.25 * breakdown["relative_star_growth"] + 0.10 * breakdown["fork_growth"] + 0.20 * breakdown["maintenance_activity"] + 0.10 * breakdown["freshness_completeness"]
        else:
            breakdown = {
                "current_star_maturity": round1(pct_maps["maturity"][index]),
                "age_normalized_star_proxy": round1(pct_maps["speed"][index]),
                "fork_traction": round1(pct_maps["fork"][index]),
                "maintenance_activity": round1(metric["activity"]),
                "freshness_completeness": round1(metric["freshness"]),
            }
            heat = 0.20 * breakdown["current_star_maturity"] + 0.30 * breakdown["age_normalized_star_proxy"] + 0.15 * breakdown["fork_traction"] + 0.25 * breakdown["maintenance_activity"] + 0.10 * breakdown["freshness_completeness"]
        project["scores"] = {"heat": round1(heat), "technology": None, "community": None, "commercial": None, "recommendation": None, "breakdown": breakdown}
    return "snapshot_delta" if historical_mode else "cold_start_proxy"


def score_star_credibility(project: dict[str, Any]) -> list[str]:
    repo = project["repo"]
    flags: list[str] = []
    stars = max(repo["stars"], 1)
    if stars >= 500 and repo["forks"] / stars < 0.005 and repo["open_issues"] == 0:
        flags.append("needs_verification:low_fork_and_issue_signal")
    if stars >= 100 and not repo["description"] and not repo["topics"]:
        flags.append("needs_verification:incomplete_repository_profile")
    return flags


def collect_queries(plan: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    merged: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    successes = {pool: 0 for pool in POOL_QUOTAS}
    for query in plan:
        try:
            payload = gh_json(
                "search/repositories",
                fields={"q": query["query"], "sort": query["sort"], "order": query["order"], "per_page": query["limit"]},
                timeout=75,
            )
            successes[query["pool"]] += 1
            for item in payload.get("items", []):
                normalized = normalize_repo(item, query)
                key = normalized["repo"]["full_name"].casefold()
                if not key:
                    continue
                if key in merged:
                    merge_candidate(merged[key], normalized)
                else:
                    merged[key] = normalized
        except GhError as exc:
            errors.append({"stage": "search", "pool": query["pool"], "query": query["query"], "message": str(exc), "detail": exc.stderr[:500], "retryable": True})
    return list(merged.values()), errors, successes


def enrich_project(project: dict[str, Any], *, now: dt.datetime) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    full_name = project["repo"]["full_name"]
    endpoint = f"repos/{full_name}"
    try:
        details = gh_json(endpoint, timeout=60)
        repo = project["repo"]
        license_obj = details.get("license") or {}
        repo.update(
            {
                "subscribers": details.get("subscribers_count"),
                "open_issues": int(details.get("open_issues_count") or 0),
                "license": {"spdx_id": license_obj.get("spdx_id") or "NOASSERTION", "name": license_obj.get("name")},
                "homepage": details.get("homepage"),
                "size_kb": details.get("size"),
                "default_branch": details.get("default_branch"),
            }
        )
    except GhError as exc:
        errors.append({"stage": "repo_details", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    try:
        release = gh_json(f"{endpoint}/releases/latest", timeout=45, allow_404=True)
        if release:
            project["latest_release"] = {"tag": release.get("tag_name"), "name": release.get("name"), "published_at": release.get("published_at"), "url": release.get("html_url")}
    except GhError as exc:
        errors.append({"stage": "release", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    since = iso_z(now - dt.timedelta(days=30))
    try:
        issues = gh_json(f"{endpoint}/issues", fields={"state": "all", "since": since, "per_page": 100, "sort": "updated", "direction": "desc"}, timeout=60)
        issue_count = sum(1 for item in issues if "pull_request" not in item)
        pr_count = sum(1 for item in issues if "pull_request" in item)
        project["recent_activity"] = {"window_days": 30, "issues_updated": issue_count, "pull_requests_updated": pr_count, "sample_capped": len(issues) >= 100}
    except GhError as exc:
        errors.append({"stage": "issues_prs", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    try:
        readme = gh_raw(f"{endpoint}/readme", timeout=60, allow_404=True)
        if readme is not None:
            clipped = readme[:16000]
            project["readme"] = {
                "source_url": f"https://github.com/{full_name}#readme",
                "fetched_at": iso_z(now),
                "content_excerpt": clipped,
                "content_truncated": len(readme) > len(clipped),
                "excerpt_sha256": hashlib.sha256(clipped.encode("utf-8")).hexdigest(),
            }
    except GhError as exc:
        errors.append({"stage": "readme", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    project["flags"] = sorted(set(project.get("flags", []) + score_star_credibility(project)))
    return errors


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_baseline(snapshot_dir: Path, *, keyword: str, now: dt.datetime, days: int) -> dict[str, Any] | None:
    if not snapshot_dir.exists():
        return None
    target = now - dt.timedelta(days=days)
    tolerance_days = max(2.0, days * 0.35)
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in snapshot_dir.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = load_json(path)
            captured = parse_time(payload.get("captured_at") or ((payload.get("run") or {}).get("data_as_of")))
            snapshot_keyword = ((payload.get("input") or {}).get("keyword") or "").casefold()
            valid = payload.get("is_valid_snapshot", ((payload.get("run") or {}).get("is_valid_snapshot", False)))
            if not captured or snapshot_keyword != keyword.casefold() or not valid or captured >= now:
                continue
            difference = abs((captured - target).total_seconds()) / 86400.0
            if difference <= tolerance_days:
                candidates.append((difference, payload))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def latest_valid_snapshot(snapshot_dir: Path, *, keyword: str) -> dict[str, Any] | None:
    snapshots: list[tuple[dt.datetime, dict[str, Any]]] = []
    if not snapshot_dir.exists():
        return None
    for path in snapshot_dir.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = load_json(path)
            captured = parse_time(payload.get("captured_at") or ((payload.get("run") or {}).get("data_as_of")))
            snapshot_keyword = ((payload.get("input") or {}).get("keyword") or "").casefold()
            valid = payload.get("is_valid_snapshot", ((payload.get("run") or {}).get("is_valid_snapshot", False)))
            if captured and snapshot_keyword == keyword.casefold() and valid:
                snapshots.append((captured, payload))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return max(snapshots, key=lambda item: item[0])[1] if snapshots else None


def snapshot_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": result["run"]["data_as_of"],
        "is_valid_snapshot": True,
        "input": result["input"],
        "queries": result["queries"],
        "projects": [
            {
                "repo": {
                    "full_name": item["repo"]["full_name"],
                    "stars": item["repo"]["stars"],
                    "forks": item["repo"]["forks"],
                    "pushed_at": item["repo"]["pushed_at"],
                    "created_at": item["repo"]["created_at"],
                    "url": item["repo"]["url"],
                    "description": item["repo"]["description"],
                    "primary_language": item["repo"]["primary_language"],
                    "license": item["repo"]["license"],
                }
            }
            for item in result["projects"]
        ],
    }


def analysis_template(projects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "executive_summary": [],
        "topic_outlook": "",
        "projects": [
            {
                "full_name": item["repo"]["full_name"],
                "technology_score": None,
                "community_score": None,
                "commercial_score": None,
                "positioning": "",
                "why_hot": [],
                "suitable_for": [],
                "secondary_development": [],
                "business_models": [],
                "risks": [],
                "facts": [],
                "inferences": [],
                "speculations": [],
                "confidence": "medium",
            }
            for item in projects
        ],
    }


def degraded_from_snapshot(snapshot: dict[str, Any], *, keyword: str, days: int, count: int, errors: list[dict[str, Any]], run_id: str, now: dt.datetime) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    for item in snapshot.get("projects", [])[:count]:
        repo = dict(item.get("repo", item))
        projects.append(
            {
                "repo": repo,
                "candidate_pools": [],
                "matched_terms": [],
                "relevance_score": None,
                "recent_activity": {},
                "latest_release": None,
                "readme": None,
                "trend": {"mode": "stale_snapshot_fallback", "stars_delta": None, "stars_growth_rate": None, "forks_delta": None},
                "scores": {"heat": None, "technology": None, "community": None, "commercial": None, "recommendation": None, "breakdown": {}},
                "analysis": None,
                "flags": ["stale_snapshot_fallback"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {"id": run_id, "started_at": iso_z(now), "completed_at": iso_z(utc_now()), "mode": "stale_snapshot_fallback", "data_as_of": snapshot.get("captured_at"), "auth_route": "unavailable", "is_valid_snapshot": False},
        "input": {"keyword": keyword, "time_range_days": days, "count": count, "language": None},
        "queries": snapshot.get("queries", []),
        "collection": {"candidate_count": len(projects), "filtered_count": len(projects), "dynamic_star_threshold": None, "low_sample": True, "degraded": True},
        "rankings": {"heat": [p["repo"]["full_name"] for p in projects], "recommendation": []},
        "projects": projects,
        "rejected_candidates": [],
        "limitations": ["GitHub 实时采集失败；本结果来自最近有效快照，不是实时榜单。"],
        "errors": errors,
    }


def collect_command(args: argparse.Namespace) -> int:
    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    keyword_slug = slugify(args.keyword)
    root = Path(args.output_root).expanduser().resolve() / keyword_slug
    run_dir = root / "runs" / run_id
    snapshot_dir = root / "snapshots"
    errors: list[dict[str, Any]] = []
    terms = normalize_terms(args.keyword, args.term or [])
    plan = build_query_plan(args.keyword, terms, days=args.days, language=args.language, now=started)

    try:
        check_gh_auth()
        candidates, query_errors, successes = collect_queries(plan)
        errors.extend(query_errors)
    except GhError as exc:
        errors.append({"stage": "authentication", "message": str(exc), "detail": exc.stderr[:500], "retryable": False})
        stale = latest_valid_snapshot(snapshot_dir, keyword=args.keyword)
        if not stale:
            run_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(run_dir / "errors.json", errors)
            print(f"ERROR: {exc}", file=sys.stderr)
            print(f"错误记录：{run_dir / 'errors.json'}", file=sys.stderr)
            return 2
        result = degraded_from_snapshot(stale, keyword=args.keyword, days=args.days, count=args.count, errors=errors, run_id=run_id, now=started)
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(run_dir / "result.json", result)
        atomic_write_json(run_dir / "errors.json", errors)
        atomic_write_json(run_dir / "analysis-template.json", analysis_template(result["projects"]))
        print(str(run_dir))
        return 0

    for project in candidates:
        project["relevance_score"] = relevance_score(project, args.keyword)
    filtered, rejected, threshold, low_sample = filter_candidates(candidates, count=args.count)
    baseline = find_baseline(snapshot_dir, keyword=args.keyword, now=started, days=args.days)

    score_projects(filtered, baseline=baseline, now=started, days=args.days)
    filtered.sort(key=lambda item: (item["scores"]["heat"], item["relevance_score"], item["repo"]["stars"]), reverse=True)
    shortlist_size = min(len(filtered), args.count)
    shortlist = filtered[:shortlist_size]
    for project in shortlist:
        errors.extend(enrich_project(project, now=started))
    mode = score_projects(shortlist, baseline=baseline, now=started, days=args.days)
    shortlist.sort(key=lambda item: (item["scores"]["heat"], item["relevance_score"], item["repo"]["stars"]), reverse=True)
    top = shortlist[: args.count]

    pool_ok = all(successes.get(pool, 0) > 0 for pool in POOL_QUOTAS)
    core_fields_ok = bool(top) and all(p["repo"]["full_name"] and p["repo"]["stars"] >= 0 and p["repo"]["forks"] >= 0 and p["repo"]["created_at"] for p in top)
    valid_snapshot = pool_ok and core_fields_ok
    limitations: list[str] = []
    if mode == "cold_start_proxy":
        limitations.append("没有合格历史快照；热度为首次运行代理分，不代表真实近 7 天增长。")
    if low_sample:
        limitations.append("结果不足以维持 20 Stars 门槛，已降至 5；低样本项目置信度需下调。")
    if errors:
        limitations.append("部分补充字段请求失败；请查看 errors.json。")
    if not valid_snapshot:
        limitations.append("关键候选池或字段不完整，本次不会写入有效快照。")

    completed = utc_now()
    result = {
        "schema_version": SCHEMA_VERSION,
        "run": {"id": run_id, "started_at": iso_z(started), "completed_at": iso_z(completed), "mode": mode, "data_as_of": iso_z(completed), "auth_route": "direct_gh", "is_valid_snapshot": valid_snapshot},
        "input": {"keyword": args.keyword, "time_range_days": args.days, "count": args.count, "language": args.language, "repository_types": ["application", "tool", "framework", "library", "model", "dataset"]},
        "queries": [{"text": item["query"], "kind": item["pool"], "term": item["term"], "term_weight": item["term_weight"], "sort": item["sort"], "limit": item["limit"]} for item in plan],
        "collection": {"candidate_count": len(candidates), "filtered_count": len(filtered), "shortlist_count": len(shortlist), "dynamic_star_threshold": threshold, "low_sample": low_sample, "pool_successes": successes, "degraded": False},
        "rankings": {"heat": [p["repo"]["full_name"] for p in top], "recommendation": []},
        "projects": top,
        "rejected_candidates": rejected[:100],
        "limitations": limitations,
        "errors": errors,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "collection.json", {"queries": result["queries"], "collection": result["collection"], "candidates": shortlist, "rejected_candidates": rejected})
    atomic_write_json(run_dir / "result.json", result)
    atomic_write_json(run_dir / "errors.json", errors)
    atomic_write_json(run_dir / "analysis-template.json", analysis_template(top))
    if valid_snapshot:
        atomic_write_json(snapshot_dir / f"{run_id}.json", snapshot_payload(result))
    print(str(run_dir))
    return 0


def validate_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoutError(f"{field} 必须是 0–100 的数字。")
    if not 0 <= float(value) <= 100:
        raise ScoutError(f"{field} 超出 0–100。")
    return round1(float(value))


def validate_analysis(analysis: dict[str, Any], expected: set[str]) -> dict[str, dict[str, Any]]:
    projects = analysis.get("projects")
    if not isinstance(projects, list):
        raise ScoutError("analysis.projects 必须是数组。")
    mapped: dict[str, dict[str, Any]] = {}
    for item in projects:
        name = str(item.get("full_name") or "")
        key = name.casefold()
        if key not in expected:
            raise ScoutError(f"分析包含未知项目：{name}")
        if key in mapped:
            raise ScoutError(f"分析项目重复：{name}")
        for field in ("technology_score", "community_score", "commercial_score"):
            item[field] = validate_score(item.get(field), f"{name}.{field}")
        confidence = item.get("confidence")
        if confidence not in ("high", "medium", "low"):
            raise ScoutError(f"{name}.confidence 必须是 high、medium 或 low。")
        for fact in item.get("facts", []):
            if not isinstance(fact, dict) or not fact.get("claim") or not str(fact.get("source_url") or "").startswith(("https://", "http://")):
                raise ScoutError(f"{name}.facts 每项必须包含 claim 和 HTTP(S) source_url。")
        mapped[key] = item
    missing = expected - set(mapped)
    if missing:
        raise ScoutError(f"缺少项目分析：{', '.join(sorted(missing))}")
    return mapped


def md_escape(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def bullet_lines(values: list[Any] | None, empty: str = "暂无。") -> str:
    if not values:
        return empty
    output = []
    for value in values:
        if isinstance(value, dict):
            claim = value.get("claim") or value.get("text") or json.dumps(value, ensure_ascii=False)
            url = value.get("source_url")
            output.append(f"- {claim}" + (f"（[来源]({url})）" if url else ""))
        else:
            output.append(f"- {value}")
    return "\n".join(output)


def render_report(result: dict[str, Any], analysis: dict[str, Any]) -> str:
    projects = result["projects"]
    by_name = {p["repo"]["full_name"]: p for p in projects}
    heat_order = result["rankings"]["heat"]
    recommendation_order = result["rankings"]["recommendation"]
    rec_rank = {name: i + 1 for i, name in enumerate(recommendation_order)}
    heat_rank = {name: i + 1 for i, name in enumerate(heat_order)}
    mode_labels = {"snapshot_delta": "真实快照增量", "cold_start_proxy": "首次运行代理热度", "stale_snapshot_fallback": "旧快照降级（非实时）"}
    mode = result["run"]["mode"]
    lines = [
        f"# GitHub 趋势与机会报告：{result['input']['keyword']}",
        "",
        f"> 数据时间：{result['run'].get('data_as_of') or '未知'}｜请求窗口：{result['input']['time_range_days']} 天｜运行模式：{mode_labels.get(mode, mode)}｜样本候选：{result['collection'].get('candidate_count', 0)}",
        "",
    ]
    if mode == "stale_snapshot_fallback":
        lines.extend(["> **注意：这是旧快照降级结果，不是实时榜单。**", ""])
    lines.extend(["## 执行摘要", "", bullet_lines(analysis.get("executive_summary")), ""])
    if analysis.get("topic_outlook"):
        lines.extend([f"主题判断：{analysis['topic_outlook']}", ""])
    lines.extend(["## 双榜对比", "", "| 热度排名 | 综合排名 | 项目 | 热度 | 技术 | 社区 | 商业 | 综合 | 置信度 |", "|---:|---:|---|---:|---:|---:|---:|---:|---|"])
    for name in heat_order:
        project = by_name[name]
        scores = project["scores"]
        lines.append(f"| {heat_rank[name]} | {rec_rank.get(name, '—')} | [{md_escape(name)}]({project['repo']['url']}) | {scores['heat']} | {scores['technology']} | {scores['community']} | {scores['commercial']} | {scores['recommendation']} | {project['analysis']['confidence']} |")
    lines.extend(["", "## 事实热度榜", "", "| 排名 | 项目 | Stars | 增长或代理信号 | Forks | 最近 Push |", "|---:|---|---:|---|---:|---|"])
    for name in heat_order:
        project = by_name[name]
        trend = project["trend"]
        if trend["mode"] == "snapshot_delta":
            signal = f"+{trend['stars_delta']} Stars / {trend.get('window_actual_days')} 天"
        elif trend["mode"] == "cold_start_proxy":
            signal = f"代理：{trend.get('proxy', {}).get('stars_per_age_day', 0):.2f} Stars/项目日"
        else:
            signal = "旧快照，无实时增量"
        lines.append(f"| {heat_rank[name]} | [{md_escape(name)}]({project['repo']['url']}) | {project['repo']['stars']} | {signal} | {project['repo']['forks']} | {project['repo'].get('pushed_at') or '—'} |")
    lines.extend(["", "## 综合推荐榜", "", "| 排名 | 项目 | 综合分 | 一句话定位 | 主要风险 |", "|---:|---|---:|---|---|"])
    for name in recommendation_order:
        project = by_name[name]
        risks = project["analysis"].get("risks") or []
        lines.append(f"| {rec_rank[name]} | [{md_escape(name)}]({project['repo']['url']}) | {project['scores']['recommendation']} | {md_escape(project['analysis'].get('positioning') or '—')} | {md_escape(risks[0] if risks else '暂无明确高风险')} |")

    lines.extend(["", "## 项目详情", ""])
    for name in recommendation_order:
        project = by_name[name]
        repo = project["repo"]
        detail = project["analysis"]
        license_id = (repo.get("license") or {}).get("spdx_id") or "NOASSERTION"
        lines.extend(
            [
                f"### {rec_rank[name]}. {name}",
                "",
                f"- GitHub：[{repo['url']}]({repo['url']})",
                f"- 定位：{detail.get('positioning') or '—'}",
                f"- 数据：{repo['stars']} Stars｜{repo['forks']} Forks｜{repo.get('primary_language') or '未知语言'}｜{license_id}",
                f"- 最近 Push：{repo.get('pushed_at') or '—'}",
                f"- 分数：热度 {project['scores']['heat']}｜技术 {project['scores']['technology']}｜社区 {project['scores']['community']}｜商业 {project['scores']['commercial']}｜综合 {project['scores']['recommendation']}",
                f"- 置信度：{detail['confidence']}",
                "",
                "**事实**",
                "",
                bullet_lines(detail.get("facts")),
                "",
                "**为什么热门（分析）**",
                "",
                bullet_lines(detail.get("why_hot")),
                "",
                "**适合对象与场景**",
                "",
                bullet_lines(detail.get("suitable_for")),
                "",
                "**二次开发方向**",
                "",
                bullet_lines(detail.get("secondary_development")),
                "",
                "**商业模式**",
                "",
                bullet_lines(detail.get("business_models")),
                "",
                "**风险**",
                "",
                bullet_lines(detail.get("risks")),
                "",
                "**分析依据与推测**",
                "",
                bullet_lines(detail.get("inferences")),
                "",
                bullet_lines(detail.get("speculations"), empty="无额外推测。"),
                "",
            ]
        )
    lines.extend(["## 落选候选与原因", "", "| 项目 | 阶段 | 原因 |", "|---|---|---|"])
    for item in result.get("rejected_candidates", [])[:30]:
        lines.append(f"| {md_escape(item.get('full_name'))} | {md_escape(item.get('stage'))} | {md_escape(item.get('reason'))} |")
    if not result.get("rejected_candidates"):
        lines.append("| — | — | 本次没有可展示的落选记录 |")
    lines.extend(["", "## 方法、数据时间与限制", "", f"- 实际查询词：{', '.join(q['text'] for q in result.get('queries', [])) or '旧快照未保留查询词'}", f"- 动态 Stars 门槛：{result['collection'].get('dynamic_star_threshold')}"])
    lines.extend(f"- {item}" for item in result.get("limitations", []))
    lines.append("- Star 热度不等于代码质量、用户量或商业需求；README 属于项目方自述。")
    return "\n".join(lines) + "\n"


def finalize_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise ScoutError(f"找不到 {result_path}")
    result = load_json(result_path)
    if result.get("run", {}).get("mode") == "stale_snapshot_fallback":
        raise ScoutError("旧快照降级结果缺少实时证据，不应生成完整商业分析报告。")
    analysis = load_json(Path(args.analysis_file).expanduser().resolve())
    expected = {p["repo"]["full_name"].casefold() for p in result["projects"]}
    mapped = validate_analysis(analysis, expected)
    for project in result["projects"]:
        item = mapped[project["repo"]["full_name"].casefold()]
        scores = project["scores"]
        scores["technology"] = item["technology_score"]
        scores["community"] = item["community_score"]
        scores["commercial"] = item["commercial_score"]
        scores["recommendation"] = round1(0.30 * scores["heat"] + 0.25 * scores["technology"] + 0.20 * scores["community"] + 0.25 * scores["commercial"])
        project["analysis"] = {key: value for key, value in item.items() if key not in ("technology_score", "community_score", "commercial_score", "full_name")}
    result["rankings"]["recommendation"] = [p["repo"]["full_name"] for p in sorted(result["projects"], key=lambda p: (p["scores"]["recommendation"], p["scores"]["heat"]), reverse=True)]
    result["analysis_summary"] = {"executive_summary": analysis.get("executive_summary", []), "topic_outlook": analysis.get("topic_outlook", "")}
    result["run"]["finalized_at"] = iso_z(utc_now())
    atomic_write_json(result_path, result)
    atomic_write_text(run_dir / "report.md", render_report(result, analysis))
    print(str(run_dir / "report.md"))
    return 0


def month_end_utc(year: int, month: int) -> dt.datetime:
    return dt.datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=dt.timezone.utc)


def compact_history(args: argparse.Namespace) -> int:
    keyword_root = Path(args.output_root).expanduser().resolve() / slugify(args.keyword)
    snapshot_dir = (keyword_root / "snapshots").resolve()
    monthly_dir = keyword_root / "monthly"
    if not snapshot_dir.exists():
        print("没有可整理的快照。")
        return 0
    cutoff = utc_now() - dt.timedelta(days=args.retention_days)
    groups: dict[str, list[tuple[Path, dict[str, Any], dt.datetime]]] = {}
    for path in snapshot_dir.glob("*.json"):
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file() or resolved.parent != snapshot_dir:
            continue
        try:
            payload = load_json(path)
            captured = parse_time(payload.get("captured_at"))
            if captured and captured < cutoff:
                groups.setdefault(captured.strftime("%Y-%m"), []).append((path, payload, captured))
        except (OSError, json.JSONDecodeError):
            continue
    plan: list[dict[str, Any]] = []
    for month, entries in sorted(groups.items()):
        entries.sort(key=lambda item: item[2])
        repo_series: dict[str, list[dict[str, Any]]] = {}
        for _, payload, captured in entries:
            for project in payload.get("projects", []):
                repo = project.get("repo", project)
                name = repo.get("full_name")
                if name:
                    repo_series.setdefault(name, []).append({"captured_at": iso_z(captured), "stars": repo.get("stars"), "forks": repo.get("forks")})
        summary = {"schema_version": SCHEMA_VERSION, "keyword": args.keyword, "month": month, "source_snapshot_count": len(entries), "repositories": []}
        for name, series in sorted(repo_series.items()):
            first, last = series[0], series[-1]
            summary["repositories"].append({"full_name": name, "first": first, "last": last, "stars_delta": (last["stars"] or 0) - (first["stars"] or 0), "forks_delta": (last["forks"] or 0) - (first["forks"] or 0)})
        plan.append({"month": month, "files": [str(item[0]) for item in entries], "summary": summary})
    if not plan:
        print("没有超过保留期限的快照。")
        return 0
    if not args.apply:
        print(json.dumps({"dry_run": True, "months": [{"month": item["month"], "snapshot_count": len(item["files"])} for item in plan]}, ensure_ascii=False, indent=2))
        return 0
    for item in plan:
        atomic_write_json(monthly_dir / f"{item['month']}.json", item["summary"])
        for raw_path in item["files"]:
            path = Path(raw_path)
            resolved = path.resolve()
            if path.is_symlink() or not path.is_file() or resolved.parent != snapshot_dir:
                raise ScoutError(f"拒绝删除未通过边界检查的文件：{path}")
            path.unlink()
    print(json.dumps({"dry_run": False, "compacted_months": [item["month"] for item in plan]}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按关键词采集并分析 GitHub 趋势项目。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="采集、评分并生成分析模板。")
    collect.add_argument("--keyword", required=True)
    collect.add_argument("--term", action="append", default=[], help="扩展查询词，可重复；最多连同原词保留 4 个。")
    collect.add_argument("--days", type=int, default=DEFAULT_DAYS, choices=range(1, 366), metavar="1..365")
    collect.add_argument("--count", type=int, default=DEFAULT_COUNT, choices=range(5, 21), metavar="5..20")
    collect.add_argument("--language")
    collect.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    collect.set_defaults(func=collect_command)

    finalize = subparsers.add_parser("finalize", help="校验分析并生成最终 Markdown 与 JSON。")
    finalize.add_argument("--run-dir", required=True)
    finalize.add_argument("--analysis-file", required=True)
    finalize.set_defaults(func=finalize_command)

    compact = subparsers.add_parser("compact-history", help="汇总超期快照；默认只做 dry-run。")
    compact.add_argument("--keyword", required=True)
    compact.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    compact.add_argument("--retention-days", type=int, default=365)
    compact.add_argument("--apply", action="store_true", help="写入月度汇总并删除已汇总的逐日快照。")
    compact.set_defaults(func=compact_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ScoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: 用户中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
