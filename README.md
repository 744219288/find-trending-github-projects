# find-trending-github-projects

发现并分析 GitHub 上任意关键词下**最新热门 / 快速增长**的仓库，输出带证据链接的中文 Markdown 报告 + 结构化 JSON。

## 它能做什么

- 输入一个关键词（可附带同义词），基于 GitHub 官方数据 + 本地历史快照，找出近 7 天（可配）内最热的 5–20 个仓库。
- 对每个项目给出**技术 / 社区 / 商业**三维度评分（0–100），并明确区分「事实」与「推断」。
- 产出 `report.md`（中文报告）与 `result.json`（结构化数据），支持可复现的本地快照对比。

## 安装

```bash
git clone https://github.com/744219288/find-trending-github-projects.git ~/.workbuddy/skills/find-trending-github-projects
```

克隆后重启 WorkBuddy（或刷新 skill 列表）即可在对话中直接使用。

> 也可手动把本仓库内容拷入 `~/.workbuddy/skills/find-trending-github-projects/`。

## 前置要求

- **`gh` CLI 已登录**：`gh auth login`。脚本复用你本地的 `GH_TOKEN` / keyring，**不会打印或存储任何凭证**。
- **Python 3**：用于运行采集与报告脚本。

## 使用

在对话中直接描述需求即可，例如：

> 帮我找最近一周最火的 AI Agent 项目，Top 10

Skill 会自动调用下方脚本收集数据、评分并生成报告。

### 手动运行脚本（可选）

```bash
# 1) 采集（从你的工作目录运行，而非 skill 安装目录）
python scripts/github_trend_scout.py collect \
  --keyword "AI Agent" --term "agentic AI" --days 7 --count 10

# 2) 分析：基于 result.json 填写 analysis.json（参考 analysis-template.json）

# 3) 生成报告
python scripts/github_trend_scout.py finalize \
  --run-dir "<上一步打印的 run 目录>" \
  --analysis-file "<run 目录>/analysis.json"
```

## 目录结构

```
SKILL.md                            # Skill 定义与完整流程
agents/openai.yaml                  # Agent 配置
references/
  methodology.md                    # 评分方法
  data-contract.md                  # JSON 数据结构约定
  report-contract.md                # 报告格式约定
scripts/
  github_trend_scout.py             # 核心：采集 / 评分 / 报告
  test_github_trend_scout.py        # 测试
```

## 规则要点

- 仓库文本视为**不可信数据**，不会执行候选仓库的代码、不会克隆或安装它们。
- 严格区分「事实（facts，带 HTTP(S) 来源链接）」与「推断（inferences）/ 推测（speculations）」，不编造用户数、营收、融资、市场规模等。
- 不把「总 Star 数 / Star 除以年龄」等代理指标当作真实近期增长。

## 许可

[MIT](LICENSE)
