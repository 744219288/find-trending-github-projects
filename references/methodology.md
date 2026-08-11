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

## Comprehensive recommendation

```text
recommendation =
  30% heat
+ 25% technology value
+ 20% community health
+ 25% commercial potential
```

Show both rankings. Explain meaningful rank changes.

## Analysis rubrics

### Technology value

- Positioning and differentiation: 25%
- Usability and completion evidence: 20%
- Public engineering-quality signals: 20%
- Extensibility and integrations: 20%
- Documentation and reproducibility: 15%

### Community health

- Recent maintenance continuity: 30%
- Issue/PR collaboration signals: 25%
- Release cadence: 20%
- Documentation and contribution entry points: 15%
- Fork and participation breadth: 10%

Do not equate a high raw Issue count with health. Consider responsiveness, recency, and whether activity is capped or incomplete.

### Commercial potential

- Importance of the problem: 20%
- Target-user clarity: 15%
- Secondary-development and integration room: 20%
- Plausible business models: 15%
- Differentiation and competitive barrier: 15%
- Inverse license/deployment/dependency risk: 15%

Do not fabricate market size, revenue, funding, customers, or adoption.

## Confidence and credibility

- High: complete official fields, suitable historical snapshot, and directly traceable key evidence.
- Medium: complete current official data but no history, or conclusions mainly rely on project self-description.
- Low: small sample, missing fields, partial API coverage, or stale data.

Keep confidence separate from score. Star/Fork imbalance, growth discontinuity, minimal repository content, or absent collaboration may add `needs_verification`; never use these signals alone to accuse manipulation or automatically exclude a project.

