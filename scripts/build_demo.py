#!/usr/bin/env python3
"""Build deterministic, synthetic HTML assets for the public project demo.

The demo is deliberately static: it never imports the orchestrator, opens a
network connection, or reads deployment configuration.  Every value rendered
below is documentation-only data reserved for examples.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "demo"


DEMO_CSS = """\
:root {
  color-scheme: light;
  --ink: #243447;
  --muted: #687988;
  --line: #d8e1e8;
  --panel: #ffffff;
  --canvas: #f4f7fa;
  --teal: #3aa0b8;
  --blue: #175c9b;
  --navy: #073a90;
  --green: #43a957;
  --amber: #b66b10;
  --red: #ad2b3a;
  --shadow: 0 12px 32px rgba(25, 54, 76, 0.11);
}
* { box-sizing: border-box; }
html { min-height: 100%; background: var(--canvas); }
body {
  min-width: 320px;
  min-height: 100vh;
  margin: 0;
  background:
    radial-gradient(circle at 86% 4%, rgba(58, 160, 184, 0.11), transparent 24rem),
    var(--canvas);
  color: var(--ink);
  font: 14px/1.55 Arial, "Microsoft YaHei", sans-serif;
}
a { color: inherit; }
.site-header {
  background: linear-gradient(105deg, #0b4e82, #2d91ad);
  color: #fff;
  box-shadow: 0 2px 12px rgba(14, 61, 97, 0.24);
}
.header-inner, .page, .site-footer {
  width: min(1180px, calc(100% - 40px));
  margin: 0 auto;
}
.header-inner {
  min-height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.brand { display: flex; align-items: center; gap: 13px; text-decoration: none; }
.brand-mark {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, 0.52);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.12);
  font-weight: 700;
}
.brand strong { display: block; font-size: 18px; letter-spacing: 0.2px; }
.brand small { display: block; color: #d8f3fa; }
.demo-flag {
  border: 1px solid rgba(255, 255, 255, 0.45);
  border-radius: 999px;
  padding: 6px 12px;
  background: rgba(255, 255, 255, 0.1);
  font-size: 12px;
}
.demo-nav { background: rgba(4, 45, 78, 0.24); }
.demo-nav-inner {
  width: min(1180px, calc(100% - 40px));
  margin: 0 auto;
  display: flex;
  gap: 4px;
  overflow-x: auto;
}
.demo-nav a {
  flex: none;
  border-bottom: 3px solid transparent;
  padding: 12px 17px 10px;
  color: #e8f6fb;
  text-decoration: none;
}
.demo-nav a:hover, .demo-nav a[aria-current="page"] {
  border-bottom-color: #82dc8b;
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}
.page { padding: 32px 0 44px; }
.hero {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 28px;
  margin-bottom: 22px;
}
.eyebrow {
  margin: 0 0 5px;
  color: var(--teal);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
h1 { margin: 0; font-size: clamp(26px, 4vw, 38px); line-height: 1.18; }
.lead { max-width: 720px; margin: 9px 0 0; color: var(--muted); font-size: 15px; }
.synthetic-note {
  flex: none;
  border: 1px solid #b9d7e0;
  border-radius: 8px;
  padding: 9px 12px;
  background: #eaf8fb;
  color: #23637a;
  font-size: 12px;
}
.grid { display: grid; gap: 18px; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.card {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--line);
  padding: 15px 18px;
  background: #fbfcfd;
}
.card-head h2, .card-head h3 { margin: 0; font-size: 16px; }
.card-body { padding: 18px; }
.metric { min-height: 132px; }
.metric-label { color: var(--muted); font-size: 12px; }
.metric-value { margin: 6px 0 2px; font-size: 26px; font-weight: 700; }
.metric-detail { color: var(--muted); }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 999px;
  padding: 4px 9px;
  font-size: 12px;
  font-weight: 700;
}
.pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.pill-ok { background: #e8f6eb; color: #24763a; }
.pill-safe { background: #e8f2fb; color: #225f95; }
.pill-warn { background: #fff3df; color: #915615; }
.progress { height: 8px; margin: 13px 0 8px; border-radius: 99px; background: #e7edf1; overflow: hidden; }
.progress > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--teal), var(--green)); }
.progress-full > span { width: 100%; }
.status-list { list-style: none; margin: 0; padding: 0; }
.status-list li { display: grid; grid-template-columns: 24px 1fr auto; gap: 10px; padding: 13px 0; border-bottom: 1px solid #e8edf1; }
.status-list li:last-child { border-bottom: 0; }
.status-icon { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; background: #e8f6eb; color: #267c3c; font-size: 12px; font-weight: 700; }
.status-list strong { display: block; }
.status-list small { color: var(--muted); }
.mono { font-family: Consolas, "Courier New", monospace; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid var(--line); padding: 11px 12px; text-align: left; vertical-align: top; }
th { background: #edf3f8; color: #405c72; font-size: 12px; font-weight: 700; }
tbody tr:last-child td { border-bottom: 0; }
.audit { position: relative; margin: 0; padding: 0; list-style: none; }
.audit::before { content: ""; position: absolute; top: 8px; bottom: 8px; left: 7px; width: 2px; background: #cbdbe4; }
.audit li { position: relative; padding: 0 0 18px 32px; }
.audit li:last-child { padding-bottom: 0; }
.audit li::before { content: ""; position: absolute; top: 5px; left: 1px; width: 14px; height: 14px; border: 3px solid #fff; border-radius: 50%; background: var(--teal); box-shadow: 0 0 0 1px var(--teal); }
.audit time { color: var(--muted); font: 12px Consolas, monospace; }
.login-shell {
  display: grid;
  grid-template-columns: minmax(360px, 0.78fr) minmax(440px, 1.22fr);
  min-height: 560px;
  border-radius: 12px;
  background: var(--navy);
  box-shadow: 0 16px 38px rgba(7, 43, 82, 0.22);
  overflow: hidden;
}
.login-panel {
  padding: 76px 58px;
  background: radial-gradient(circle at 52% 20%, rgba(75, 219, 255, 0.65), transparent 18%), #073a90;
  color: #fff;
}
.login-panel h2 { margin: 0 0 20px; font-size: 24px; font-weight: 400; }
.login-context { margin-bottom: 15px; border: 1px solid #9fc7ef; padding: 12px; background: #eaf4ff; color: #173f71; }
.login-context strong { display: block; }
.field-label { display: block; margin: 10px 0 5px; color: #dff7ff; font-size: 12px; }
.login-panel input {
  width: 100%;
  height: 44px;
  border: 1px solid #d0d8df;
  border-radius: 2px;
  padding: 0 12px;
  background: #fff;
  color: #63717a;
  font-size: 14px;
}
.login-panel button { width: 100%; height: 42px; margin-top: 16px; border: 0; border-radius: 2px; background: #57bd5d; color: #fff; font-size: 15px; opacity: 0.78; }
.login-panel .help { margin-top: 13px; color: #d7eff8; font-size: 12px; }
.login-hero {
  position: relative;
  display: grid;
  place-items: center;
  overflow: hidden;
  background: linear-gradient(135deg, #245be1, #0d8bf1);
  color: #fff;
  text-align: center;
}
.login-hero::before {
  content: "";
  position: absolute;
  inset: 0;
  opacity: 0.68;
  background: linear-gradient(45deg, transparent 25%, #ffd000 25% 34%, transparent 34% 58%, #27bfc8 58% 68%, transparent 68%), radial-gradient(circle at 72% 34%, #5742dc 0 45px, transparent 46px), radial-gradient(circle at 28% 76%, #082b91 0 55px, transparent 56px);
}
.login-hero-content { position: relative; max-width: 420px; padding: 40px; }
.login-hero h2 { margin: 0; font-size: 30px; line-height: 1.35; text-shadow: 0 2px 4px #17448d; }
.login-hero p { color: #e5f7ff; }
.report-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  border: 1px solid #9ed0aa;
  border-radius: 10px;
  padding: 18px 20px;
  background: linear-gradient(100deg, #e8f7eb, #f8fcf9);
}
.report-banner strong { display: block; color: #216e35; font-size: 18px; }
.report-banner p { margin: 4px 0 0; color: #577463; }
.report-number { color: #24763a; font-size: 28px; font-weight: 700; }
.source-web { color: #126d96; font-weight: 700; }
.source-folder { color: #7b5a16; font-weight: 700; }
.demo-cards a { display: block; min-height: 225px; text-decoration: none; }
.demo-cards a:hover { border-color: #86b8c8; transform: translateY(-2px); }
.demo-card-index { color: var(--teal); font-size: 12px; font-weight: 700; letter-spacing: 0.1em; }
.demo-card-title { margin: 10px 0 6px; font-size: 20px; font-weight: 700; }
.demo-card-copy { color: var(--muted); }
.demo-card-link { display: inline-block; margin-top: 22px; color: var(--blue); font-weight: 700; }
.site-footer { border-top: 1px solid var(--line); padding: 20px 0 30px; color: var(--muted); font-size: 12px; }
@media (max-width: 820px) {
  .header-inner, .hero, .report-banner { align-items: flex-start; flex-direction: column; }
  .grid-2, .grid-3 { grid-template-columns: 1fr; }
  .login-shell { grid-template-columns: 1fr; }
  .login-panel { padding: 42px 30px; }
  .login-hero { min-height: 310px; }
}
"""


NAV_ITEMS = (
    ("index", "演示首页", "index.html"),
    ("teacher", "教师状态", "teacher-status.html"),
    ("student", "学生登录", "student-login.html"),
    ("report", "收卷报告", "collection-report.html"),
)


def _navigation(active: str) -> str:
    links = []
    for key, label, filename in NAV_ITEMS:
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a href="{filename}"{current}>{label}</a>')
    return '<nav class="demo-nav" aria-label="演示状态"><div class="demo-nav-inner">' + "".join(links) + "</div></nav>"


def _page(*, title: str, active: str, eyebrow: str, lead: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="demo-data" content="synthetic-only">
  <title>{title} · Hydro NOI Contest Kit 演示</title>
  <link rel="stylesheet" href="demo.css">
</head>
<body data-demo="synthetic">
  <header class="site-header">
    <div class="header-inner">
      <a class="brand" href="index.html"><span class="brand-mark">NOI</span><span><strong>Hydro NOI Contest Kit</strong><small>开源演示资产</small></span></a>
      <span class="demo-flag">离线静态演示 · example.test</span>
    </div>
    {_navigation(active)}
  </header>
  <main class="page">
    <section class="hero">
      <div><p class="eyebrow">{eyebrow}</p><h1>{title}</h1><p class="lead">{lead}</p></div>
      <div class="synthetic-note">仅含合成数据 · 不连接 OJ、云平台或比赛服务器</div>
    </section>
    {content}
  </main>
  <footer class="site-footer">固定演示数据：<span class="mono">example.test</span> 与 RFC 5737 文档地址 · 由 <span class="mono">scripts/build_demo.py</span> 确定性生成</footer>
</body>
</html>
"""


def _index_page() -> str:
    content = """
    <section class="grid grid-3 demo-cards" aria-label="演示页面">
      <a class="card card-body" href="teacher-status.html"><span class="demo-card-index">01 / TEACHER</span><div class="demo-card-title">教师状态总览</div><p class="demo-card-copy">展示比赛机、普通 OJ、材料、座位池与截止保护的可观察状态。</p><span class="demo-card-link">打开教师演示 →</span></a>
      <a class="card card-body" href="student-login.html"><span class="demo-card-index">02 / STUDENT</span><div class="demo-card-title">学生登录入口</div><p class="demo-card-copy">复现程序回收系统的青蓝登录界面；字段为空且页面不可提交。</p><span class="demo-card-link">打开学生演示 →</span></a>
      <a class="card card-body" href="collection-report.html"><span class="demo-card-index">03 / REPORT</span><div class="demo-card-title">收卷完成报告</div><p class="demo-card-copy">展示冻结、来源选择、合成 RID、回传和入口撤销的审计结果。</p><span class="demo-card-link">打开报告演示 →</span></a>
    </section>
    <section class="card" style="margin-top:18px"><div class="card-head"><h2>演示边界</h2><span class="pill pill-safe">公开安全</span></div><div class="card-body"><p>这些页面不是生产后台快照，也不会读取任何数据库、日志或部署配置。所有比赛、选手、座位和评测标识均以 <span class="mono">DEMO-</span> 开头；网络信息只使用保留域名和文档地址。</p></div></section>
    """
    return _page(
        title="可重复的离线产品演示",
        active="index",
        eyebrow="Synthetic demo gallery",
        lead="三张固定状态页覆盖老师办赛、学生进入回收系统和截止收卷。页面可直接用浏览器打开，也可以按截图规范在固定视口重复生成图片。",
        content=content,
    )


def _teacher_page() -> str:
    content = """
    <section class="grid grid-3" aria-label="核心状态">
      <article class="card metric"><div class="card-body"><div class="metric-label">普通 Hydro OJ</div><div class="metric-value">正常</div><span class="pill pill-ok">独立可用</span><p class="metric-detail"><span class="mono">https://oj.example.test</span><br>健康检查 18 / 18</p></div></article>
      <article class="card metric"><div class="card-body"><div class="metric-label">比赛服务器</div><div class="metric-value">已停止</div><span class="pill pill-safe">StopCharging</span><p class="metric-detail">文档地址 <span class="mono">203.0.113.24</span><br>学生入口返回 503</p></div></article>
      <article class="card metric"><div class="card-body"><div class="metric-label">座位池验收</div><div class="metric-value">17 / 17</div><div class="progress progress-full"><span></span></div><p class="metric-detail">正式座位 15 · 备用座位 2</p></div></article>
    </section>
    <section class="grid grid-2" style="margin-top:18px">
      <article class="card"><div class="card-head"><h2>比赛状态</h2><span class="pill pill-ok">准备完成</span></div><div class="card-body"><ul class="status-list">
        <li><span class="status-icon">✓</span><span><strong>比赛快照已冻结</strong><small>OI 赛制 · 双轨交卷 · 截止 17:00</small></span><span class="mono">DEMO-CONTEST-001</span></li>
        <li><span class="status-icon">✓</span><span><strong>材料已批准</strong><small>试题 PDF、自测包和文件读写映射一致</small></span><span class="pill pill-ok">approved</span></li>
        <li><span class="status-icon">✓</span><span><strong>桌面逐座验收</strong><small>画面、答案目录、试题和自测数据均就绪</small></span><span class="pill pill-ok">verified</span></li>
        <li><span class="status-icon">✓</span><span><strong>截止保护已安装</strong><small>比赛机本地定时器先冻结，再关闭入口</small></span><span class="pill pill-safe">armed</span></li>
      </ul></div></article>
      <article class="card"><div class="card-head"><h2>办赛检查点</h2><span class="mono">exam.example.test</span></div><div class="card-body"><ol class="audit">
        <li><time>08:30:00</time><strong>Doctor 只读检查通过</strong><div class="metric-detail">Hydro、Docker、Caddy 和云权限均满足首个支持 profile。</div></li>
        <li><time>08:42:15</time><strong>座位池完成预热</strong><div class="metric-detail">17 个桌面全部通过独立验收，没有临近开赛集中创建。</div></li>
        <li><time>08:45:00</time><strong>名单绑定完成</strong><div class="metric-detail">15 个合成选手绑定主座位，备用位保持未发放。</div></li>
        <li><time>08:55:00</time><strong>入口等待发放</strong><div class="metric-detail">教师仍需显式确认；演示页面不执行任何真实操作。</div></li>
      </ol></div></article>
    </section>
    """
    return _page(
        title="教师状态总览",
        active="teacher",
        eyebrow="Teacher control plane",
        lead="老师在一页内判断普通 OJ 是否独立可用、比赛机是否处于预期状态，以及材料、座位和截止保护是否具备开赛条件。",
        content=content,
    )


def _student_page() -> str:
    content = """
    <section class="login-shell" aria-label="CSP 程序回收系统登录演示">
      <div class="login-panel">
        <h2>选手登录</h2>
        <div class="login-context"><strong>合成测试赛 · DEMO-CONTEST-001</strong><span>状态：进行中（静态演示）</span></div>
        <div role="form" aria-label="不可提交的登录表单">
          <label class="field-label" for="candidate-demo">准考证号</label>
          <input id="candidate-demo" name="candidate-demo" autocomplete="off" placeholder="示例字段保持为空" disabled>
          <label class="field-label" for="credential-demo">登录口令</label>
          <input id="credential-demo" name="credential-demo" type="password" autocomplete="off" placeholder="演示页面不保存口令" disabled>
          <button type="button" disabled>登录（静态演示）</button>
        </div>
        <p class="help">入口示例：<span class="mono">https://exam.example.test</span><br>此页面没有认证服务，也不会发送表单数据。</p>
      </div>
      <div class="login-hero"><div class="login-hero-content"><p class="eyebrow" style="color:#d6f6ff">Student entry</p><h2>CSP 程序回收系统</h2><p>进入桌面前核对比赛名称与状态。真实系统会在错误账号、错误口令或入口结束时给出明确反馈。</p></div></div>
    </section>
    """
    return _page(
        title="学生登录入口",
        active="student",
        eyebrow="Student submission portal",
        lead="这一状态复用现有程序回收页面的品牌色和布局，但输入框被禁用且没有预填账号、口令或链接凭据。",
        content=content,
    )


def _report_page() -> str:
    content = """
    <section class="report-banner"><div><strong>收卷完成，入口已撤销</strong><p>冻结快照、目录回收、网页版本选择和 Hydro 回传均已完成。</p></div><div class="report-number">2 / 2 成功</div></section>
    <section class="grid grid-3" style="margin-top:18px" aria-label="收卷摘要">
      <article class="card metric"><div class="card-body"><div class="metric-label">合成选手</div><div class="metric-value">DEMO-007</div><p class="metric-detail">座位 <span class="mono">SEAT-DEMO-007</span><br>未展示姓名或源码</p></div></article>
      <article class="card metric"><div class="card-body"><div class="metric-label">冻结时间</div><div class="metric-value">17:00:00</div><span class="pill pill-safe">本地定时器</span><p class="metric-detail">2026-08-09 · UTC+08:00</p></div></article>
      <article class="card metric"><div class="card-body"><div class="metric-label">比赛服务器</div><div class="metric-value">已停止</div><span class="pill pill-ok">入口 503</span><p class="metric-detail">文档地址 <span class="mono">192.0.2.44</span></p></div></article>
    </section>
    <section class="card" style="margin-top:18px"><div class="card-head"><h2>最终版本与 Hydro 回传</h2><span class="pill pill-ok">report complete</span></div><div class="table-wrap"><table><thead><tr><th>题目</th><th>最终来源</th><th>选择规则</th><th>合成 RID</th><th>结果</th></tr></thead><tbody>
      <tr><td><strong>alpha.cpp</strong></td><td><span class="source-web">网页递交</span></td><td>使用最后一次明确递交</td><td class="mono">RID-DEMO-ALPHA-001</td><td><span class="pill pill-ok">已回传</span></td></tr>
      <tr><td><strong>beta.cpp</strong></td><td><span class="source-folder">目录回退</span></td><td>无网页版本，使用冻结目录</td><td class="mono">RID-DEMO-BETA-001</td><td><span class="pill pill-ok">已回传</span></td></tr>
    </tbody></table></div></section>
    <section class="grid grid-2" style="margin-top:18px">
      <article class="card"><div class="card-head"><h2>截止审计</h2><span class="mono">DEMO-COLLECT-001</span></div><div class="card-body"><ol class="audit">
        <li><time>17:00:00</time><strong>全部座位冻结</strong><div class="metric-detail">答案目录进入只读收卷快照。</div></li>
        <li><time>17:00:01</time><strong>学生入口关闭</strong><div class="metric-detail">新连接和既有桌面连接均不再可用。</div></li>
        <li><time>17:00:03</time><strong>最终版本选择完成</strong><div class="metric-detail">逐题记录网页或目录来源及选择原因。</div></li>
        <li><time>17:00:06</time><strong>Hydro 回传确认</strong><div class="metric-detail">两条合成 RID 均取得幂等收据。</div></li>
      </ol></div></article>
      <article class="card"><div class="card-head"><h2>隔离边界</h2><span class="pill pill-ok">已恢复</span></div><div class="card-body"><ul class="status-list">
        <li><span class="status-icon">✓</span><span><strong>普通 OJ 未受影响</strong><small><span class="mono">oj.example.test</span> 持续健康</small></span></li>
        <li><span class="status-icon">✓</span><span><strong>公网桌面规则已撤销</strong><small>比赛入口保持 HTTP 503</small></span></li>
        <li><span class="status-icon">✓</span><span><strong>比赛服务器已停止</strong><small>收卷确认后进入节省停机状态</small></span></li>
      </ul></div></article>
    </section>
    """
    return _page(
        title="收卷完成报告",
        active="report",
        eyebrow="Collection audit report",
        lead="报告把截止冻结、最终版本选择、Hydro 回传、入口撤销和普通 OJ 隔离放在同一条可核对的审计链上。",
        content=content,
    )


def render_assets() -> Mapping[str, str]:
    """Return every generated asset in stable filename order."""
    assets = {
        "collection-report.html": _report_page(),
        "demo.css": DEMO_CSS,
        "index.html": _index_page(),
        "student-login.html": _student_page(),
        "teacher-status.html": _teacher_page(),
    }
    return {
        filename: "\n".join(
            line.rstrip() for line in content.replace("\r\n", "\n").splitlines()
        )
        + "\n"
        for filename, content in assets.items()
    }


def write_assets(output_dir: Path = DEFAULT_OUTPUT) -> tuple[str, ...]:
    """Write changed assets with UTF-8/LF and return changed filenames."""
    output_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for filename, content in render_assets().items():
        path = output_dir / filename
        normalized = content.replace("\r\n", "\n")
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != normalized:
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(normalized)
            changed.append(filename)
    return tuple(changed)


def check_assets(output_dir: Path = DEFAULT_OUTPUT) -> tuple[str, ...]:
    """Return missing or stale generated filenames without changing files."""
    mismatches: list[str] = []
    for filename, expected in render_assets().items():
        path = output_dir / filename
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            mismatches.append(filename)
    return tuple(mismatches)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="asset directory (default: docs/demo)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when checked-in assets differ from the deterministic render",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if args.check:
        mismatches = check_assets(output)
        if mismatches:
            print("demo assets are missing or stale: " + ", ".join(mismatches))
            return 1
        print(f"demo assets are current: {len(render_assets())} files")
        return 0
    changed = write_assets(output)
    if changed:
        print("generated demo assets: " + ", ".join(changed))
    else:
        print(f"demo assets unchanged: {len(render_assets())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
