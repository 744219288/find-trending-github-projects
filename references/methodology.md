# Methodology

Read this file before ranking or analyzing projects.

## Candidate discovery

Search up to four transparent terms: the original keyword plus at most three closely related terms. The script builds four pools:

| Pool | Soft quota | Purpose |
|---|---:|---|
| Relevance | 24 | Preserve topical precision. |
| New | 20 | Find repositories created within 90 days. |
| Active | 20 | Find recently pushed repositories. |
| Popular | 16 | Retain mature topic leaders. |

Merge by case-insensitive `owner/name`. Exclude archived, disabled, mirror, ordinary fork, pure Awesome/list, link collection, and personal-notes repositories. Normally require 20 Stars; lower to 5 only when the topic is narrow and label the result low-sample.

Candidate selection is two-stage. The search response provides the preliminary ordering. In authenticated mode, enrich every retained candidate with repository details, release, Issue/PR activity, and README before choosing Top N. Anonymous mode skips ranking-critical release and Issue/PR calls and enriches a bounded buffer (`max(2 × Top N, Top N + 5)`) to stay within the public quota.

## Trend modes

### `snapshot_delta`

Use only a valid local snapshot near the requested starting point. Display the actual interval rather than relabeling it as exactly 7 days.

```text
heat =
  35% absolute Star delta percentile
+ 25% relative Star growth percentile
+ 10% Fork delta percentile
+ 20% maintenance activity
+ 10% freshness and completeness
```

Use `delta / max(baseline Stars, 20)` for relative growth to suppress tiny-base explosions.

When only part of the current list exists in the baseline, projects with a baseline use `snapshot_delta`; new entrants use `cold_start_proxy`. Percentiles are normalized inside the applicable cohort only. Missing metrics from the other cohort are never inserted as zero. The per-project `trend.mode` is authoritative, and the report records a limitation explaining the mixed cohort.

### `cold_start_proxy`

GitHub restricted public Stargazer listing access in July 2026. Do not rely on Stargazer timestamps for arbitrary public repositories. On the first run use:

```text
heat =
  20% current Star maturity percentile
+ 30% age-normalized Star proxy percentile
+ 15% Fork traction percentile
+ 25% maintenance activity
+ 10% freshness and completeness
```

Keep `stars_delta`, `stars_growth_rate`, and `forks_delta` null. Never call this “actual 7-day growth.” First-run trend confidence is at most medium.

### `stale_snapshot_fallback`

Use only when current collection fails and a valid prior snapshot exists. Mark the result non-real-time, preserve the old data timestamp, do not write a new snapshot, and do not finalize it as a current commercial report.

## Pre-analysis recommendation

```text
recommendation =
  45% heat
+ 35% maintenance activity
+ 20% freshness and completeness
```

This deterministic order becomes the final card order. Narrative analysis explains the projects but does not inject hidden scores or reorder them.

## Confidence and credibility

- High: complete official fields, suitable historical snapshot, and directly traceable key evidence.
- Medium: complete current official data but no history, or conclusions mainly rely on project self-description.
- Low: small sample, missing fields, partial API coverage, or stale data.

Confidence is internal reasoning guidance and is not written into `analysis.json` or shown on cards. Star/Fork imbalance, growth discontinuity, minimal repository content, or absent collaboration may add `needs_verification`; never use these signals alone to accuse manipulation or automatically exclude a project.
