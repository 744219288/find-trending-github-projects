# find-trending-github-projects

发现并分析 GitHub 上任意关键词下**最新热门 / 快速增长**的仓库，输出**可交互的卡片式 HTML 报告**（自包含单文件、离线双击可开）。默认只生成 `report.html` 一个文件。

## 它能做什么

- 输入一个关键词（可附带同义词），基于 GitHub 官方数据 + 本地历史快照，找出近 7 天（可配）内最热的 5–20 个仓库。
- **默认偏新**：热度分混入 15% 的近 30 天新度分，「今天热门」类结果天然偏向新发布项目（老牌仍可凭真实热度上榜，只是排位后移）；`--no-fresh-bias` 切回纯热度口径，`--fresh` 则严格只看新发布。
- 每个项目一张卡：排名徽章 + 项目名直达链接 + 一句大白话介绍 + Star/Fork/Watch（数字滚动动画）/语言/许可证/更新时间徽章；点卡片展开「详细说明 / 适合谁 / 注意事项 / 二次开发·商业化」。
- 产出 `report.html`（主交付物，交互卡片页，也是**默认唯一输出文件**）。如需 `report.md`（精简 Markdown）与 `rankings.csv`（Excel 兼容），在 `finalize` 时加 `--keep-extra`；`result.json` 始终保留为结构化快照数据，支持可复现的本地对比。

## 安装

```bash
git clone https://github.com/744219288/find-trending-github-projects.git ~/.workbuddy/skills/find-trending-github-projects
```

克隆后重启 WorkBuddy（或刷新 skill 列表）即可在对话中直接使用。

**方式二：下载发布包（免 git）**

