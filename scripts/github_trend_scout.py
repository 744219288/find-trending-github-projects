#!/usr/bin/env python3
"""Collect, score, snapshot, and finalize keyword-driven GitHub trend reports.

Uses only the Python standard library and GitHub CLI. Repository content is read as
untrusted data and is never executed.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_card_report import render_card_report as _render_card_report  # noqa: E402

SCHEMA_VERSION = "1.0"
DEFAULT_COUNT = 10
DEFAULT_DAYS = 7
DEFAULT_CANDIDATE_TARGET = 80
MAX_TERMS = 4
POOL_QUOTAS = {"relevance": 24, "new": 20, "active": 20, "popular": 16}

# 传输层：auto（默认，优先 gh CLI，否则回退到纯 API；无 token 时自动进入匿名模式）/ gh / api。
TRANSPORT = os.environ.get("GTS_TRANSPORT", "auto")

# 匿名模式（未认证）限流参数：GitHub 未认证搜索 10 次/分钟、核心 60 次/小时。
ANONYMOUS_MIN_INTERVAL = 6.5  # 匿名请求最小间隔（秒），保守按搜索配额 10/min 预留
ANONYMOUS_MAX_WAIT = 300.0    # 等待限流重置的最长时间（秒），超过则放弃并降级
try:
    ANONYMOUS_MIN_INTERVAL = max(1.0, float(os.environ.get("GTS_MIN_INTERVAL", ANONYMOUS_MIN_INTERVAL)))
except (TypeError, ValueError):
    pass


class RateLimitTracker:
    """跨请求维护 GitHub API 配额预算；匿名模式下强制串行 + 自适应等待。"""

    def __init__(self, anonymous: bool):
        self.anonymous = anonymous
        self.min_interval = ANONYMOUS_MIN_INTERVAL if anonymous else 0.0
        self._last_request_at = 0.0
        self.remaining: int | None = None
        self.reset_at: float | None = None

    def before_request(self) -> None:
        if self.anonymous:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        if self.remaining is not None and self.remaining <= 0 and self.reset_at is not None:
            wait = self.reset_at - time.time()
            if 0 < wait <= ANONYMOUS_MAX_WAIT:
                time.sleep(wait)

    def observe(self, headers: Any) -> None:
        remaining = headers.get("X-RateLimit-Remaining") if headers is not None else None
        reset = headers.get("X-RateLimit-Reset") if headers is not None else None
        if remaining is not None:
            try:
                self.remaining = int(remaining)
            except (TypeError, ValueError):
                pass
        if reset is not None:
            try:
                self.reset_at = float(reset)
            except (TypeError, ValueError):
                pass
        self._last_request_at = time.monotonic()

    def wait_seconds(self) -> float:
        if self.reset_at is None:
            return 0.0
        return max(0.0, self.reset_at - time.time())


_RATE_LIMIT: RateLimitTracker | None = None
# auto 模式下 gh 存在但未认证时，回退到纯 API（含匿名），避免"装了 gh 没登录"被拦。
_FALLBACK_API = False


def _ensure_rate_tracker(anonymous: bool) -> RateLimitTracker:
    global _RATE_LIMIT
    if _RATE_LIMIT is None or _RATE_LIMIT.anonymous != anonymous:
        _RATE_LIMIT = RateLimitTracker(anonymous)
    return _RATE_LIMIT

# README 跨运行缓存（仅在本进程 collect 期间加载/保存，避免对未改动仓库重复抓取）。
_README_CACHE: dict[str, Any] = {}
_README_CACHE_PATH: Path | None = None

# 仓库详情跨运行缓存（跨关键词共享：同一仓库 24h 内不重复抓详情/发版，省配额省时间）。
DETAIL_CACHE_TTL_HOURS = 24.0
try:
    DETAIL_CACHE_TTL_HOURS = max(1.0, float(os.environ.get("GTS_DETAIL_CACHE_HOURS", DETAIL_CACHE_TTL_HOURS)))
except (TypeError, ValueError):
    pass
_DETAIL_CACHE: dict[str, Any] = {}
_DETAIL_CACHE_PATH: Path | None = None

# 认证模式下详情抓取的并发数（匿名模式强制串行，避免配额浪费在竞争重试上）。
ENRICH_WORKERS = 4
try:
    ENRICH_WORKERS = max(1, int(os.environ.get("GTS_MAX_WORKERS", ENRICH_WORKERS)))
except (TypeError, ValueError):
    pass
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
    def __init__(self, message: str, *, command: list[str] | None = None, stderr: str = "", detail: str = ""):
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr or detail
        self.detail = detail or stderr


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


def _read_token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _auto_transport() -> str:
    if shutil.which("gh") is not None:
        return "gh"
    # 无 gh CLI 时回退到纯 API；无 token 也允许（匿名模式），由 _api_request 处理。
    return "api"


def _active_transport() -> str:
    setting = TRANSPORT
    if setting == "auto":
        if _FALLBACK_API:
            return "api"
        return _auto_transport()
    if setting == "api":
        return "api"
    if setting == "gh" and shutil.which("gh") is None:
        raise GhError("GTS_TRANSPORT=gh 但找不到 gh CLI，请安装 GitHub CLI。")
    return setting


def _is_anonymous() -> bool:
    """当前是否处于匿名（未认证）API 模式。"""
    return _active_transport() == "api" and not _read_token()


def check_auth() -> str | None:
    """确保传输层可用。返回补充提示（如匿名/回退模式说明），不可用时抛 GhError。"""
    transport = _active_transport()
    if transport == "gh":
        if shutil.which("gh") is None:
            raise GhError("未找到 gh CLI。请先安装 GitHub CLI。")
        result = run_process(["gh", "auth", "status"], timeout=20, allow_failure=True)
        if result.returncode != 0:
            global _FALLBACK_API
            if TRANSPORT == "auto":
                _FALLBACK_API = True
                return "检测到 gh CLI 但未认证，已自动回退到纯 API 模式（无 token 时匿名）。配置凭证可提升配额。"
            raise GhError("gh 尚未认证。请在本机运行 gh auth login。", command=["gh", "auth", "status"], stderr=result.stderr.strip())
        return None
    # api 路径：有 token 用认证模式，无 token 自动匿名，均可用。
    if not _read_token():
        return "未检测到 GH_TOKEN/GITHUB_TOKEN，已启用匿名模式（未认证配额：搜索 10 次/分钟、核心 60 次/小时）。配置 token 可获得更高配额与更完整详情。"
    return None


def gh_json(endpoint: str, *, fields: dict[str, Any] | None = None, timeout: int = 60, allow_404: bool = False) -> Any:
    if _active_transport() == "api":
        return _api_json(endpoint, fields=fields, timeout=timeout, allow_404=allow_404)
    return _gh_json(endpoint, fields=fields, timeout=timeout, allow_404=allow_404)


def gh_raw(endpoint: str, *, timeout: int = 60, allow_404: bool = False) -> str | None:
    if _active_transport() == "api":
        return _api_raw(endpoint, timeout=timeout, allow_404=allow_404)
    return _gh_raw(endpoint, timeout=timeout, allow_404=allow_404)


def _gh_json(endpoint: str, *, fields: dict[str, Any] | None = None, timeout: int = 60, allow_404: bool = False) -> Any:
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


def _gh_raw(endpoint: str, *, timeout: int = 60, allow_404: bool = False) -> str | None:
    command = ["gh", "api", "--method", "GET", endpoint, "-H", "Accept: application/vnd.github.raw+json"]
    result = run_process(command, timeout=timeout, allow_failure=allow_404)
    if allow_404 and result.returncode != 0:
        stderr = result.stderr.lower()
        if "404" in stderr or "not found" in stderr:
            return None
        raise GhError("GitHub CLI 请求失败。", command=command, stderr=result.stderr.strip())
    return result.stdout


def _network_diagnosis() -> str:
    """请求失败后快速自检网络，返回人话诊断与建议。"""
    hints: list[str] = []
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if proxy:
        hints.append(f"当前走代理 {proxy}，请确认代理可用")
    else:
        hints.append("未配置代理；网络受限环境可设置 HTTPS_PROXY 环境变量后重试")
    try:
        with urllib.request.urlopen("https://api.github.com/rate_limit", timeout=8) as resp:
            hints.append(f"api.github.com 此刻可达（HTTP {resp.status}），刚才是临时抖动，直接重试即可")
    except urllib.error.HTTPError as exc:
        hints.append(f"api.github.com 返回 HTTP {exc.code}（网络通，多为配额/服务端问题）")
    except Exception as exc:  # noqa: BLE001 - 诊断需捕获所有网络异常
        hints.append(f"api.github.com 不可达（{exc}）：请检查网络能否访问 GitHub，或设置 HTTPS_PROXY")
    return "；".join(hints)


def _api_request(endpoint: str, *, params: dict[str, Any] | None, accept: str, allow_404: bool, timeout: int) -> str | None:
    token = _read_token()
    anonymous = token is None
    tracker = _ensure_rate_tracker(anonymous)
    url = "https://api.github.com/" + endpoint.lstrip("/")
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {
        "Accept": accept,
        "User-Agent": "github-trend-scout/1.2",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    attempts = 0
    while True:
        tracker.before_request()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                tracker.observe(resp.headers)
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            is_rate_limit = exc.code in (403, 429) and str(exc.headers.get("X-RateLimit-Remaining", "")) == "0"
            if is_rate_limit:
                tracker.observe(exc.headers)
                attempts += 1
                wait = tracker.wait_seconds()
                if attempts >= 3 or (anonymous and wait > ANONYMOUS_MAX_WAIT):
                    if anonymous:
                        mins = max(1, round(tracker.wait_seconds() / 60))
                        raise GhError(
                            f"GitHub 匿名配额已用尽（未认证约 60 次/小时）。约 {mins} 分钟后自动恢复；"
                            "想立即继续并解锁完整详情，可免费申请 GitHub Token 并设置环境变量 GH_TOKEN（配额提升到 5000 次/小时）。"
                        )
                    raise GhError("GitHub API 速率限制已用尽，请稍后重试或提升 token 配额。")
                if wait <= 0:
                    wait = 30.0
                time.sleep(min(wait, ANONYMOUS_MAX_WAIT))
                continue
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code == 401 and anonymous:
                raise GhError("GitHub API 拒绝匿名访问此端点（HTTP 401）。请设置 GH_TOKEN 后重试。", detail=detail)
            raise GhError(f"GitHub API 请求失败：HTTP {exc.code}。", detail=detail)
        except urllib.error.URLError as exc:
            raise GhError(f"GitHub API 网络错误：{exc.reason}。诊断：{_network_diagnosis()}")


def _api_json(endpoint: str, *, fields: dict[str, Any] | None = None, timeout: int = 60, allow_404: bool = False) -> Any:
    body = _api_request(endpoint, params=fields, accept="application/vnd.github+json", allow_404=allow_404, timeout=timeout)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GhError("GitHub API 返回的不是有效 JSON。", detail=body[:500]) from exc


def _api_raw(endpoint: str, *, timeout: int = 60, allow_404: bool = False) -> str | None:
    return _api_request(endpoint, params=None, accept="application/vnd.github.raw+json", allow_404=allow_404, timeout=timeout)


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
    fresh_days: int | None = None,
) -> list[dict[str, Any]]:
    """构建四池搜索计划。

    fresh_days（--fresh --fresh-days N）会把 created:>=cutoff 注入**全部**池的查询：
    只靠事后过滤会浪费四分之三的候选池配额（老仓库占满名额再被剔除），
    且新仓库短期内攒不到 20 星，popular 池的 stars:>=20 同步放宽为 >=5。
    """
    plan: list[dict[str, Any]] = []
    new_cutoff = (now - dt.timedelta(days=90)).date().isoformat()
    active_cutoff = (now - dt.timedelta(days=max(30, days))).date().isoformat()
    fresh_cutoff = (now - dt.timedelta(days=fresh_days)).date().isoformat() if fresh_days else None
    for pool, quota in POOL_QUOTAS.items():
        per_term = max(5, math.ceil(quota / max(len(terms), 1)))
        for index, term in enumerate(terms):
            pieces = [quote_term(term), "in:name,description,topics", "archived:false", "fork:false"]
            sort = "stars"
            if pool == "new":
                pieces.append(f"created:>={fresh_cutoff or new_cutoff}")
            elif pool == "active":
                pieces.append(f"pushed:>={active_cutoff}")
                sort = "updated"
            elif pool == "popular":
                # 新项目 7 天内攒 10 星已经很热：fresh 模式放宽星数门槛
                pieces.append("stars:>=5" if fresh_cutoff else "stars:>=20")
            if fresh_cutoff and pool != "new":
                pieces.append(f"created:>={fresh_cutoff}")
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


# 词干级停用词：这些词单独出现不代表命中（GitHub 搜索会做词干化导致过度匹配）。
STOP_TOKENS = {"the", "for", "and", "with", "to", "of", "a", "an", "in", "on", "by"}

# 主题相关度阈值：主关键词分词后至少这么高比例出现在项目文本里，否则视为疑似跑题。
RELEVANCE_COVERAGE_THRESHOLD = 0.5


def token_coverage(project: dict[str, Any]) -> float:
    """主题相关度复核：所有查询词里最好的一个，其有效分词有多少出现在 name/description/topics。

    GitHub 搜索会做词干化（image 命中 images），容易放进跑题项目（如搜生图混入截图工具）；
    本函数用严格整词匹配重新复核，返回 0.0~1.0 的最佳覆盖率。
    """
    repo = project["repo"]
    canonical = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
    haystack = set(
        f"{canonical(repo.get('name'))} {canonical(repo.get('description'))} {canonical(' '.join(repo.get('topics') or []))}".split()
    )
    best = 0.0
    for match in project.get("matched_terms") or []:
        term = canonical(match.get("term"))
        tokens = [t for t in term.split() if t and t not in STOP_TOKENS]
        if not tokens:
            continue
        hits = sum(1 for token in tokens if token in haystack)
        best = max(best, hits / len(tokens))
    return best


def apply_excludes(candidates: list[dict[str, Any]], excludes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按用户给的排除词剔除候选（子串匹配 name/full_name/description/topics，不区分大小写）。"""
    if not excludes:
        return candidates, []
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for project in candidates:
        repo = project["repo"]
        text = " ".join(
            str(part) for part in [repo.get("full_name"), repo.get("description"), " ".join(repo.get("topics") or [])]
        ).casefold()
        hit = next((term for term in excludes if term.casefold() in text), None)
        if hit:
            rejected.append({"full_name": repo["full_name"], "reason": f"excluded_by_user:{hit}", "stage": "exclude"})
        else:
            kept.append(project)
    return kept, rejected


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


