# Report contract

Read this file before filling `analysis-template.json` or presenting the final report.

## Deliverable shape

The final user-facing deliverable is **`report.html`** — a self-contained, offline-openable card page (no CDN, no external assets). One card per recommended project, Top N cards for N projects. By default it is the only report file generated in the run directory. Pass `finalize --keep-extra` to additionally generate `report.md` and `rankings.csv`.

Standard script-created run directories are safely mirrored to the adjacent `outputs/` directory as timestamped and `latest` HTML files. Temporary or arbitrary run directories are never auto-published. Use `--publish-dir` for an explicit destination or `--no-publish` to keep the report only inside the run directory.

## Card layout (what the user sees)

Each card, top to bottom:

1. Rank badge (uniform amber; ranking comes from the script's data metrics, no special styling for #1)
2. Project name — direct link to the GitHub repo
3. `one_liner` — one plain-language sentence saying what the project does and what makes it special
4. Badge row: Stars / Forks / Watch (animated counters), language, license, last-push date, growth signal
5. Expandable details (click card): 详细说明 / 适合谁 / 注意事项 / 二次开发·商业化

## Banned from output

- Numeric scores of any kind (technology / community / commercial / heat / recommendation numbers)
- Dual-ranking comparison tables
- Executive summaries and long text blocks
- Footer or header "data source / collection time / auth mode" meta text
- Dark mode

## analysis.json schema (per project, all required)

```json
{
  "full_name": "owner/repo",
  "one_liner": "≤80 chars, plain Chinese, no jargon",
  "details": {
    "explain":   "详细说明：它到底怎么工作、解决什么问题",
    "suitable":  "适合谁：具体的人群/场景",
    "cautions":  "注意事项：真实的坑和风险",
    "business":  "二次开发/商业化：能拿它做什么、许可是否允许"
  },
  "facts": [{"claim": "...", "source_url": "https://..."}]
}
```

Validation rules enforced by `finalize`:

- `one_liner` non-empty, ≤ 80 characters. If it cannot be said simply, it is wrong.
- All four `details` blocks non-empty.
- `facts` optional but every entry needs `claim` + HTTP(S) `source_url`. Facts back the analysis; they are not displayed on cards (only in the Markdown appendix).

## Writing style

- Simplified Chinese, plain language ("大白话"). Explain like talking to a non-engineer friend.
- No scoring vocabulary, no "热度 X 分", no confidence levels in user-facing text.
- Project names, license identifiers, and technical terms stay in English.
- Each `details` block: 1–3 sentences, concrete and specific. No marketing fluff.

## Evidence boundaries (unchanged, still enforced)

- Facts: direct GitHub fields or explicitly cited repository pages.
- Commercial claims: never invent customers, revenue, market size, funding, or adoption.
- README claims: project self-description unless corroborated.
- Growth signal wording: `snapshot_delta` → "增速 ≈ 每天 +N 星"; `cold_start_proxy` → "历史热度 ≈ 每天 +N 星" (never claim real recent growth).