从 [Releases](https://github.com/744219288/find-trending-github-projects/releases) 下载最新的 `find-trending-github-projects.zip`，解压到 `~/.workbuddy/skills/`，使目录结构为：

```
~/.workbuddy/skills/find-trending-github-projects/SKILL.md
```

然后重启 WorkBuddy（或刷新 skill 列表）即可。发布包已通过官方 skill 校验（quick_validate），不含 `.git` 等无关内容。

> 也可手动把本仓库内容拷入 `~/.workbuddy/skills/find-trending-github-projects/`。

## 前置要求

- **仅需 Python 3.9+**：安装后**无需任何凭证、无需连接 GitHub 账号、无需验证**即可直接使用。脚本自动进入「匿名模式」，用 GitHub 未认证 API 完成采集（未认证配额：搜索 10 次/分钟、核心 60 次/小时）。
- **可选：配置凭证提升体验**（非必需）：
  - 推荐 `gh CLI` 已登录：`gh auth login`。脚本复用你本地的 keyring，**不会打印或存储任何凭证**。
  - 或设置环境变量 `GH_TOKEN` / `GITHUB_TOKEN`，脚本用 Python 标准库直连 GitHub REST API。
  - 配置凭证后配额充足，报告会包含 Release、近 30 天 Issue/PR 活跃度等完整详情；匿名模式自动跳过这些非关键详情以节省配额（在报告的 limitations 中注明）。

> 首次使用前可运行 `python scripts/github_trend_scout.py doctor` 做环境自检，确认能否零配置直接采集。

## 使用

在对话中直接描述需求即可，例如：

> 帮我找最近一周最火的 AI Agent 项目，Top 10

Skill 会自动调用下方脚本收集数据、评分并生成报告。中文关键词会被自动翻译成对应英文术语再检索（GitHub 仓库描述几乎全是英文），开始采集前会用一句话复述解析出的参数，方便你纠正。

### 手动运行脚本（可选）

```bash
# 1) 采集（从你的工作目录运行，而非 skill 安装目录）
python scripts/github_trend_scout.py collect \
  --keyword "AI Agent" --term "agentic AI" --days 7 --count 10
#    可选：--exclude screenshot   剔除名称/描述/主题命中的仓库（可重复多个）
#    可选：--strict-relevance     疑似跑题项目直接剔除（默认仅打标保留）
#    可选：--fresh --fresh-days 30   只看近 30 天新发布的项目（排除老牌翻红，默认 30 天，可用 -N 调）
#    开启后 created:>= 会注入全部搜索池，星数门槛同步放宽（新项目短期攒不到 20 星）
#    可选：--no-fresh-bias   关闭默认偏新软偏置（默认开启：热度分混入 15% 的近 30 天新度分，
#    「今天热门」类结果天然偏新发布；关闭后回到纯热度口径，老牌高星可能垄断榜单）

# 2) 分析：基于 result.json 填写 analysis.json（一句话 + 四板块，参考 analysis-template.json）
#    注意 facts 必须是 [{"claim": "...", "source_url": "https://..."}] 对象数组

# 3) 生成报告（默认只输出卡片 report.html；加 --keep-extra 同时生成 report.md + rankings.csv）
python scripts/github_trend_scout.py finalize \
  --run-dir "<上一步打印的 run 目录>" \
  --analysis-file "<run 目录>/analysis.json"

# 4) （可选）单独重渲染卡片报告
python scripts/render_card_report.py --run-dir "<run 目录>"

# 5) （可选）采集前查该关键词最近一次 run 是否可复用（省配额省时间）
python scripts/github_trend_scout.py latest-run --keyword "AI Agent" --max-age-minutes 120

# 6) 热榜模式：不输关键词、只想看 GitHub 官方 Trending（默认当天榜）
python scripts/github_trend_scout.py trending --window daily --count 15
#    --window daily|weekly|monthly（默认 daily）；--language 为编程语言（映射 ?language=）
#    直接抓取 github.com/trending 原样照搬，含老牌翻红；逐库详情走 24h 缓存（跨 run 共享），
#    认证模式附 README 摘要；匿名模式跳过 README 省配额
#    产出 result.json + analysis-template.json，填 analysis.json 后 finalize 出卡片 report.html

# 7) 订阅模式：盯住某关键词，只输出与上次相比的增量（新上榜/掉榜/星数异动/状态变化）
python scripts/github_trend_scout.py watch --keyword "AI Agent" --days 7 --count 10
#    首次运行建立基线；之后每次输出增量摘要（watch-summary.md/json），无变化时明确说"无变化"
#    适合配合宿主（如 WorkBuddy）的定时自动化做"新项目提醒"
```

## 订阅（定时报告）

安装后想**每天 / 每隔两三天自动出一份报告**，不需要写任何代码——用 WorkBuddy 的定时自动化配合本 skill 的 `watch` 模式即可：

1. **首次先跑一次建立基线**：在对话里说「帮我盯住 AI Agent 方向，近 7 天 Top 10」。skill 会跑一次完整采集，建立对比基线。
2. **创建定时自动化**：在 WorkBuddy 里新建一个自动化（recurring），例如每天或每 2 天早上跑一次，提示词写清楚关键词与口径，例如：

   > 使用 find-trending-github-projects skill 的追踪模式，对关键词「AI Agent」运行 watch（近 7 天、Top 10、与上次同口径），输出增量摘要：新上榜、掉榜、星数明显变化、状态变化（归档/新版本）；如无变化明确说「无变化」。工作目录用固定的 github-trend-output 目录。

3. **订阅口径保持一致**：每次定时运行使用相同的关键词、`--days`、`--count`、以及相同的偏新开关（默认开启的软偏置 / `--fresh` 硬门限 / `--no-fresh-bias` 纯热度）。口径切换会让 diff 产生上榜/掉榜噪音（watch 会自动提示口径不一致）。
4. **产物**：每次 watch 会新增一个 run 目录并输出 `watch-summary.md/json` 增量摘要；旧的 run 目录自动只保留最近 10 个，不会无限累积。想看完整卡片报告时，随时让 skill 对同一关键词再跑一次完整流程即可。

> 提示：匿名模式配额约 60 次/小时，每天一次的订阅绰绰有余；订阅频繁（如每小时）建议配置 `GH_TOKEN` 或 `gh auth login`。

## 目录结构

```
SKILL.md                            # Skill 定义与完整流程
agents/openai.yaml                  # 可选：仅供把本 Skill 暴露给 OpenAI 兼容 Agent 接口时使用，WorkBuddy 直接调用脚本时无需此文件
references/
  methodology.md                    # 排序方法（热度/活跃度/新鲜度）
  data-contract.md                  # JSON 数据结构约定
  report-contract.md                # 卡片报告契约（analysis.json 填写规范）
scripts/
  github_trend_scout.py             # 核心：采集 / 排序 / 快照 / finalize
  render_card_report.py             # 卡片式 HTML 报告渲染器
  test_github_trend_scout.py        # 测试
```

## 传输层与运行模式

采集支持两种传输层，通过 `--transport`（或环境变量 `GTS_TRANSPORT`）选择，默认 `auto`：

| 取值 | 说明 |
|---|---|
| `auto` | 优先 `gh` CLI，检测不到时回退到 `api`（推荐，零配置可用） |
| `gh` | 仅使用已登录的 GitHub CLI |
| `api` | 仅用纯 Python + GitHub REST API（无需 gh、无需 keyring）；无 token 时自动进入匿名模式 |

`api` 模式特别适合 WorkBuddy 的沙箱 / 子 agent 环境（那里 gh 的 keyring 凭证通常不可用）。**无凭证时自动以匿名模式运行**：请求自动串行并按未认证配额自适应节流，遇到限流会等待或优雅降级（写入 errors.json），不会崩溃。

- 报告中的 `run.auth_route` 标注实际路线：`direct_gh` / `direct_api_token` / `direct_api_anonymous`。
- 匿名模式下 `run.anonymous = true`，并在 `limitations` 中说明跳过了哪些非关键详情。
- 环境变量 `GTS_MIN_INTERVAL` 可覆盖匿名请求的最小间隔（秒，默认 6.5）。

## 网络与代理

脚本使用 Python 标准库发请求，**天然支持标准代理环境变量**。直连 GitHub 超时或不可达时：

```bash
# macOS / Linux
export HTTPS_PROXY=http://127.0.0.1:7890
# Windows (PowerShell)
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
```

请求失败时错误信息会自动附带网络诊断（代理是否配置、api.github.com 是否可达、建议动作），无需手动排查。

## 提升配额（可选，两分钟搞定）

匿名配额约 60 次/小时，一般够每次完整采集（约 15–20 次请求）；一小时内多次运行可能撞墙（错误信息会提示恢复时间）。想解锁 5000 次/小时 + 完整详情（Release、近 30 天 Issue/PR 活跃度）：

1. 打开 https://github.com/settings/tokens → **Generate new token (classic)**，勾选 `public_repo`（只需公开仓库读权限即可，不给其他权限）。
2. 设置环境变量：

```bash
export GH_TOKEN=ghp_xxxxxxxxxxxx   # macOS / Linux
setx GH_TOKEN ghp_xxxxxxxxxxxx     # Windows（设置后重开终端）
```

3. 重新运行即可自动进入认证模式，无需其他配置。

## 存储管理

- 每次采集在 `<关键词>/runs/<时间戳>/` 生成一个 run 目录，**自动只保留最近 10 个**（环境变量 `GTS_MAX_RUNS` 可改），旧目录自动清理；快照对比读取的是独立的 `snapshots/` 目录，**清理旧 run 不影响增速数据**。热榜模式的 run 目录（`_trending/<窗口>/`）同样自动保留最近 10 个。
- 首次运行某关键词没有历史快照，热度为代理分；报告顶部会出现提示条，终端也会提示「过几天再跑一次即可获得与本期对比的真实增速」。
- `cache/readme.json` 缓存 README，未改动的仓库不会重复抓取。
- `cache/repo-details.json` 缓存仓库详情与最新发版（默认 24 小时，环境变量 `GTS_DETAIL_CACHE_HOURS` 可改），**跨关键词共享**：搜「AI 生图」后紧接着搜「Stable Diffusion」，重叠项目不再重复抓取，第二次明显更快。

## 速度与相关度

- **并行加速**：认证模式（gh 登录或 token）下详情抓取默认 4 并发（`GTS_MAX_WORKERS` 可调）；匿名模式强制串行以保护配额。
- **跑题项目治理**：GitHub 搜索有词干化（image 会命中 images），容易混入跑题项目（如搜生图混入截图工具）。脚本会用严格整词匹配复核，覆盖不足半数的项目默认打上 `needs_verification:possible_offtopic` 标记并在分析中说明；确定不要的可用 `--exclude` 精准剔除，或 `--strict-relevance` 一刀切。

## 额外产出

`finalize` 后默认只生成 `report.html`（零依赖单文件，可直接发给任何人离线打开）；若加 `--keep-extra` 会额外生成 `rankings.csv`（兼容 Excel 的排名表：排名/链接/Star/Fork/Watch/语言/许可证/更新时间/一句话介绍）。关键词根目录下的 `cache/readme.json` 缓存 README，避免对未改动仓库重复抓取。

## 规则要点

- 仓库文本视为**不可信数据**，不会执行候选仓库的代码、不会克隆或安装它们。
- 严格区分「事实（facts，带 HTTP(S) 来源链接）」与「推断（inferences）/ 推测（speculations）」，不编造用户数、营收、融资、市场规模等。
- 不把「总 Star 数 / Star 除以年龄」等代理指标当作真实近期增长。

## 许可

[MIT](LICENSE)
