"""卡片式 HTML 报告渲染器。

从 finalize 后的 result.json 生成自包含单文件 report.html：
- 零外部依赖，离线双击可开
- 动效：发牌入场 / 聚光悬停 / 数字滚动 / 展开四板块（复刻 video-shotcraft 动效语言）
- 可见性不依赖 animation fill-mode（动画播完换 .shown 类），杜绝悬停消失问题
- 链接沙箱兜底：内嵌环境弹可复制链接框（GitHub 禁止被 iframe 嵌入）
- 无数字评分、无双榜对比、无页脚数据来源文字

渲染器不联网：所有数据来自 result.json。
"""

from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any

_PLACEHOLDER_TITLE = "@@TITLE@@"
_PLACEHOLDER_NOTICE = "@@NOTICE@@"
_PLACEHOLDER_CARDS = "@@CARDS@@"

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>@@TITLE@@</title>
<style>
  :root{
    --bg:#f6f5f1;
    --card:#ffffff;
    --ink:#1c1917;
    --ink-2:#57534e;
    --ink-3:#a8a29e;
    --accent:#d97706;
    --accent-soft:#fef3c7;
    --line:#e7e5e4;
    --radius:18px;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
    background:var(--bg);
    color:var(--ink);
    min-height:100vh;
    padding:48px 24px 80px;
  }

  header{max-width:1080px;margin:0 auto}
  .kicker{
    display:inline-flex;align-items:center;gap:8px;
    font-size:13px;font-weight:600;color:var(--accent);
    background:var(--accent-soft);border:1px solid #fde68a;
    padding:5px 14px;border-radius:999px;margin-bottom:14px;
  }
  h1{font-size:30px;font-weight:800;letter-spacing:-.5px}

  .notice{
    max-width:1080px;margin:18px auto 0;
    display:flex;align-items:flex-start;gap:10px;
    background:var(--accent-soft);border:1px solid #fde68a;
    border-radius:12px;padding:12px 16px;
    font-size:13.5px;line-height:1.65;color:#78350f;
    animation:notice-in .45s cubic-bezier(.2,1.25,.3,1);
  }
  @keyframes notice-in{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
  .notice-icon{flex:none;font-style:normal;font-size:15px;line-height:1.6}

  .grid{
    max-width:1080px;margin:34px auto 0;
    display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));
    gap:20px;perspective:1200px;
  }
  @media (max-width:1024px){.grid{grid-template-columns:1fr}}

  /* 外层：只做定位和入场。动画播完 JS 换 .shown（纯 opacity:1，绝不依赖 fill-mode） */
  .card{
    position:relative;
    border-radius:var(--radius);
    opacity:0;
    transition:opacity .3s ease;
  }
  .card.dealing{
    animation:deal-in .62s cubic-bezier(.3,0,.2,1) forwards,
              settle .28s cubic-bezier(.3,0,.25,1.15) .62s forwards;
  }
  .card.shown{opacity:1}
  @keyframes deal-in{
    0%{opacity:0;transform:translateY(64px) scale(.92)}
    60%{opacity:1;transform:translateY(-6px) scale(1.05)}
    100%{opacity:1;transform:translateY(0) scale(1.01)}
  }
  @keyframes settle{
    from{transform:translateY(0) scale(1.01)}
    to{transform:translateY(0) scale(1)}
  }
  @keyframes press{
    0%{transform:scale(1)}
    40%{transform:scale(.996)}
    100%{transform:scale(1)}
  }

  /* 内层：承担视觉。悬停弹起只发生在内层，外层不动 → 悬停区域稳定 */
  .lift{
    position:relative;
    background:var(--card);
    border:1px solid var(--line);
    border-radius:var(--radius);
    padding:22px 24px 18px;
    cursor:pointer;
    box-shadow:0 2px 6px rgba(28,25,23,.05);
    transition:transform .35s cubic-bezier(.2,1.25,.3,1),
               box-shadow .35s ease, filter .35s ease, opacity .35s ease;
  }
  .lift.press{animation:press .18s ease-out}
  .card.shown:hover .lift{
    transform:translateY(-8px) scale(1.012);
    box-shadow:0 10px 18px rgba(28,25,23,.10),0 44px 80px rgba(28,25,23,.16);
  }
  .card.dimmed .lift{opacity:.55;filter:saturate(.6)}
  .card.shown:hover{z-index:5}

  .beam{position:absolute;inset:-2px;width:calc(100% + 4px);height:calc(100% + 4px);pointer-events:none;opacity:0;transition:opacity .25s;z-index:6}
  .card:hover .beam{opacity:1}
  .beam rect{
    width:100%;height:100%;rx:18;
    fill:none;stroke:var(--accent);stroke-width:2.5;
    stroke-dasharray:.14 1;stroke-dashoffset:0;
  }
  .card:hover .beam rect{animation:beam-scan 1.6s linear infinite}
  @keyframes beam-scan{from{stroke-dashoffset:0}to{stroke-dashoffset:-2}}

  .row1{display:flex;align-items:center;gap:12px}
  .rank{
    flex:none;width:34px;height:34px;border-radius:10px;
    display:flex;align-items:center;justify-content:center;
    font-size:15px;font-weight:800;color:#fff;
    background:linear-gradient(135deg,#f59e0b,#d97706);
    box-shadow:0 3px 8px rgba(217,119,6,.35);
  }
  .name{
    font-size:19px;font-weight:700;color:var(--ink);
    text-decoration:none;letter-spacing:-.2px;
  }
  .name:hover{text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:4px}
  .name .org{color:var(--ink-3);font-weight:500}
  .expand-hint{
    margin-left:auto;flex:none;font-size:12px;color:var(--ink-3);
    display:inline-flex;align-items:center;gap:4px;
    transition:transform .3s cubic-bezier(.2,1.25,.3,1),color .2s;
  }
  .card.open .expand-hint{transform:rotate(180deg);color:var(--accent)}

  .blurb{margin-top:13px;font-size:14.5px;line-height:1.65;color:var(--ink)}

  .badges{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
  .badge{
    display:inline-flex;align-items:center;gap:5px;
    font-size:12.5px;font-weight:600;color:var(--ink-2);
    background:#fafaf9;border:1px solid var(--line);
    padding:4px 11px;border-radius:999px;
  }
  .badge.star{color:#b45309;background:var(--accent-soft);border-color:#fde68a}
  .badge .num{font-variant-numeric:tabular-nums;font-weight:700}
  .badge.lang{color:#4338ca;background:#eef2ff;border-color:#e0e7ff}
  .badge.license{color:#047857;background:#ecfdf5;border-color:#d1fae5}

  .details{
    display:grid;grid-template-rows:0fr;
    transition:grid-template-rows .5s cubic-bezier(.3,0,.2,1);
  }
  .card.open .details{grid-template-rows:1fr}
  .details-inner{overflow:hidden}
  .detail-blocks{
    margin-top:16px;padding-top:16px;border-top:1px dashed var(--line);
    display:grid;gap:12px;
  }
  .card.open .dblock{opacity:1;transform:none}
  .card.open .dblock:nth-child(1){transition-delay:.12s}
  .card.open .dblock:nth-child(2){transition-delay:.2s}
  .card.open .dblock:nth-child(3){transition-delay:.28s}
  .card.open .dblock:nth-child(4){transition-delay:.36s}
  .dblock{opacity:0;transform:translateY(10px);transition:opacity .4s ease,transform .4s cubic-bezier(.2,1.25,.3,1)}
  .dblock h4{
    font-size:12px;font-weight:700;color:var(--accent);
    letter-spacing:1px;margin-bottom:5px;
  }
  .dblock p{font-size:13.5px;line-height:1.7;color:var(--ink-2)}

  .link-toast{
    position:fixed;left:50%;bottom:28px;transform:translateX(-50%);
    display:flex;align-items:center;gap:14px;
    background:#1c1917;color:#fff;border-radius:12px;
    padding:12px 14px 12px 18px;max-width:min(92vw,720px);
    box-shadow:0 12px 32px rgba(28,25,23,.35);z-index:99;
    font-size:13px;animation:toast-in .3s cubic-bezier(.2,1.25,.3,1);
  }
  @keyframes toast-in{from{opacity:0;transform:translateX(-50%) translateY(16px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
  .lt-url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#e7e5e4}
  .lt-copy{
    flex:none;border:none;cursor:pointer;font-size:13px;font-weight:600;
    color:#1c1917;background:#fbbf24;border-radius:8px;padding:7px 14px;
  }
  .lt-copy:hover{background:#f59e0b}
</style>
</head>
<body>

<header>
  <span class="kicker">&#9889; GitHub 趋势雷达</span>
  <h1>@@TITLE@@</h1>
</header>

@@NOTICE@@
<div class="grid" id="grid">
@@CARDS@@
</div>

<script>
(function(){
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));

  /* 发牌入场：动画播完立即换 .shown，可见性绝不依赖 animation fill-mode */
  var INTERVAL0 = 130, SHRINK = 0.84;
  var t = 0;
  cards.forEach(function(c, k){
    var lift = c.querySelector('.lift');
    setTimeout(function(){
      c.classList.add('dealing');
      setTimeout(function(){
        c.classList.remove('dealing');
        c.classList.add('shown');
        lift.classList.add('press');
        setTimeout(function(){ lift.classList.remove('press'); }, 250);
      }, 900);
    }, t);
    t += INTERVAL0 * Math.pow(SHRINK, k);
  });

  // 兜底：任何原因导致动画没跑，2.5s 后强制可见
  setTimeout(function(){
    cards.forEach(function(c){
      if(!c.classList.contains('shown')){
        c.classList.remove('dealing');
        c.classList.add('shown');
      }
    });
  }, 2500);

  // DigitRoll：数字滚动（卡面所有带 data-count 的数字）
  document.querySelectorAll('.num').forEach(function(el){
    var target = +el.getAttribute('data-count');
    var dur = 1100, start = null;
    function step(ts){
      if(!start) start = ts;
      var p = Math.min((ts - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(target * eased).toLocaleString('en-US');
      if(p < 1) requestAnimationFrame(step);
    }
    setTimeout(function(){ requestAnimationFrame(step); }, 350);
  });

  // 点击展开 / 收起（点链接不触发）
  cards.forEach(function(c){
    c.addEventListener('click', function(e){
      if(e.target.closest('a')) return;
      c.classList.toggle('open');
    });
  });

  // spotlight：悬停主角亮、其余压暗（JS 加类，不依赖 :has()）
  cards.forEach(function(c){
    c.addEventListener('mouseenter', function(){
      if(!c.classList.contains('shown')) return;
      cards.forEach(function(x){ if(x !== c) x.classList.add('dimmed'); });
    });
    c.addEventListener('mouseleave', function(){
      cards.forEach(function(x){ x.classList.remove('dimmed'); });
    });
  });

  // 链接兜底：沙箱环境（GitHub 禁止被嵌入 + 弹窗被拦截）→ 弹可复制链接框
  var embedded = (function(){ try { return window.self !== window.top; } catch(e){ return true; } })();
  document.querySelectorAll('a.name').forEach(function(a){
    a.addEventListener('click', function(e){
      if(!embedded) return;
      e.preventDefault();
      var url = a.href, w = null;
      try { window.top.location.href = url; } catch(err){}
      try { w = window.open(url); } catch(err){}
      if(!w) showLinkToast(url);
    });
  });

  function showLinkToast(url){
    var old = document.querySelector('.link-toast');
    if(old) old.remove();
    var t = document.createElement('div');
    t.className = 'link-toast';
    t.innerHTML = '<span class="lt-url"></span><button class="lt-copy">复制链接</button>';
    t.querySelector('.lt-url').textContent = url;
    document.body.appendChild(t);
    t.querySelector('.lt-copy').addEventListener('click', function(){
      var done = function(){
        var b = t.querySelector('.lt-copy');
        b.textContent = '已复制 \\u2713';
        setTimeout(function(){ t.remove(); }, 1200);
      };
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(url).then(done, function(){ fallbackCopy(url, done); });
      } else { fallbackCopy(url, done); }
    });
    setTimeout(function(){ if(document.body.contains(t)) t.remove(); }, 12000);
  }
  function fallbackCopy(text, done){
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch(e){}
    ta.remove(); done();
  }
})();
</script>
</body>
</html>
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _updated_badge(pushed_at: Any) -> str | None:
    moment = _parse_time(pushed_at)
    if not moment:
        return None
    return f"更新于 {moment.month} 月 {moment.day} 日"


def _age_badge(created_at: Any) -> str | None:
    """项目年龄徽章：直接区分「新项目」与「老牌」，杜绝「要新的却拿到老的」的意外。"""
    moment = _parse_time(created_at)
    if not moment:
        return None
    days = (dt.datetime.now(dt.timezone.utc) - moment).days
    if days < 0:
        return None
    if days < 30:
        return f"🆕 新项目 · 创建于 {days} 天前"
    if days < 365:
        return f"创建于 {days // 30} 个月前"
    return f"已成立 {days // 365} 年"


def _period_badge(trend: dict[str, Any]) -> str | None:
    """热榜原生增速徽章：GitHub Trending 页面给的「当日/本周新增 Star」。"""
    period = trend.get("stars_period")
    label = trend.get("stars_period_label")
    if isinstance(period, (int, float)) and period > 0 and label:
        label_cn = {"today": "今日", "this week": "本周", "this month": "本月"}.get(label, label)
        return f"⏱ {label_cn} +{period:,} 星"
    return None


def _trend_badge(trend: dict[str, Any]) -> str | None:
    """增速徽章。严格区分真实增量与代理信号，绝不虚标。"""
    mode = trend.get("mode")
    if mode == "snapshot_delta":
        delta = trend.get("stars_delta")
        window = trend.get("window_actual_days")
        if isinstance(delta, (int, float)) and isinstance(window, (int, float)) and window > 0:
            per_day = round(delta / window)
            if per_day > 0:
                return f"增速 ≈ 每天 +{per_day} 星"
    elif mode == "cold_start_proxy":
        per_day = ((trend.get("proxy") or {}).get("stars_per_age_day")) or 0
        if per_day > 0:
            return f"历史热度 ≈ 每天 +{round(per_day)} 星"
    return None


def _card_html(rank: int, project: dict[str, Any]) -> str:
    repo = project.get("repo") or {}
    analysis = project.get("analysis") or {}
    full_name = repo.get("full_name") or ""
    owner, _, name = full_name.partition("/")
    url = repo.get("url") or f"https://github.com/{full_name}"
    one_liner = analysis.get("one_liner") or repo.get("description") or ""
    details = analysis.get("details") or {}

    badges: list[str] = []
    stars = repo.get("stars")
    if isinstance(stars, (int, float)):
        badges.append(f'<span class="badge star">&#9733; <span class="num" data-count="{int(stars)}">0</span></span>')
    forks = repo.get("forks")
    if isinstance(forks, (int, float)):
        badges.append(f'<span class="badge">&#8618; <span class="num" data-count="{int(forks)}">0</span></span>')
    watchers = repo.get("subscribers")
    if isinstance(watchers, (int, float)):
        badges.append(f'<span class="badge">&#9678; <span class="num" data-count="{int(watchers)}">0</span></span>')
    language = repo.get("primary_language")
    if language:
        badges.append(f'<span class="badge lang">{_esc(language)}</span>')
    license_id = (repo.get("license") or {}).get("spdx_id")
    if license_id and license_id not in ("NOASSERTION", "None"):
        badges.append(f'<span class="badge license">{_esc(license_id)}</span>')
    updated = _updated_badge(repo.get("pushed_at"))
    if updated:
        badges.append(f'<span class="badge">{_esc(updated)}</span>')
    age = _age_badge(repo.get("created_at"))
    if age:
        badges.append(f'<span class="badge">{_esc(age)}</span>')
    period = _period_badge(project.get("trend") or {})
    if period:
        badges.append(f'<span class="badge star">{_esc(period)}</span>')
    trend = _trend_badge(project.get("trend") or {})
    if trend:
        badges.append(f'<span class="badge">{_esc(trend)}</span>')

    blocks = [
        ("详细说明", details.get("explain")),
        ("适合谁", details.get("suitable")),
        ("注意事项", details.get("cautions")),
        ("二次开发 / 商业化", details.get("business")),
    ]
    detail_html = "".join(
        f'<div class="dblock"><h4>{_esc(title)}</h4><p>{_esc(text) if text else "暂无。"}</p></div>'
        for title, text in blocks
    )

    return (
        '  <article class="card" data-url="' + _esc(url) + '">\n'
        '    <svg class="beam" preserveAspectRatio="none"><rect pathLength="100"/></svg>\n'
        '    <div class="lift">\n'
        '      <div class="row1">\n'
        f'        <span class="rank">{rank}</span>\n'
        f'        <a class="name" href="{_esc(url)}"><span class="org">{_esc(owner)} /</span> {_esc(name)}</a>\n'
        '        <span class="expand-hint">展开 &#9662;</span>\n'
        '      </div>\n'
        f'      <p class="blurb">{_esc(one_liner)}</p>\n'
        '      <div class="badges">\n        ' + "\n        ".join(badges) + "\n      </div>\n"
        '      <div class="details"><div class="details-inner"><div class="detail-blocks">\n'
        f'        {detail_html}\n'
        '      </div></div></div>\n'
        '    </div>\n'
        '  </article>\n'
    )


def _notice_html(result: dict[str, Any]) -> str:
    """首跑冷启动提示条：明确告知本期热度是代理分，并引导二次运行获取真实增速。"""
    mode = (result.get("run") or {}).get("mode")
    if mode != "cold_start_proxy":
        return ""
    return (
        '<aside class="notice">\n'
        '  <i class="notice-icon">&#9432;</i>\n'
        "  <div><strong>首次运行，还没有历史快照。</strong>"
        "本期卡面上的「历史热度」是按项目年龄折算的代理分，不代表真实的近期增长；"
        "过几天对同一关键词再跑一次，即可看到与本期对比的真实星数增速。</div>\n"
        "</aside>\n"
    )


def render_card_report(result: dict[str, Any]) -> str:
    """从 finalize 后的 result.json 生成完整 HTML 字符串。"""
    projects = result.get("projects") or []
    by_name = {p.get("repo", {}).get("full_name", ""): p for p in projects}
    order = (
        (result.get("rankings") or {}).get("recommendation")
        or (result.get("rankings") or {}).get("pre_analysis_recommendation")
        or (result.get("rankings") or {}).get("heat")
        or [p.get("repo", {}).get("full_name", "") for p in projects]
    )
    cards_html = "".join(
        _card_html(rank, by_name[name])
        for rank, name in enumerate((n for n in order if n in by_name), start=1)
    )
    visible = sum(1 for n in order if n in by_name)
    run_mode = (result.get("run") or {}).get("mode")
    keyword = (result.get("input") or {}).get("keyword")
    window = (result.get("input") or {}).get("window")
    window_cn = {"daily": "当日", "weekly": "本周", "monthly": "本月"}.get(window, "当期")
    if run_mode == "trending":
        kicker = "GitHub 热榜"
        title = f"GitHub {window_cn}热榜 · 最火的 {visible} 个项目"
    else:
        kicker = "GitHub 趋势雷达"
        title = f"{keyword or 'GitHub 趋势'} 方向 · 本期最值得看的 {visible} 个项目"
    return (
        _TEMPLATE
        .replace(_PLACEHOLDER_TITLE, _esc(title))
        .replace("GitHub 趋势雷达", _esc(kicker))
        .replace(_PLACEHOLDER_NOTICE, _notice_html(result).rstrip("\n"))
        .replace(_PLACEHOLDER_CARDS, cards_html.rstrip("\n"))
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="从 result.json 渲染卡片式 HTML 报告。")
    parser.add_argument("--run-dir", required=True, help="run 目录（含 finalize 后的 result.json）")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise SystemExit(f"ERROR: 找不到 {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not (result.get("projects") or [{}])[0].get("analysis"):
        raise SystemExit("ERROR: result.json 尚未 finalize（缺少 analysis），请先运行 finalize。")
    output = run_dir / "report.html"
    output.write_text(render_card_report(result), encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