def filter_candidates(
    candidates: list[dict[str, Any]],
    *,
    count: int,
    strict_relevance: bool = False,
    fresh_days: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    now = utc_now()
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
        if reason is None and fresh_days is not None:
            age = days_since(repo.get("created_at"), now)
            if repo.get("created_at") is None or age > fresh_days:
                reason = "too_old_for_fresh"
        if reason is None and token_coverage(project) < RELEVANCE_COVERAGE_THRESHOLD:
            # GitHub 词干化搜索放进来的跑题项目（如搜生图混入截图工具）：
            # 默认保留但打上复核标记，交给分析阶段说明；--strict-relevance 直接剔除。
            if strict_relevance:
                reason = "offtopic"
            else:
                project["flags"] = sorted(set(project.get("flags", [])) | {"needs_verification:possible_offtopic"})
        if reason:
            rejected.append({"full_name": repo["full_name"], "reason": reason, "stage": "filter"})
        else:
            kept.append(project)

    # 星数门槛：新发布项目短期内攒不到 20 星（7 天 10 星已经很热），
    # fresh 模式门槛从 20/5 降至 5/1，避免把真正的新热门项目在门槛阶段剔掉。
    if fresh_days is None:
        normal_floor, low_floor = 20, 5
    else:
        normal_floor, low_floor = 5, 1
    normal = [p for p in kept if p["repo"]["stars"] >= normal_floor]
    low = [p for p in kept if low_floor <= p["repo"]["stars"] < normal_floor]
    threshold = normal_floor
    low_sample = False
    if len(normal) < count:
        threshold = low_floor
        low_sample = True
        normal.extend(low)
    selected_ids = {p["repo"]["full_name"].casefold() for p in normal}
    for project in kept:
        if project["repo"]["full_name"].casefold() not in selected_ids:
            rejected.append({"full_name": project["repo"]["full_name"], "reason": "below_dynamic_threshold", "stage": "threshold"})
    return normal, rejected, threshold, low_sample


def detect_near_duplicates(filtered: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    """标记并移除明显是同一项目克隆/分叉的候选（相同描述+主题+语言，不同 full_name）。

    保守策略：仅在描述与主题集完全归一化一致、且主语言相同才算近重复，避免误伤。
    保留 Stars 更高的项目，其余移入落选列表。
    """
    def canonical(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for project in filtered:
        repo = project["repo"]
        key = (canonical(repo.get("description")), canonical(" ".join(repo.get("topics") or [])), canonical(repo.get("primary_language")))
        if not key[0]:
            continue
        groups.setdefault(key, []).append(project)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: p["repo"]["stars"], reverse=True)
        keeper = members[0]
        for dup in members[1:]:
            dup["flags"] = sorted(set(dup.get("flags", [])) | {"near_duplicate_of:" + keeper["repo"]["full_name"]})
            rejected.append({"full_name": dup["repo"]["full_name"], "reason": "near_duplicate", "stage": "dedupe", "duplicate_of": keeper["repo"]["full_name"]})
            filtered.remove(dup)


def _load_readme_cache(path: Path) -> None:
    global _README_CACHE, _README_CACHE_PATH
    _README_CACHE_PATH = path
    _README_CACHE = {}
    try:
        if path.is_file():
            payload = load_json(path)
            if isinstance(payload, dict):
                _README_CACHE = payload
    except (OSError, json.JSONDecodeError):
        _README_CACHE = {}


def _save_readme_cache() -> None:
    if _README_CACHE_PATH is None:
        return
    try:
        atomic_write_json(_README_CACHE_PATH, _README_CACHE)
    except OSError:
        pass


def _load_detail_cache(path: Path) -> None:
    global _DETAIL_CACHE, _DETAIL_CACHE_PATH
    _DETAIL_CACHE_PATH = path
    _DETAIL_CACHE = {}
    try:
        if path.is_file():
            payload = load_json(path)
            if isinstance(payload, dict):
                _DETAIL_CACHE = payload
    except (OSError, json.JSONDecodeError):
        _DETAIL_CACHE = {}


def _save_detail_cache() -> None:
    if _DETAIL_CACHE_PATH is None:
        return
    try:
        atomic_write_json(_DETAIL_CACHE_PATH, _DETAIL_CACHE)
    except OSError:
        pass


def _cached_detail(full_name: str, now: dt.datetime, *, allow_degraded: bool = True) -> dict[str, Any] | None:
    """命中且未过期（默认 24h）则返回缓存的 {repo_updates, latest_release, fetched_at}。

    匿名模式写入的缓存缺发版信息（allow_degraded=False 时视为未命中，认证模式下重新抓全量）。
    """
    entry = _DETAIL_CACHE.get(full_name)
    if not isinstance(entry, dict):
        return None
    if entry.get("anonymous") and not allow_degraded:
        return None
    fetched = parse_time(entry.get("fetched_at"))
    if not fetched or (now - fetched).total_seconds() > DETAIL_CACHE_TTL_HOURS * 3600:
        return None
    return entry


def _detail_repo_updates(details: dict[str, Any]) -> dict[str, Any]:
    license_obj = details.get("license") or {}
    return {
        "subscribers": details.get("subscribers_count"),
        "open_issues": int(details.get("open_issues_count") or 0),
        "license": {"spdx_id": license_obj.get("spdx_id") or "NOASSERTION", "name": license_obj.get("name")},
        "homepage": details.get("homepage"),
        "size_kb": details.get("size"),
        "default_branch": details.get("default_branch"),
    }


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


# 「默认偏新」软偏置：趋势/浏览类请求对近 30 天创建的新项目加权（半衰期 30 天），
# 避免老牌高星仓库（如 9 个月前的 openclaw）垄断"今天热门"类结果。
# --no-fresh-bias 可关闭；--fresh --fresh-days N 为硬门限（此时不再叠加软偏置）。
FRESH_BIAS_HALF_LIFE_DAYS = 30.0
FRESH_BIAS_WEIGHT = 0.15


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


def score_projects(projects: list[dict[str, Any]], *, baseline: dict[str, Any] | None, now: dt.datetime, days: int, fresh_bias: bool = True) -> str:
    baseline_map = snapshot_project_map(baseline)
    baseline_at = parse_time((baseline or {}).get("captured_at") or ((baseline or {}).get("run") or {}).get("data_as_of"))
    coverage = sum(1 for p in projects if p["repo"]["full_name"].casefold() in baseline_map) / max(len(projects), 1)
    historical_mode = bool(baseline_at and coverage >= 0.4)

    metrics: list[dict[str, float]] = []
    for project in projects:
        repo = project["repo"]
        activity = activity_score(project, now)
        freshness = freshness_completeness(project, now)
        bias = recency_score(days_since(repo.get("created_at"), now), FRESH_BIAS_HALF_LIFE_DAYS)
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
            metrics.append({"abs": float(star_delta), "rel": relative, "fork": float(fork_delta), "activity": activity, "freshness": freshness, "bias": bias})
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
            metrics.append({"maturity": math.log1p(repo["stars"]), "speed": math.log1p(repo["stars"] / age), "fork": math.log1p(repo["forks"]), "activity": activity, "freshness": freshness, "bias": bias})

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
        if fresh_bias:
            # 方案A「默认偏新」：heat 中混入 15% 的近 30 天新度分（半衰期衰减），
            # 让"今天热门"类结果天然偏新；老牌仓库仍可凭真实增长上榜，只是不再稳赢。
            bias_val = float(metric.get("bias", 0.0))
            breakdown["recency_boost"] = round1(bias_val)
            heat = (1.0 - FRESH_BIAS_WEIGHT) * heat + FRESH_BIAS_WEIGHT * bias_val
        activity_score_val = breakdown.get("maintenance_activity", 0.0)
        freshness_val = breakdown.get("freshness_completeness", 0.0)
        pre_recommendation = round1(0.45 * heat + 0.35 * activity_score_val + 0.20 * freshness_val)
        project["scores"] = {
            "heat": round1(heat),
            "technology": None,
            "community": None,
            "commercial": None,
            "recommendation": None,
            "breakdown": breakdown,
            "pre_recommendation": pre_recommendation,
            "activity": round1(activity_score_val),
            "freshness": round1(freshness_val),
        }
    return "snapshot_delta" if historical_mode else "cold_start_proxy"


def score_star_credibility(project: dict[str, Any], *, now: dt.datetime | None = None) -> list[str]:
    repo = project["repo"]
    flags: list[str] = []
    stars = max(repo["stars"], 1)
    if stars >= 500 and repo["forks"] / stars < 0.005 and repo["open_issues"] == 0:
        flags.append("needs_verification:low_fork_and_issue_signal")
    if stars >= 100 and not repo["description"] and not repo["topics"]:
        flags.append("needs_verification:incomplete_repository_profile")
    ref_now = now or utc_now()
    if days_since(repo.get("pushed_at"), ref_now) >= 365:
        flags.append("needs_verification:stale_no_recent_push")
    if repo.get("size_kb") is not None and repo["size_kb"] < 50 and stars >= 50:
        flags.append("needs_verification:thin_repository")
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
    anonymous = _is_anonymous()
    cached = _cached_detail(full_name, now, allow_degraded=anonymous)
    if cached:
        # 详情缓存命中（跨关键词共享，默认 24h）：仓库基本属性和最新发版很少天内变化，直接复用。
        project["repo"].update(cached.get("repo_updates") or {})
        if cached.get("latest_release") is not None:
            project["latest_release"] = cached["latest_release"]
        project["flags"] = sorted(set(project.get("flags", []) + ["detail_cache:hit"]))
    if anonymous:
        # 匿名模式：跳过 release/issues（配额留给 README）。repo 详情仍然要抓：
        # 单项目 1 个请求，可带回 subscribers(watch) 等卡片报告必需字段。
        project["flags"] = sorted(set(project.get("flags", []) + ["anonymous_quota:skipped_nonessential_details"]))
        project["recent_activity"] = {"window_days": 30, "issues_updated": None, "pull_requests_updated": None, "sample_capped": False, "degraded": "anonymous_quota"}
        if not cached:
            try:
                details = gh_json(endpoint, timeout=60)
                updates = _detail_repo_updates(details)
                project["repo"].update(updates)
                _DETAIL_CACHE[full_name] = {"fetched_at": iso_z(now), "repo_updates": updates, "latest_release": None, "anonymous": True}
            except GhError as exc:
                errors.append({"stage": "repo_details", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})
    else:
        if not cached:
            try:
                details = gh_json(endpoint, timeout=60)
                updates = _detail_repo_updates(details)
                project["repo"].update(updates)
            except GhError as exc:
                errors.append({"stage": "repo_details", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})
                updates = None
            release = None
            try:
                release = gh_json(f"{endpoint}/releases/latest", timeout=45, allow_404=True)
                if release:
                    project["latest_release"] = {"tag": release.get("tag_name"), "name": release.get("name"), "published_at": release.get("published_at"), "url": release.get("html_url")}
            except GhError as exc:
                errors.append({"stage": "release", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})
            if updates is not None:
                _DETAIL_CACHE[full_name] = {"fetched_at": iso_z(now), "repo_updates": updates, "latest_release": project.get("latest_release")}

        since = iso_z(now - dt.timedelta(days=30))
        try:
            issues = gh_json(f"{endpoint}/issues", fields={"state": "all", "since": since, "per_page": 100, "sort": "updated", "direction": "desc"}, timeout=60)
            issue_count = sum(1 for item in issues if "pull_request" not in item)
            pr_count = sum(1 for item in issues if "pull_request" in item)
            project["recent_activity"] = {"window_days": 30, "issues_updated": issue_count, "pull_requests_updated": pr_count, "sample_capped": len(issues) >= 100}
        except GhError as exc:
            errors.append({"stage": "issues_prs", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    try:
        cached = _README_CACHE.get(full_name)
        pushed = project["repo"].get("pushed_at")
        if cached and cached.get("pushed_at") == pushed and cached.get("readme"):
            project["readme"] = cached["readme"]
        else:
            readme = gh_raw(f"{endpoint}/readme", timeout=60, allow_404=True)
            if readme is not None:
                clipped = readme[:16000]
                excerpt = {
                    "source_url": f"https://github.com/{full_name}#readme",
                    "fetched_at": iso_z(now),
                    "content_excerpt": clipped,
                    "content_truncated": len(readme) > len(clipped),
                    "excerpt_sha256": hashlib.sha256(clipped.encode("utf-8")).hexdigest(),
                }
                project["readme"] = excerpt
                _README_CACHE[full_name] = {"pushed_at": pushed, "readme": excerpt}
    except GhError as exc:
        errors.append({"stage": "readme", "repo": full_name, "message": str(exc), "detail": exc.stderr[:500], "retryable": True})

    project["flags"] = sorted(set(project.get("flags", []) + score_star_credibility(project, now=now)))
    return errors


def enrich_projects(projects: list[dict[str, Any]], *, now: dt.datetime) -> list[dict[str, Any]]:
    """批量详情补全。认证模式适度并发（默认 4，GTS_MAX_WORKERS 可调）；匿名强制串行保配额。"""
    if not projects:
        return []
    workers = 1 if _is_anonymous() else min(len(projects), ENRICH_WORKERS)
    if workers <= 1:
        errors: list[dict[str, Any]] = []
        for project in projects:
            errors.extend(enrich_project(project, now=now))
        return errors
    from concurrent.futures import ThreadPoolExecutor  # 局部导入：纯 gh CLI 路径无需并发也就不加载

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sub_errors in pool.map(lambda p: enrich_project(p, now=now), projects):
            errors.extend(sub_errors)
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
    """分析模板：卡片式契约。one_liner 上卡面，details 四板块进展开区。

    不再包含任何数字评分字段（技术/社区/商业分均已废弃）。
    facts 保留证据纪律：claim + source_url，仅用于校验与 Markdown 附注，不上卡面。
    顶层带「_填写说明」防呆：明确 facts 必须是对象数组（曾因写成"文字: 链接"字符串返工）。
    """
    return {
        "_填写说明（填完后请删除本键）": {
            "one_liner": "一句大白话说清项目是干嘛的，≤80 字，会印在卡面上",
            "details": "四个板块全必填：explain=详细说明 / suitable=适合谁 / cautions=注意事项 / business=二次开发与商业化思路",
            "facts": "证据数组，每项必须是对象 {\"claim\": 结论, \"source_url\": https 链接}；不要写成 \"结论: 链接\" 这种字符串",
            "facts_示例": [{"claim": "该项目 8 月仍在发版", "source_url": "https://github.com/owner/repo/releases"}],
        },
        "projects": [
            {
                "full_name": item["repo"]["full_name"],
                "one_liner": "",
                "details": {"explain": "", "suitable": "", "cautions": "", "business": ""},
                "facts": [],
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


MAX_RUNS_PER_KEYWORD = 10
try:
    MAX_RUNS_PER_KEYWORD = max(1, int(os.environ.get("GTS_MAX_RUNS", MAX_RUNS_PER_KEYWORD)))
except (TypeError, ValueError):
    pass


def prune_runs(runs_dir: Path, *, keep: int = MAX_RUNS_PER_KEYWORD) -> list[str]:
    """删除过旧的 run 目录，保留最近 keep 个（至少 1 个，供追溯与问题排查）。

    只按目录名（UTC 时间戳）排序；快照对比不依赖 run 目录（读的是 snapshots/），
    因此清理旧 run 不影响增速数据。
    """
    if not runs_dir.is_dir():
        return []
    runs = sorted((d for d in runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    keep = max(1, keep)
    removed: list[str] = []
    for old in runs[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def latest_run_command(args: argparse.Namespace) -> int:
    """查询某关键词最近一次 run 的新鲜度，供采集前判断是否值得复用（省配额省时间）。

    输出 key=value 行：run_dir / age_minutes / finalized / projects / mode。
    退出码：0 = 在 max_age_minutes 内有新鲜 run；1 = 没有或已过期（可放心重新采集）。
    跳过 stale_snapshot_fallback 的降级 run（那不是实时数据，不应复用）。
    """
    keyword_slug = slugify(args.keyword)
    runs_dir = Path(args.output_root).expanduser().resolve() / keyword_slug / "runs"
    now = utc_now()
    if runs_dir.is_dir():
        for run in sorted((d for d in runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
            result_path = run / "result.json"
            if not result_path.is_file():
                continue
            try:
                payload = load_json(result_path)
            except (OSError, json.JSONDecodeError):
                continue
            run_meta = payload.get("run") or {}
            if run_meta.get("mode") == "stale_snapshot_fallback":
                continue
            completed = parse_time(run_meta.get("completed_at") or run_meta.get("started_at"))
            if not completed:
                continue
            age_minutes = (now - completed).total_seconds() / 60.0
            print(f"run_dir={run}")
            print(f"age_minutes={age_minutes:.0f}")
            print(f"finalized={str(bool(run_meta.get('finalized_at'))).lower()}")
            print(f"projects={len(payload.get('projects') or [])}")
            print(f"mode={run_meta.get('mode') or ''}")
            if age_minutes <= max(1, args.max_age_minutes):
                print(f"fresh_within={args.max_age_minutes}")
                return 0
            print(f"stale_older_than={args.max_age_minutes}")
            return 1
    print(f"NO_RUN:{args.keyword}")
    return 1


def _load_runs(runs_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """按时间倒序返回 (run 目录, result.json 内容)，跳过损坏文件。"""
    if not runs_dir.is_dir():
        return []
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for run in sorted((d for d in runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        result_path = run / "result.json"
        if not result_path.is_file():
            continue
        try:
            payload = load_json(result_path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("projects"):
            loaded.append((run, payload))
    return loaded


def watch_diff(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """对比两次 run 的结果，产出增量摘要（新上榜 / 掉榜 / 星数变化 / 状态变化）。

    状态变化包括：archived 归档、最新发版 tag 变化。星数变化按绝对差排序。
    """
    prev_map = {p["repo"]["full_name"]: p for p in previous.get("projects") or []}
    new_map = {p["repo"]["full_name"]: p for p in current.get("projects") or []}
    new_entries = sorted(n for n in new_map if n not in prev_map)
    dropped = sorted(n for n in prev_map if n not in new_map)
    star_moves: list[dict[str, Any]] = []
    status_changes: list[dict[str, Any]] = []
    for name, new_project in new_map.items():
        old_project = prev_map.get(name)
        if old_project is None:
            continue
        old_repo, new_repo = old_project["repo"], new_project["repo"]
        old_stars, new_stars = int(old_repo.get("stars") or 0), int(new_repo.get("stars") or 0)
        if new_stars != old_stars:
            star_moves.append({"full_name": name, "from": old_stars, "to": new_stars, "delta": new_stars - old_stars})
        if bool(old_repo.get("archived")) != bool(new_repo.get("archived")):
            status_changes.append({"full_name": name, "change": "archived" if new_repo.get("archived") else "unarchived"})
        old_tag = (old_project.get("latest_release") or {}).get("tag")
        new_tag = (new_project.get("latest_release") or {}).get("tag")
        if new_tag and new_tag != old_tag:
            status_changes.append({"full_name": name, "change": "new_release", "from": old_tag, "to": new_tag})
    star_moves.sort(key=lambda m: abs(m["delta"]), reverse=True)
    return {
        "new_entries": new_entries,
        "dropped": dropped,
        "star_moves": star_moves,
        "status_changes": status_changes,
    }


def watch_command(args: argparse.Namespace) -> int:
    """订阅式增量采集：重新 collect 后与上一次 run 对比，只输出变化部分。

    适合配合宿主的定时自动化：每次定时触发 watch，输出增量摘要（新上榜/掉榜/星数异动/状态变化），
    无变化时明确说"无变化"，不重复推送全文报告。
    """
    keyword_slug = slugify(args.keyword)
    runs_dir = Path(args.output_root).expanduser().resolve() / keyword_slug / "runs"
    baseline_runs = _load_runs(runs_dir)
    existing = {run.name for run, _ in baseline_runs}
    baseline: dict[str, Any] | None = None
    baseline_dir: Path | None = None
    for run, payload in baseline_runs:
        if (payload.get("run") or {}).get("mode") != "stale_snapshot_fallback":
            baseline, baseline_dir = payload, run
            break
    if baseline is None and baseline_runs:
        baseline_dir, baseline = baseline_runs[0]

    rc = collect_command(args)
    if rc != 0:
        return rc

    current_dir: Path | None = None
    current: dict[str, Any] | None = None
    for run, payload in _load_runs(runs_dir):
        if run.name not in existing:
            current_dir, current = run, payload
            break
    if current is None:
        # 新 run 可能本期 0 个项目（如 --fresh 把结果过滤清空）：_load_runs 会跳过空 projects，
        # 这里直接扫新目录读 result.json，给出明确提示而不是误导性的"找不到新 run 目录"。
        for run in sorted((d for d in runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
            if run.name in existing:
                break  # 时间倒序，遇到旧目录即可停
            result_path = run / "result.json"
            if not result_path.is_file():
                continue
            try:
                payload = load_json(result_path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                current_dir, current = run, payload
                break
    if current is None or current_dir is None:
        print("ERROR: watch 采集完成但找不到新 run 目录。", file=sys.stderr)
        return 2
    if not (current.get("projects") or []):
        print(f"[watch] 本期「{args.keyword}」0 个项目入选（可能因 --fresh 新度门限或搜索结果为空）；已写入 {current_dir}。")
        print("[watch] 若长期为 0，建议放宽 --fresh-days 或去掉 --fresh 再跑。")
    if baseline is None:
        print(f"[watch] 首次监控「{args.keyword}」：本期已建立基线（{len(current.get('projects') or [])} 个项目）。")
        print("[watch] 下次再运行 watch 时，将输出与本次对比的增量变化。")
        print(current_dir)
        return 0

    baseline_fresh = (baseline.get("input") or {}).get("fresh_days")
    current_fresh = (current.get("input") or {}).get("fresh_days")
    if baseline_fresh != current_fresh:
        print(f"[watch][提示] 基线与本期的「新度门限」不一致（基线 fresh_days={baseline_fresh}，本期 fresh_days={current_fresh}）：掉榜可能只是老项目被门限过滤，不一定是热度下降。", file=sys.stderr)
    if bool((baseline.get("input") or {}).get("fresh_bias")) != bool((current.get("input") or {}).get("fresh_bias")):
        print("[watch][提示] 基线与本期的「默认偏新软偏置」开关不一致：上榜/掉榜可能只是排序口径变化，不一定是热度变化。", file=sys.stderr)

    diff = watch_diff(baseline, current)
    prev_time = (baseline.get("run") or {}).get("completed_at") or ""
    run_meta = current.get("run") or {}
    summary_lines = [
        f"# watch 增量摘要：{args.keyword}",
        "",
        f"- 基线：{baseline_dir.name}（{prev_time}）",
        f"- 本期：{current_dir.name}（{run_meta.get('completed_at') or ''}，模式 {run_meta.get('mode')}）",
        "",
        f"## 新上榜（{len(diff['new_entries'])}）",
    ]
    summary_lines.extend(f"- {name}" for name in diff["new_entries"])
    if not diff["new_entries"]:
        summary_lines.append("- 无")
    summary_lines.append(f"\n## 掉榜（{len(diff['dropped'])}）")
    summary_lines.extend(f"- {name}" for name in diff["dropped"])
    if not diff["dropped"]:
        summary_lines.append("- 无")
    summary_lines.append(f"\n## 星数变化（{len(diff['star_moves'])}）")
    summary_lines.extend(f"- {m['full_name']}：{m['from']} → {m['to']}（{'+' if m['delta'] >= 0 else ''}{m['delta']}）" for m in diff["star_moves"])
    if not diff["star_moves"]:
        summary_lines.append("- 无")
    summary_lines.append(f"\n## 状态变化（{len(diff['status_changes'])}）")
    label = {"archived": "已归档（官方停摆）", "unarchived": "取消归档", "new_release": "发了新版本"}
    for change in diff["status_changes"]:
        extra = f"：{change.get('from') or '无'} → {change['to']}" if change["change"] == "new_release" else ""
        summary_lines.append(f"- {change['full_name']}：{label.get(change['change'], change['change'])}{extra}")
    if not diff["status_changes"]:
        summary_lines.append("- 无")
    summary = "\n".join(summary_lines)
    atomic_write_text(current_dir / "watch-summary.md", summary + "\n")
    atomic_write_json(current_dir / "watch-summary.json", {"baseline_run": baseline_dir.name, "current_run": current_dir.name, **diff})

    has_change = bool(diff["new_entries"] or diff["dropped"] or diff["star_moves"] or diff["status_changes"])
    headline = "有变化" if has_change else "与上次相比无变化"
    print(f"[watch] {args.keyword}：{headline}（基线 {baseline_dir.name} → 本期 {current_dir.name}）")
    print(summary)
    print(f"[watch] 增量摘要已写入：{current_dir / 'watch-summary.md'}")
    print(current_dir)
    return 0


def collect_command(args: argparse.Namespace) -> int:
    started = utc_now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    keyword_slug = slugify(args.keyword)
    root = Path(args.output_root).expanduser().resolve() / keyword_slug
    run_dir = root / "runs" / run_id
    snapshot_dir = root / "snapshots"
    _load_readme_cache(root / "cache" / "readme.json")
    _load_detail_cache(root / "cache" / "repo-details.json")
    errors: list[dict[str, Any]] = []
    global TRANSPORT
    if getattr(args, "transport", "auto") != "auto":
        TRANSPORT = args.transport
    terms = normalize_terms(args.keyword, args.term or [])
    fresh_days = None
    if getattr(args, "fresh", False):
        fresh_days = int(getattr(args, "fresh_days", FRESH_DEFAULT_DAYS) or FRESH_DEFAULT_DAYS)
    # 默认偏新软偏置：未启用 --fresh 硬门限、且用户未显式 --no-fresh-bias 时开启
    fresh_bias = fresh_days is None and not bool(getattr(args, "no_fresh_bias", False))
    plan = build_query_plan(args.keyword, terms, days=args.days, language=args.language, now=started, fresh_days=fresh_days)
    excludes = [re.sub(r"\s+", " ", e.strip()) for e in (getattr(args, "exclude", None) or []) if e.strip()]

    try:
        auth_hint = check_auth()
        if auth_hint:
            print(f"[提示] {auth_hint}", file=sys.stderr)
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
    candidates, exclude_rejected = apply_excludes(candidates, excludes)
    rejected = list(exclude_rejected)
    if exclude_rejected:
        print(f"[提示] 已按 --exclude 剔除 {len(exclude_rejected)} 个候选：{', '.join(item['full_name'] for item in exclude_rejected)}", file=sys.stderr)
    strict_relevance = bool(getattr(args, "strict_relevance", False))
    if fresh_days is not None:
        print(f"[提示] 新度门限已启用：全部搜索池已注入 created:>=（近 {fresh_days} 天创建），星数门槛同步放宽（新项目短期攒不到 20 星）。", file=sys.stderr)
    if fresh_bias:
        print(f"[提示] 默认偏新软偏置已启用：热度分混入 {int(FRESH_BIAS_WEIGHT * 100)}% 的近 {int(FRESH_BIAS_HALF_LIFE_DAYS)} 天新度分，结果天然偏新发布；如需纯热度老牌榜可用 --no-fresh-bias，如需只看新发布请用 --fresh。", file=sys.stderr)
    filtered, filter_rejected, threshold, low_sample = filter_candidates(candidates, count=args.count, strict_relevance=strict_relevance, fresh_days=fresh_days)
    rejected.extend(filter_rejected)
    offtopic_flagged = [p for p in filtered if "needs_verification:possible_offtopic" in p.get("flags", [])]
    if offtopic_flagged:
        names = ', '.join(p["repo"]["full_name"] for p in offtopic_flagged)
        if strict_relevance:
            print(f"[提示] 严格相关度模式：已剔除疑似跑题项目（{names}）。", file=sys.stderr)
        else:
            print(f"[提示] {len(offtopic_flagged)} 个项目疑似跑题（关键词分词覆盖不足半数）：{names}；已打标 needs_verification:possible_offtopic，分析时请说明其与主题的关系，或建议用户下次用 --exclude 排除。", file=sys.stderr)
    detect_near_duplicates(filtered, rejected)
    baseline = find_baseline(snapshot_dir, keyword=args.keyword, now=started, days=args.days)

    score_projects(filtered, baseline=baseline, now=started, days=args.days, fresh_bias=fresh_bias)
    filtered.sort(key=lambda item: (item["scores"]["heat"], item["relevance_score"], item["repo"]["stars"]), reverse=True)
    shortlist_size = min(len(filtered), args.count)
    shortlist = filtered[:shortlist_size]
    detail_cache_hits = sum(1 for p in shortlist if _cached_detail(p["repo"]["full_name"], started, allow_degraded=_is_anonymous()) is not None)
    enrich_started = time.monotonic()
    errors.extend(enrich_projects(shortlist, now=started))
    enrich_seconds = round(time.monotonic() - enrich_started, 1)
    if detail_cache_hits:
        print(f"[提示] {detail_cache_hits}/{len(shortlist)} 个项目的详情命中缓存（{DETAIL_CACHE_TTL_HOURS:.0f}h 内跨关键词共享），本次未重复抓取。", file=sys.stderr)
    mode = score_projects(shortlist, baseline=baseline, now=started, days=args.days, fresh_bias=fresh_bias)
    shortlist.sort(key=lambda item: (item["scores"]["heat"], item["relevance_score"], item["repo"]["stars"]), reverse=True)
    top = shortlist[: args.count]
    pre_order = sorted(top, key=lambda p: (p["scores"].get("pre_recommendation", 0.0), p["scores"]["heat"]), reverse=True)

    pool_ok = all(successes.get(pool, 0) > 0 for pool in POOL_QUOTAS)
    core_fields_ok = bool(top) and all(p["repo"]["full_name"] and p["repo"]["stars"] >= 0 and p["repo"]["forks"] >= 0 and p["repo"]["created_at"] for p in top)
    valid_snapshot = pool_ok and core_fields_ok
    limitations: list[str] = []
    anonymous_mode = _is_anonymous()
    if anonymous_mode:
        limitations.append("匿名模式（未配置 token）：使用 GitHub 未认证配额，已跳过 release / 近 30 天 Issue-PR 活跃度等非关键详情；配置 GH_TOKEN 可获得完整数据。")
    if mode == "cold_start_proxy":
        limitations.append("没有合格历史快照；热度为首次运行代理分，不代表真实近 7 天增长。")
    if low_sample:
        limitations.append(f"结果不足以维持星数门槛，已降至 {threshold} Stars；低样本项目置信度需下调。")
    if errors:
        limitations.append("部分补充字段请求失败；请查看 errors.json。")
    if not valid_snapshot:
        limitations.append("关键候选池或字段不完整，本次不会写入有效快照。")

    completed = utc_now()
    auth_route = "direct_gh" if _active_transport() == "gh" else ("direct_api_anonymous" if anonymous_mode else "direct_api_token")
    result = {
        "schema_version": SCHEMA_VERSION,
        "run": {"id": run_id, "started_at": iso_z(started), "completed_at": iso_z(completed), "mode": mode, "data_as_of": iso_z(completed), "auth_route": auth_route, "anonymous": anonymous_mode, "is_valid_snapshot": valid_snapshot},
        "input": {"keyword": args.keyword, "time_range_days": args.days, "count": args.count, "language": args.language, "exclude_terms": excludes, "strict_relevance": strict_relevance, "fresh_days": fresh_days, "fresh_bias": fresh_bias, "repository_types": ["application", "tool", "framework", "library", "model", "dataset"]},
        "queries": [{"text": item["query"], "kind": item["pool"], "term": item["term"], "term_weight": item["term_weight"], "sort": item["sort"], "limit": item["limit"]} for item in plan],
        "collection": {"candidate_count": len(candidates), "filtered_count": len(filtered), "shortlist_count": len(shortlist), "dynamic_star_threshold": threshold, "low_sample": low_sample, "pool_successes": successes, "degraded": False, "detail_cache_hits": detail_cache_hits, "enrich_seconds": enrich_seconds},
        "rankings": {"heat": [p["repo"]["full_name"] for p in top], "recommendation": [], "pre_analysis_recommendation": [p["repo"]["full_name"] for p in pre_order]},
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
    _save_readme_cache()
    _save_detail_cache()
    removed = prune_runs(root / "runs")
    if removed:
        print(f"[提示] 已自动清理 {len(removed)} 个旧 run 目录（每关键词保留最近 {MAX_RUNS_PER_KEYWORD} 个；快照对比不受影响）。", file=sys.stderr)
    if mode == "cold_start_proxy":
        print("[提示] 首次运行没有历史快照，本期热度为代理分；下次对同一关键词再跑一次，即可获得与本期对比的真实增速数据。", file=sys.stderr)
    print(str(run_dir))
    return 0


def validate_score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoutError(f"{field} 必须是 0–100 的数字。")
    if not 0 <= float(value) <= 100:
        raise ScoutError(f"{field} 超出 0–100。")
    return round1(float(value))


DETAIL_KEYS = ("explain", "suitable", "cautions", "business")


def validate_analysis(analysis: dict[str, Any], expected: set[str]) -> dict[str, dict[str, Any]]:
    projects = analysis.get("projects")
    if not isinstance(projects, list):
        raise ScoutError("analysis.projects 必须是数组。")
    mapped: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    problems: list[str] = []
    for item in projects:
        name = str(item.get("full_name") or "")
        key = name.casefold()
        if key not in expected:
            problems.append(f"未知项目「{name}」：不在本次采集名单里，full_name 需与 analysis-template.json 完全一致")
            continue
        if key in seen:
            problems.append(f"「{name}」重复出现：同一项目只能有一条分析，请删除多余条目")
            continue
        seen.add(key)
        item_problems: list[str] = []
        one_liner = str(item.get("one_liner") or "").strip()
        if not one_liner:
            item_problems.append(f"「{name}」one_liner 为空：需要一句通俗易懂的话说明项目是干嘛的")
        elif len(one_liner) > 80:
            item_problems.append(f"「{name}」one_liner 超长：当前 {len(one_liner)} 字（限 80 字，需再删 {len(one_liner) - 80} 字），细节请移到 details")
        details = item.get("details")
        if not isinstance(details, dict):
            item_problems.append(f"「{name}」details 缺失：必须是对象，含 {'、'.join(DETAIL_KEYS)} 四个板块")
        else:
            missing_fields = [f for f in DETAIL_KEYS if not str(details.get(f) or "").strip()]
            if missing_fields:
                item_problems.append(f"「{name}」details 空板块：{', '.join(missing_fields)} 还没填写")
        for idx, fact in enumerate(item.get("facts", []) or [], start=1):
            if isinstance(fact, str):
                problems.append(
                    f"「{name}」facts 第 {idx} 项是字符串：必须改成对象格式 "
                    '{"claim": "结论文字", "source_url": "https://..."}；'
                    f'例如 "{fact[:30]}..." 应拆成 claim 和 source_url 两个字段'
                )
            elif not isinstance(fact, dict) or not fact.get("claim") or not str(fact.get("source_url") or "").startswith(("https://", "http://")):
                problems.append(
                    f"「{name}」facts 第 {idx} 项无效：每项需是对象，含 claim（结论）+ source_url（https:// 开头的来源链接），"
                    '例如 {"claim": "该项目 8 月仍在发版", "source_url": "https://github.com/x/y/releases"}'
                )
        problems.extend(item_problems)
        if not item_problems:
            mapped[key] = item
    missing = expected - seen
    if missing:
        problems.append(f"缺少项目分析：{', '.join(sorted(missing))}（每个采集到的项目都必须有分析）")
    if problems:
        raise ScoutError("analysis.json 校验未通过，共 {} 处问题：\n  {}".format(len(problems), "\n  ".join(f"{i}. {p}" for i, p in enumerate(problems, 1))))
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
    """精简 Markdown 报告：与卡片报告同构，无数字评分、无双榜、无大段总结。

    主交付物是 report.html（卡片式）；本 Markdown 供纯命令行环境快速浏览。
    """
    projects = result["projects"]
    by_name = {p["repo"]["full_name"]: p for p in projects}
    order = result["rankings"].get("recommendation") or []
    # 热榜模式没有 keyword（input 只含 mode/window/language/count/source），
    # 不能用 result["input"]["keyword"] 硬取值，否则 finalize 会 KeyError 崩溃。
    inp = result.get("input") or {}
    run_mode = (result.get("run") or {}).get("mode")
    if run_mode == "trending":
        window_cn = {"daily": "当日", "weekly": "本周", "monthly": "本月"}.get(inp.get("window"), "当期")
        title = f"# GitHub {window_cn}热榜 · 最火的 {len(order)} 个项目"
    else:
        keyword = inp.get("keyword") or "GitHub 趋势"
        title = f"# GitHub 趋势报告：{keyword} 方向最值得看的 {len(order)} 个项目"
    lines = [title, ""]
    for rank, name in enumerate(order, start=1):
        project = by_name[name]
        repo = project["repo"]
        detail = project["analysis"]
        license_id = (repo.get("license") or {}).get("spdx_id") or "NOASSERTION"
        watchers = repo.get("subscribers")
        watch_text = f"｜Watch {watchers}" if isinstance(watchers, (int, float)) else ""
        lines.extend(
            [
                f"## {rank}. [{name}]({repo['url']})",
                "",
                detail.get("one_liner") or "",
                "",
                f"- 数据：{repo['stars']} Stars｜{repo['forks']} Forks{watch_text}｜{repo.get('primary_language') or '未知语言'}｜{license_id}｜最近 Push {repo.get('pushed_at') or '—'}",
                "",
                f"**详细说明**：{detail['details']['explain']}",
                "",
                f"**适合谁**：{detail['details']['suitable']}",
                "",
                f"**注意事项**：{detail['details']['cautions']}",
                "",
                f"**二次开发 / 商业化**：{detail['details']['business']}",
                "",
            ]
        )
        facts = detail.get("facts") or []
        if facts:
            lines.append("**事实依据**")
            lines.append("")
            lines.append(bullet_lines(facts))
            lines.append("")
    return chr(10).join(lines) + chr(10)


def _write_rankings_csv(run_dir: Path, result: dict[str, Any]) -> None:
    projects = result["projects"]
    by_name = {p["repo"]["full_name"]: p for p in projects}
    order = (
        result["rankings"].get("recommendation")
        or result["rankings"].get("pre_analysis_recommendation")
        or result["rankings"].get("heat")
        or []
    )
    path = run_dir / "rankings.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "full_name", "url", "stars", "forks", "watchers", "language", "license", "pushed_at", "one_liner"])
        for index, name in enumerate(order, start=1):
            project = by_name.get(name)
            if not project:
                continue
            repo = project["repo"]
            analysis = project.get("analysis") or {}
            writer.writerow([
                index,
                name,
                repo.get("url", ""),
                repo.get("stars"),
                repo.get("forks"),
                repo.get("subscribers"),
                repo.get("primary_language") or "",
                (repo.get("license") or {}).get("spdx_id") or "",
                repo.get("pushed_at") or "",
                analysis.get("one_liner", ""),
            ])


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
        project["analysis"] = {key: value for key, value in item.items() if key != "full_name"}
    # 排序：热榜沿用 GitHub Trending 名次；其余由脚本的数据指标决定（预分析推荐分 > 热度）。
    if result.get("run", {}).get("mode") == "trending":
        result["rankings"]["recommendation"] = [
            p["repo"]["full_name"]
            for p in sorted(result["projects"], key=lambda p: p.get("trending_rank") or 0)
        ]
    else:
        result["rankings"]["recommendation"] = [
            p["repo"]["full_name"]
            for p in sorted(result["projects"], key=lambda p: (p["scores"].get("pre_recommendation") or 0, p["scores"].get("heat") or 0), reverse=True)
        ]
    result["run"]["finalized_at"] = iso_z(utc_now())
    atomic_write_json(result_path, result)
    atomic_write_text(run_dir / "report.html", _render_card_report(result))
    if getattr(args, "keep_extra", False):
        # 默认只出卡片 HTML；需要 Markdown / CSV 时再开此开关。
        atomic_write_text(run_dir / "report.md", render_report(result, analysis))
        _write_rankings_csv(run_dir, result)
    else:
        # 用户只要卡片 HTML：不生成 report.md / rankings.csv，并清理历史残留。
        for extra in ("report.md", "rankings.csv"):
            extra_path = run_dir / extra
            if extra_path.exists():
                extra_path.unlink()
    print(str(run_dir / "report.html"))
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


TRENDING_WINDOWS = {"daily": 1, "weekly": 7, "monthly": 30}
TRENDING_MIN_STARS = {"daily": 10, "weekly": 50, "monthly": 200}
FRESH_DEFAULT_DAYS = 30
GITHUB_TRENDING_URL = "https://github.com/trending"


def _tag_text(block: str, anchor_substr: str) -> str:
    """取出 block 中某个 <a href="...anchor...">...</a> 的纯文本（剥离标签），避免误抓 svg 的 viewBox 数字。"""
    match = re.search(r'<a[^>]*href="[^"]*' + re.escape(anchor_substr) + r'"[^>]*>(.*?)</a>', block, re.S)
    if not match:
        return ""
    return re.sub(r"<[^>]+>", "", match.group(1))


def _parse_count(text: str) -> int:
    """解析 star/forks 文本：支持 28,094 与 16.1k / 1.2m 缩写。"""
    text = text.strip().replace(",", "")
    match = re.match(r"([\d.]+)\s*([kKmM]?)", text)
    if not match:
        return 0
    value = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def scrape_github_trending(window: str, *, language: str | None) -> list[dict[str, Any]]:
    """抓取 github.com/trending 页面，原样照搬 GitHub 当日/周/月热榜。

    返回每项：full_name, url, description, primary_language, stars, forks,
    stars_period, stars_period_label。GitHub 官方 Trending 没有 API，只能抓页面；
    失败时抛 ScoutError，交由上层回退到搜索 API 近似榜。
    """
    params: list[tuple[str, str]] = []
    if window in TRENDING_WINDOWS:
        params.append(("since", window))
    if language:
        # 注意：--language 是**编程语言**维度（python/rust），必须用 ?language= 参数；
        # ?spoken_language_code= 是自然语言维度（zh/en），语义完全不同（Bug 修复：曾误用）。
        params.append(("language", language))
    url = GITHUB_TRENDING_URL
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; GitHubTrendScout/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html_text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise ScoutError(f"抓取 github.com/trending 失败：{exc}")
    if "<article" not in html_text:
        raise ScoutError("抓到的页面不是 GitHub Trending（可能被限流或结构已变化）。")
    blocks = re.findall(r'<article class="Box-row">(.*?)</article>', html_text, re.S)
    repos: list[dict[str, Any]] = []
    for block in blocks:
        match = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', block)
        if not match:
            continue
        full_name = match.group(1).strip("/")
        if full_name.count("/") != 1:
            continue
        # 注意：描述段是 <p class="col-9 ...">，不能用 <p[^>]*> 否则会误匹配 SVG 的 <path> 标签。
        desc_match = re.search(r'<p class="col-9[^"]*">(.*?)</p>', block, re.S)
        if desc_match:
            description = re.sub(r"<[^>]+>", "", desc_match.group(1))
            description = re.sub(r"\s+", " ", description).strip()
            description = html.unescape(description)
        else:
            description = ""
        stars = _parse_count(_tag_text(block, "/stargazers"))
        forks = _parse_count(_tag_text(block, "/forks"))
        lang_match = re.search(r'itemprop="programmingLanguage">([^<]+)</span>', block)
        primary_language = lang_match.group(1).strip() if lang_match else None
        block_text = re.sub(r"<[^>]+>", "", block)
        period_match = re.search(r"([\d,]+)\s+stars\s+(today|this week|this month)", block_text)
        repos.append(
            {
                "full_name": full_name,
                "url": f"https://github.com/{full_name}",
                "description": description,
                "primary_language": primary_language,
                "stars": stars,
                "forks": forks,
                "stars_period": int(period_match.group(1).replace(",", "")) if period_match else None,
                "stars_period_label": period_match.group(2) if period_match else None,
            }
        )
    if not repos:
        raise ScoutError("未能从 Trending 页面解析出任何仓库（页面结构可能已变化）。")
    return repos


def _trending_fallback_repos(window: str, *, language: str | None, count: int) -> list[dict[str, Any]]:
    """搜索 API 近似榜（抓取失败时的兜底）：高星 + 窗口内活跃。"""
    query = trending_query(window, language=language, now=utc_now())
    payload = gh_json(
        "search/repositories",
        fields={"q": query, "sort": "stars", "order": "desc", "per_page": min(count, 100)},
        timeout=60,
    )
    items = (payload or {}).get("items") or []
    return [
        {
            "full_name": item.get("full_name") or "",
            "url": item.get("html_url") or "",
            "description": item.get("description") or "",
            "primary_language": item.get("language"),
            "stars": int(item.get("stargazers_count") or 0),
            "forks": int(item.get("forks_count") or 0),
            "stars_period": None,
            "stars_period_label": None,
        }
        for item in items
    ]


def _trending_detail(full_name: str, *, now: dt.datetime) -> tuple[dict[str, Any], bool]:
    """热榜逐库补抓创建/更新时间、许可证等（用于卡片完整度与年龄徽章）。

    走 24h 详情缓存（热榜仓库日复一日高度重复，跨 run 命中率高）；
    缓存条目里以 `trending_detail` 键存热榜专用字段（含 created_at）。
    失败返回 ({}, False)，交由上层静默降级（卡片缺徽章但流程不中断）。
    """
    cached = _DETAIL_CACHE.get(full_name)
    if isinstance(cached, dict):
        fetched = parse_time(cached.get("fetched_at"))
        stored = cached.get("trending_detail")
        if fetched and isinstance(stored, dict) and (now - fetched).total_seconds() <= DETAIL_CACHE_TTL_HOURS * 3600:
            return stored, True
    try:
        item = gh_json(f"repos/{full_name}", timeout=30)
    except GhError:
        return {}, False
    if not isinstance(item, dict):
        return {}, False
    license_obj = item.get("license") or {}
    detail = {
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "pushed_at": item.get("pushed_at"),
        "license": {"spdx_id": license_obj.get("spdx_id") or "NOASSERTION", "name": license_obj.get("name")},
        "homepage": item.get("homepage"),
        "default_branch": item.get("default_branch"),
        "subscribers": int(item.get("subscribers_count") or 0),
        "open_issues": int(item.get("open_issues_count") or 0),
    }
    entry = dict(cached) if isinstance(cached, dict) else {}
    entry["fetched_at"] = iso_z(now)
    entry["trending_detail"] = detail
    _DETAIL_CACHE[full_name] = entry
    return detail, False


def _readme_excerpt_for(full_name: str, *, now: dt.datetime) -> dict[str, Any] | None:
    """取仓库 README 摘要（带缓存），供分析阶段参考；失败/无 README 返回 None。"""
    cached = _README_CACHE.get(full_name)
    if cached and cached.get("readme"):
        return cached["readme"]
    try:
        readme = gh_raw(f"repos/{full_name}/readme", timeout=60, allow_404=True)
    except GhError:
        return None
    if readme is None:
        return None
    clipped = readme[:16000]
    excerpt = {
        "source_url": f"https://github.com/{full_name}#readme",
        "fetched_at": iso_z(now),
        "content_excerpt": clipped,
        "content_truncated": len(readme) > len(clipped),
        "excerpt_sha256": hashlib.sha256(clipped.encode("utf-8")).hexdigest(),
    }
    # 热榜路径没有可靠的 pushed_at 对比依据，直接按 24h 级别缓存（README 一天内基本不变）。
    _README_CACHE[full_name] = {"pushed_at": None, "readme": excerpt}
    return excerpt


def _trending_enrich_project(project: dict[str, Any], *, now: dt.datetime, want_readme: bool) -> tuple[bool, bool]:
    """就地补全单个热榜项目（详情 + README）。返回 (详情缓存命中, 详情失败)。"""
    repo = project["repo"]
    detail, from_cache = _trending_detail(repo["full_name"], now=now)
    if detail:
        repo["primary_language"] = repo.get("primary_language") or detail.get("primary_language")
        repo["created_at"] = repo.get("created_at") or detail.get("created_at")
        repo["updated_at"] = repo.get("updated_at") or detail.get("updated_at")
        repo["pushed_at"] = repo.get("pushed_at") or detail.get("pushed_at")
        repo["subscribers"] = detail.get("subscribers")
        repo["open_issues"] = detail.get("open_issues") or 0
        repo["license"] = detail.get("license") or repo.get("license")
        repo["homepage"] = detail.get("homepage")
        repo["default_branch"] = detail.get("default_branch")
    if want_readme:
        project["readme"] = _readme_excerpt_for(repo["full_name"], now=now)
    return from_cache, not bool(detail)


def _trending_enrich_all(projects: list[dict[str, Any]], *, now: dt.datetime, want_readme: bool) -> tuple[int, int]:
    """批量补全热榜项目。认证模式适度并发（默认 4，GTS_MAX_WORKERS 可调）；匿名串行保配额。

    返回 (详情缓存命中数, 详情失败数)。
    """
    hits = 0
    failed = 0
    if not projects:
        return hits, failed
    workers = 1 if _is_anonymous() else min(len(projects), ENRICH_WORKERS)

    def _work(project: dict[str, Any]) -> tuple[bool, bool]:
        return _trending_enrich_project(project, now=now, want_readme=want_readme)

    if workers <= 1:
        outcomes = [_work(p) for p in projects]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(_work, projects))
    for from_cache, failed_one in outcomes:
        hits += 1 if from_cache else 0
        failed += 1 if failed_one else 0
    return hits, failed


def trending_query(window: str, *, language: str | None, now: dt.datetime) -> str:
    """搜索 API 近似榜查询（热榜抓取失败时的兜底）：窗口内有推送 + 星数下限，按星数排序。"""
    days = TRENDING_WINDOWS[window]
    cutoff = (now - dt.timedelta(days=days)).date().isoformat()
    pieces = [f"stars:>={TRENDING_MIN_STARS[window]}", f"pushed:>={cutoff}", "archived:false", "fork:false"]
    if language:
        pieces.append(f'language:"{language.replace(chr(34), "")}"')
    return " ".join(pieces)


def trending_command(args: argparse.Namespace) -> int:
    """热榜模式：抓取 github.com/trending 原样照搬 GitHub 热门榜（默认当天），
    逐库做完整分析并出卡片报告（不打我们的排名/打分）。

    抓取失败则回退到搜索 API 近似榜（结果标注「近似」）。本模式不自己发现/排名，
    排名完全沿用 GitHub Trending 顺序；我们负责提取内容 + 渲染成卡片。
    """
    global TRANSPORT
    if getattr(args, "transport", "auto") != "auto":
        TRANSPORT = args.transport
    started = utc_now()
    window = args.window
    count = args.count
    language = args.language
    trending_root = Path(args.output_root).expanduser().resolve() / "_trending"
    # 热榜专用缓存（详情 24h / README）：热榜仓库日复一日高度重复，跨 run 命中率高
    _load_readme_cache(trending_root / "cache" / "readme.json")
    _load_detail_cache(trending_root / "cache" / "repo-details.json")
    auth_hint = check_auth()
    if auth_hint:
        print(f"[提示] {auth_hint}", file=sys.stderr)
    degraded = False
    try:
        repos = scrape_github_trending(window, language=language)
    except ScoutError as exc:
        print(f"[提示] 抓取 Trending 页面失败，回退到搜索 API 近似榜：{exc}", file=sys.stderr)
        degraded = True
        repos = _trending_fallback_repos(window, language=language, count=count)
    repos = repos[:count]
    projects: list[dict[str, Any]] = []
    for rank, meta in enumerate(repos, start=1):
        owner, _, name = meta["full_name"].partition("/")
        repo = {
            "id": None,
            "full_name": meta["full_name"],
            "name": name,
            "owner": owner,
            "url": meta["url"],
            "description": meta.get("description") or "",
            "topics": [],
            "primary_language": meta.get("primary_language"),
            "created_at": None,
            "updated_at": None,
            "pushed_at": meta.get("pushed_at"),
            "stars": int(meta.get("stars") or 0),
            "forks": int(meta.get("forks") or 0),
            "subscribers": None,
            "open_issues": 0,
            "archived": False,
            "disabled": False,
            "is_fork": False,
            "is_mirror": False,
            "upstream": None,
            "license": {"spdx_id": "NOASSERTION", "name": None},
            "homepage": None,
            "size_kb": None,
            "default_branch": None,
        }
        projects.append(
            {
                "repo": repo,
                "candidate_pools": ["trending"],
                "matched_terms": [],
                "relevance_score": None,
                "recent_activity": {},
                "latest_release": None,
                "readme": None,
                "trend": {
                    "mode": "github_trending" if not degraded else "trending_api_approx",
                    "stars_period": meta.get("stars_period"),
                    "stars_period_label": meta.get("stars_period_label"),
                },
                "scores": {},
                "analysis": None,
                "flags": [],
                "trending_rank": rank,
            }
        )
    # 逐库补全（详情 24h 缓存 + README；认证模式并发）。匿名模式跳过 README 省配额。
    enrich_started = time.monotonic()
    detail_cache_hits, detail_failed = _trending_enrich_all(projects, now=started, want_readme=not _is_anonymous())
    enrich_seconds = round(time.monotonic() - enrich_started, 1)
    _save_readme_cache()
    _save_detail_cache()
    if detail_cache_hits:
        print(f"[提示] {detail_cache_hits}/{len(projects)} 个热榜项目详情命中缓存（{DETAIL_CACHE_TTL_HOURS:.0f}h 跨 run 共享），本次未重复抓取。", file=sys.stderr)
    completed = utc_now()
    auth_route = "direct_gh" if _active_transport() == "gh" else ("direct_api_anonymous" if _is_anonymous() else "direct_api_token")
    result = {
        "schema_version": SCHEMA_VERSION,
        "run": {"id": started.strftime("%Y%m%dT%H%M%SZ"), "started_at": iso_z(started), "completed_at": iso_z(completed), "mode": "trending", "data_as_of": iso_z(completed), "auth_route": auth_route, "anonymous": _is_anonymous(), "is_valid_snapshot": False},
        "input": {"mode": "trending", "window": window, "language": language, "count": count, "source": "github_trending_scrape" if not degraded else "api_approx_fallback"},
        "queries": [{"text": "github.com/trending", "kind": "trending_scrape", "term": "", "term_weight": 1.0, "sort": "trending_rank", "order": "desc", "limit": count}],
        "collection": {"candidate_count": len(repos), "filtered_count": len(projects), "shortlist_count": len(projects), "dynamic_star_threshold": None, "low_sample": len(projects) < count, "pool_successes": {"trending": 1 if repos else 0}, "degraded": degraded, "detail_cache_hits": detail_cache_hits, "detail_failed": detail_failed, "enrich_seconds": enrich_seconds},
        "rankings": {"heat": [p["repo"]["full_name"] for p in projects], "recommendation": [], "pre_analysis_recommendation": [p["repo"]["full_name"] for p in projects]},
        "projects": projects,
        "rejected_candidates": [],
        "limitations": [
            "热榜模式：直接抓取 github.com/trending 原样呈现 GitHub 官方热门榜，排名与官网 100% 一致（含老牌但近期翻红的项目，如 openclaw）。" if not degraded else "热榜抓取失败，已回退到搜索 API 近似榜（高星 + 窗口内活跃），排序与官网 Trending 不完全一致。",
            "热榜照搬 GitHub 排名，不做我们的排名/打分；如需按「只看来新发布/某关键词」筛选，请用关键词定向调研（collect --fresh）或指定 --window。",
            *([] if not detail_failed else [f"热榜 {detail_failed} 个项目的详情抓取失败，卡片对应徽章（年龄/许可证等）可能缺失。"]),
            *([] if not _is_anonymous() else ["匿名模式：已跳过 README 抓取以节省配额，分析请以描述与已知数据为准；配置 GH_TOKEN 可获得 README 摘要。"]),
        ],
        "errors": [],
    }
    run_dir = trending_root / window / started.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "result.json", result)
    atomic_write_json(run_dir / "analysis-template.json", analysis_template(projects))
    # 订阅场景防累积：热榜 run 目录同样按窗口保留最近 N 个（与 collect 的 prune 一致）
    removed = prune_runs(trending_root / window)
    if removed:
        print(f"[提示] 已自动清理 {len(removed)} 个旧热榜 run 目录（每个窗口保留最近 {MAX_RUNS_PER_KEYWORD} 个）。", file=sys.stderr)
    lines = [f"GitHub 热榜（{window}，前 {len(projects)} 名）", "=" * 44]
    for rank, project in enumerate(projects, start=1):
        repo = project["repo"]
        lang = repo.get("primary_language") or "—"
        desc = (repo.get("description") or "")[:70]
        period = project["trend"].get("stars_period")
        period_label = project["trend"].get("stars_period_label")
        period_text = f"  (+{period} {period_label})" if period else ""
        lines.append(f"{rank:>2}. {repo['full_name']}  ★{repo['stars']}{period_text}  [{lang}]")
        if desc:
            lines.append(f"     {desc}")
        lines.append(f"     {repo['url']}")
    print("\n".join(lines))
    print(f"[热榜模式] 结果已写入：{run_dir / 'result.json'}")
    if degraded:
        print("[热榜模式] 本次为搜索 API 近似榜（抓取失败回退）；正式热榜请重试。")
    print("[热榜模式] 请对每个项目填写 analysis.json（one_liner + 4 详情块 + facts），再 finalize 出卡片报告。")
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    """环境自检：安装后跑一次，确认能否零配置直接采集。"""
    print("GitHub Trend Scout 环境自检")
    print("=" * 52)
    checks: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 9)
    checks.append(("Python >= 3.9", py_ok, sys.version.split()[0]))

    gh_path = shutil.which("gh")
    checks.append(("gh CLI", gh_path is not None, gh_path or "未安装（可跳过，脚本自动走纯 API 路径）"))
    gh_auth = False
    if gh_path:
        res = run_process(["gh", "auth", "status"], timeout=20, allow_failure=True)
        gh_auth = res.returncode == 0
        checks.append(("gh 已认证", gh_auth, "已认证" if gh_auth else "未认证（可跳过，脚本自动降级）"))

    token = _read_token()
    checks.append(("GH_TOKEN / GITHUB_TOKEN", bool(token), "已设置（认证模式，配额充足）" if token else "未设置（将自动启用匿名模式，零配置可用）"))

    net_ok = False
    net_detail = "无法访问 api.github.com"
    try:
        with urllib.request.urlopen("https://api.github.com/rate_limit", timeout=10) as resp:
            net_ok = True
            net_detail = f"可访问（HTTP {resp.status}）"
    except Exception as exc:  # noqa: BLE001 - doctor 需要捕获所有网络异常并展示
        net_detail = f"无法访问 api.github.com：{exc}"
    checks.append(("网络连通性", net_ok, net_detail))

    anon_ok = False
    anon_detail = "未探测（已有认证凭证，无需匿名探测）"
    if net_ok and not token and not gh_auth:
        try:
            with urllib.request.urlopen("https://api.github.com/repos/octocat/Hello-World", timeout=10) as resp:
                anon_ok = resp.status == 200
                anon_detail = f"匿名 API 可用（HTTP {resp.status}）"
        except Exception as exc:  # noqa: BLE001
            anon_detail = f"匿名 API 受限：{exc}"
        checks.append(("匿名 API 可用性", anon_ok, anon_detail))

    for name, ok, detail in checks:
        mark = "PASS" if ok else "WARN"
        print(f"  [{mark}] {name}: {detail}")
    print("=" * 52)
    if not net_ok:
        print("结论：环境不可用（网络无法访问 GitHub API）。请检查网络/代理后重试。")
        return 1
    if gh_auth or bool(token):
        print("结论：环境就绪，可运行（认证模式，配额充足，详情完整）。")
    else:
        print("结论：环境就绪，可零配置直接运行（匿名模式，配额较低，详情部分降级）。需要完整详情可设置 GH_TOKEN。")
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
    collect.add_argument("--exclude", action="append", default=[], help="排除词，可重复：命中的仓库（名称/描述/主题子串匹配）直接剔除，如 --exclude screenshot --exclude 录屏。")
    collect.add_argument("--strict-relevance", action="store_true", dest="strict_relevance", help="严格相关度：关键词分词覆盖不足半数的疑似跑题项目直接剔除（默认仅打标保留）。")
    collect.add_argument("--fresh", action="store_true", dest="fresh", help="新度门限：仅保留创建于近 --fresh-days 天内的项目（只看新发布，排除 openclaw 这类老牌）。")
    collect.add_argument("--fresh-days", type=int, default=FRESH_DEFAULT_DAYS, help=f"新度门限天数（配合 --fresh；默认 {FRESH_DEFAULT_DAYS}）。")
    collect.add_argument("--no-fresh-bias", action="store_true", dest="no_fresh_bias", help="关闭默认偏新软偏置（默认开启：热度分混入 15%% 的近 30 天新度分，结果偏新发布；关闭后回到纯热度口径，老牌高星仓库可能垄断榜单）。")
    collect.add_argument("--transport", choices=["auto", "gh", "api"], default="auto", help="采集传输层：auto（默认，优先 gh CLI，否则用纯 API；无 token 自动匿名）/ gh（仅 gh CLI）/ api（仅纯 API；无 token 时匿名模式）。")
    collect.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    collect.set_defaults(func=collect_command)

    watch = subparsers.add_parser("watch", help="订阅式增量采集：重新 collect 并与上一次 run 对比，只输出新上榜/掉榜/星数异动/状态变化；适合定时自动化。")
    watch.add_argument("--keyword", required=True)
    watch.add_argument("--term", action="append", default=[], help="扩展查询词，可重复；与 collect 一致。")
    watch.add_argument("--days", type=int, default=DEFAULT_DAYS, choices=range(1, 366), metavar="1..365")
    watch.add_argument("--count", type=int, default=DEFAULT_COUNT, choices=range(5, 21), metavar="5..20")
    watch.add_argument("--language")
    watch.add_argument("--exclude", action="append", default=[], help="排除词，可重复。")
    watch.add_argument("--strict-relevance", action="store_true", dest="strict_relevance")
    watch.add_argument("--fresh", action="store_true", dest="fresh", help="新度门限：仅保留创建于近 --fresh-days 天内的项目。")
    watch.add_argument("--fresh-days", type=int, default=FRESH_DEFAULT_DAYS, help=f"新度门限天数（配合 --fresh；默认 {FRESH_DEFAULT_DAYS}）。")
    watch.add_argument("--no-fresh-bias", action="store_true", dest="no_fresh_bias", help="关闭默认偏新软偏置（与 collect 语义一致；注意与基线保持同口径，否则增量噪音大）。")
    watch.add_argument("--transport", choices=["auto", "gh", "api"], default="auto")
    watch.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    watch.set_defaults(func=watch_command)

    trending = subparsers.add_parser("trending", help="热榜模式：抓取 github.com/trending 原样照搬 GitHub 热门榜（默认当天），逐库出卡片报告；服务\"今天热门榜单/随便看看\"。")
    trending.add_argument("--window", choices=list(TRENDING_WINDOWS), default="daily", help="时间窗口：daily/weekly/monthly（默认 daily，即当天榜）。")
    trending.add_argument("--count", type=int, default=15, choices=range(5, 31), metavar="5..30")
    trending.add_argument("--language")
    trending.add_argument("--transport", choices=["auto", "gh", "api"], default="auto")
    trending.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    trending.set_defaults(func=trending_command)

    doctor = subparsers.add_parser("doctor", help="环境自检：确认能否零配置直接采集（无需凭证）。")
    doctor.set_defaults(func=doctor_command)

    latest = subparsers.add_parser("latest-run", help="查询关键词最近一次 run 的新鲜度；采集前先查，可复用近期结果、避免重复消耗配额。")
    latest.add_argument("--keyword", required=True)
    latest.add_argument("--max-age-minutes", type=int, default=120, help="多少分钟内算\"新鲜\"（默认 120）。")
    latest.add_argument("--output-root", default=str(Path.cwd() / "github-trend-output"))
    latest.set_defaults(func=latest_run_command)

    finalize = subparsers.add_parser("finalize", help="校验分析并生成卡片式 HTML 报告（report.html）。默认只出卡片，不生成 Markdown/CSV。")
    finalize.add_argument("--run-dir", required=True)
    finalize.add_argument("--analysis-file", required=True)
    finalize.add_argument("--keep-extra", action="store_true", help="同时保留 report.md 与 rankings.csv（默认只生成卡片 report.html，并清理这两个文件）。")
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
