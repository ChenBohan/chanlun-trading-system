"""
Build self-contained HTML report files that embed all K-line chart data inline.
No external file dependencies - can be sent to any device independently.
"""
import re
import os

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDICES = [
    ("000016", "上证50", "510050"),
    ("000300", "沪深300", "510300"),
    ("000905", "中证500", "510500"),
    ("000852", "中证1000", "512100"),
    ("399006", "创业板指", "159915"),
    ("000688", "科创50", "588000"),
    ("HSTECH", "恒生科技", "513180"),
    ("NDX100", "纳斯达克100", "513100"),
]

def extract_chart_data(code, name):
    """Extract daily, min120, and min30 chart data from per-index HTML files."""
    chart_path = os.path.join(BASE, "indices", f"{code}_{name}", "analysis", "缠论分析图.html")
    if not os.path.exists(chart_path):
        print(f"  [WARN] Chart not found: {chart_path}")
        return None, None, None

    with open(chart_path, "r", encoding="utf-8") as f:
        content = f.read()

    daily_m = re.search(r'(const dailyData\s*=\s*\{.*?\});', content, re.DOTALL)
    min30_m = re.search(r'(const min30Data\s*=\s*\{.*?\});', content, re.DOTALL)
    min120_m = re.search(r'(const min120Data\s*=\s*\{.*?\});', content, re.DOTALL)

    if not daily_m or not min30_m:
        print(f"  [WARN] Could not extract data from {chart_path}")
        return None, None, None

    return (daily_m.group(1),
            min120_m.group(1) if min120_m else None,
            min30_m.group(1))


def read_report_html_sections(report_path):
    """Read report content: try existing HTML <main> first, then fall back to .md."""
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()
        sections_match = re.search(r'(<main.*?</main>)', content, re.DOTALL)
        if sections_match:
            main_html = sections_match.group(1)
            main_html = re.sub(r'<section[^>]*id="ch6".*?</section>', '', main_html, flags=re.DOTALL)
            return main_html

    md_path = os.path.join(os.path.dirname(report_path), "指数轮动分析报告.md")
    if not os.path.exists(md_path):
        md_path = os.path.join(os.path.dirname(os.path.dirname(report_path)), "指数轮动分析报告.md")
    if os.path.exists(md_path):
        return _md_to_html_sections(md_path)
    return ""


def _md_to_html_sections(md_path):
    """Convert markdown report to HTML sections for embedding."""
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    html_parts = ['<main class="report-embedded">']
    in_table = False
    table_buf = []

    for line in lines:
        line = line.rstrip()

        if line.startswith("# "):
            continue
        if line.startswith("> "):
            html_parts.append(f'<p class="text-muted">{_escape(line[2:])}</p>')
            continue
        if line == "---":
            html_parts.append("<hr>")
            continue

        if line.startswith("## "):
            html_parts.append(f'<h2 class="section-title">{_escape(line[3:])}</h2>')
            continue
        if line.startswith("### "):
            html_parts.append(f'<h3>{_escape(line[4:])}</h3>')
            continue

        if "|" in line and line.strip().startswith("|"):
            if not in_table:
                in_table = True
                table_buf = []
            table_buf.append(line)
            continue
        elif in_table:
            html_parts.append(_render_md_table(table_buf))
            in_table = False
            table_buf = []

        if line.startswith("- "):
            content = line[2:]
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            html_parts.append(f"<p>{content}</p>")
            continue
        if line.strip():
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
            html_parts.append(f"<p>{content}</p>")

    if in_table:
        html_parts.append(_render_md_table(table_buf))

    html_parts.append("</main>")
    return "\n".join(html_parts)


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_md_table(rows):
    if len(rows) < 2:
        return ""
    parts = ["<table>"]
    headers = [c.strip() for c in rows[0].split("|")[1:-1]]
    parts.append("<thead><tr>")
    for h in headers:
        parts.append(f"<th>{h}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows[2:]:
        cells = [c.strip() for c in row.split("|")[1:-1]]
        parts.append("<tr>")
        for c in cells:
            c = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', c)
            parts.append(f"<td>{c}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def build_standalone_html(date_str="20260331"):
    print("Building self-contained HTML reports...")
    report_dir = os.path.join(BASE, "indices", "reports", date_str)
    os.makedirs(report_dir, exist_ok=True)

    all_data = {}
    has_any_120 = False
    for code, name, etf in INDICES:
        print(f"  Extracting data: {name} ({code})...")
        daily, min120, min30 = extract_chart_data(code, name)
        if daily and min30:
            safe_key = code
            entry = {
                "name": name,
                "code": code,
                "etf": etf,
                "daily_js": daily.replace("const dailyData", f"const daily_{safe_key}"),
                "min30_js": min30.replace("const min30Data", f"const min30_{safe_key}"),
                "min120_js": None,
            }
            if min120:
                entry["min120_js"] = min120.replace("const min120Data", f"const min120_{safe_key}")
                has_any_120 = True
            all_data[safe_key] = entry

    if not all_data:
        print("  [ERROR] No chart data extracted!")
        return

    existing_report = os.path.join(report_dir, "指数轮动分析图.html")
    report_sections = read_report_html_sections(existing_report)

    index_tabs_html = ""
    for code, name, etf in INDICES:
        if code in all_data:
            active = " active" if code == INDICES[0][0] else ""
            index_tabs_html += f'      <div class="idx-tab{active}" onclick="switchIndex(\'{code}\')">{name}</div>\n'

    data_js_blocks = []
    for code in all_data:
        d = all_data[code]
        data_js_blocks.append(d["daily_js"] + ";")
        if d["min120_js"]:
            data_js_blocks.append(d["min120_js"] + ";")
        data_js_blocks.append(d["min30_js"] + ";")

    data_map_entries = []
    for code in all_data:
        d = all_data[code]
        min120_ref = f"min120_{code}" if d["min120_js"] else "null"
        data_map_entries.append(
            f'  "{code}": {{ name: "{d["name"]}", code: "{code}", etf: "{d["etf"]}", '
            f'daily: daily_{code}, min120: {min120_ref}, min30: min30_{code} }}'
        )
    data_map_js = "const INDEX_DATA = {\n" + ",\n".join(data_map_entries) + "\n};"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股宽基指数轮动分析报告 — {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}（自包含版）</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #0d1117;
  color: #c9d1d9;
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.6;
}}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
h1 {{ color: #58a6ff; font-size: 22px; margin-bottom: 8px; }}
h2 {{ color: #58a6ff; font-size: 18px; margin: 24px 0 12px; }}
h3 {{ color: #c9d1d9; font-size: 15px; margin: 16px 0 8px; }}
.meta {{ color: #8b949e; font-size: 13px; margin-bottom: 20px; }}
.section {{ margin-bottom: 32px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin: 12px 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }}
th {{ background: #161b22; color: #58a6ff; padding: 8px; text-align: left; border-bottom: 2px solid #30363d; }}
td {{ padding: 8px; border-bottom: 1px solid #21262d; }}
tr:hover {{ background: #1c2333; }}
.text-muted {{ color: #8b949e; font-size: 13px; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
.badge-buy {{ background: rgba(248,81,73,0.2); color: #f85149; }}
.badge-sell {{ background: rgba(63,185,80,0.2); color: #3fb950; }}
.wolf-warn {{ background: rgba(210,153,34,0.15); border: 1px solid rgba(210,153,34,0.4); border-radius: 8px; padding: 12px 16px; margin: 12px 0; color: #d29922; }}

/* Chart section */
.chart-section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; margin: 20px 0; overflow: hidden; }}
.idx-tabs {{ display: flex; gap: 0; border-bottom: 1px solid #21262d; padding: 0; flex-wrap: wrap; }}
.idx-tab {{ padding: 10px 16px; cursor: pointer; color: #8b949e; border-bottom: 2px solid transparent; font-size: 14px; white-space: nowrap; }}
.idx-tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; background: rgba(88,166,255,0.05); }}
.idx-tab:hover {{ color: #c9d1d9; }}
.level-tabs {{ display: flex; gap: 0; border-bottom: 1px solid #21262d; padding: 0 16px; background: #0d1117; }}
.level-tab {{ padding: 8px 16px; cursor: pointer; color: #8b949e; border-bottom: 2px solid transparent; font-size: 13px; }}
.level-tab.active {{ color: #f0883e; border-bottom-color: #f0883e; }}
.level-tab:hover {{ color: #c9d1d9; }}
.chart-area {{ padding: 16px; }}
canvas {{ display: block; width: 100%; background: #0d1117; border-radius: 6px; }}
.info-panel {{ display: flex; gap: 16px; flex-wrap: wrap; padding: 12px 16px; }}
.info-card {{ background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px 14px; min-width: 120px; }}
.info-card .label {{ font-size: 11px; color: #8b949e; text-transform: uppercase; }}
.info-card .value {{ font-size: 16px; font-weight: 600; margin-top: 2px; }}
.info-card .value.up {{ color: #f85149; }}
.info-card .value.down {{ color: #3fb950; }}
.bsp-list {{ padding: 8px 16px 16px; }}
.bsp-item {{ display: flex; align-items: center; gap: 10px; padding: 6px 10px; border-radius: 6px; margin-bottom: 2px; font-size: 13px; }}
.bsp-item:hover {{ background: #0d1117; }}
.bsp-badge {{ padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
.bsp-badge.buy {{ background: rgba(248,81,73,0.2); color: #f85149; }}
.bsp-badge.sell {{ background: rgba(63,185,80,0.2); color: #3fb950; }}
.bsp-date {{ color: #8b949e; font-size: 12px; width: 110px; }}
.bsp-price {{ font-weight: 600; width: 60px; }}
.bsp-desc {{ color: #8b949e; font-size: 12px; flex: 1; }}
.bsp-conf {{ font-size: 11px; }}
.legend {{ display: flex; gap: 16px; padding: 8px 16px; flex-wrap: wrap; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: #8b949e; }}
.legend-color {{ width: 14px; height: 14px; border-radius: 2px; }}

.footer-disclaimer {{
  margin-top: 40px;
  padding: 20px;
  border-top: 1px solid #30363d;
  color: #8b949e;
  font-size: 12px;
  text-align: center;
}}

/* Report sections reuse */
.section-title {{ color: #58a6ff; font-size: 18px; margin: 24px 0 12px; padding-bottom: 8px; border-bottom: 1px solid #21262d; }}
.scenario-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
.scenario-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
.sc-prob {{ font-size: 22px; font-weight: 700; margin: 8px 0; }}

/* Mobile responsive */
@media (max-width: 768px) {{
  .container {{ padding: 10px; }}
  h1 {{ font-size: 18px; }}
  .idx-tab {{ padding: 8px 10px; font-size: 12px; }}
  .info-panel {{ gap: 8px; }}
  .info-card {{ min-width: 90px; padding: 8px 10px; }}
  .info-card .value {{ font-size: 14px; }}
  .bsp-item {{ flex-wrap: wrap; gap: 4px; }}
  .bsp-desc {{ width: 100%; }}
  table {{ font-size: 11px; display: block; overflow-x: auto; }}
  .scenario-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="container">
  <h1>A股宽基指数轮动分析 — 缠论多级别K线图</h1>
  <div class="meta">
    分析时间：{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} |
    自包含版本（所有数据内嵌，可独立查看）|
    8只指数（6A股宽基 + 2跨市场）× 3个级别（日线 + 120分钟 + 30分钟）
  </div>

  <!-- K-Line Charts Section -->
  <div class="chart-section">
    <div class="idx-tabs" id="idxTabs">
{index_tabs_html}    </div>
    <div class="level-tabs" id="levelTabs">
      <div class="level-tab active" onclick="switchLevel('daily')">日线级别</div>
      {'<div class="level-tab" onclick="switchLevel(' + "'min120'" + ')">120分钟级别</div>' if has_any_120 else ''}
      <div class="level-tab" onclick="switchLevel('min30')">30分钟级别</div>
    </div>
    <div class="info-panel" id="infoPanel"></div>
    <div class="chart-area">
      <canvas id="klineCanvas" height="400"></canvas>
    </div>
    <div class="legend">
      <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>阳线（上涨）</div>
      <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>阴线（下跌）</div>
      <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>笔</div>
      <div class="legend-item"><div class="legend-color" style="background:rgba(88,166,255,0.4)"></div>中枢</div>
      <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>买点 ▲</div>
      <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>卖点 ▼</div>
    </div>
    <div class="chart-area">
      <canvas id="macdCanvas" height="150"></canvas>
    </div>
    <div class="legend">
      <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div>DIF</div>
      <div class="legend-item"><div class="legend-color" style="background:#f0883e"></div>DEA</div>
    </div>
    <div class="bsp-list" id="bspList"></div>
  </div>

  <!-- Report sections (chapters 1-5) -->
  <div id="reportContent">
  {report_sections}
  </div>

  <footer class="footer-disclaimer">
    <strong>重要声明</strong>：本分析<strong>完全基于缠论技术方法</strong>（缠中说禅理论体系），不使用动量、量价等非缠论指标。<br>
    走势推演为概率判断，实际走势可能偏离任何预设场景。<br>
    指数ETF投资也有风险，请根据自身风险承受能力做出决策。严格执行操作纪律和止损规则比预测走势更重要。<br><br>
    自包含版本 — 所有K线数据与渲染代码已内嵌，无需外部文件依赖。
  </footer>
</div>

<script>
// === All Index Data ===
{chr(10).join(data_js_blocks)}

{data_map_js}

let currentIndex = '{INDICES[0][0]}';
let currentLevel = 'daily';
let viewStart = 0;
let viewEnd = 0;
let isDragging = false;
let dragStartX = 0;
let dragStartView = 0;
let pinchStartDist = 0;
let pinchStartRange = 0;
const MIN_VIEW = 20;

function resetView() {{
  const data = getData();
  if (!data) return;
  viewStart = 0;
  viewEnd = data.klines.length;
}}

function clampView(total) {{
  const range = viewEnd - viewStart;
  if (range < MIN_VIEW) {{
    const mid = (viewStart + viewEnd) / 2;
    viewStart = Math.round(mid - MIN_VIEW / 2);
    viewEnd = viewStart + MIN_VIEW;
  }}
  if (viewStart < 0) {{ viewStart = 0; viewEnd = Math.min(range, total); }}
  if (viewEnd > total) {{ viewEnd = total; viewStart = Math.max(0, total - range); }}
}}

function switchIndex(code) {{
  currentIndex = code;
  document.querySelectorAll('.idx-tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  resetView();
  render();
}}

function switchLevel(level) {{
  currentLevel = level;
  document.querySelectorAll('.level-tab').forEach(t => t.classList.remove('active'));
  const tabs = document.querySelectorAll('.level-tab');
  const levelOrder = ['daily'];
  if (tabs.length === 3) levelOrder.push('min120');
  levelOrder.push('min30');
  const idx = levelOrder.indexOf(level);
  if (idx >= 0 && tabs[idx]) tabs[idx].classList.add('active');
  resetView();
  render();
}}

function getData() {{
  const idx = INDEX_DATA[currentIndex];
  if (!idx) return null;
  if (currentLevel === 'daily') return idx.daily;
  if (currentLevel === 'min120' && idx.min120) return idx.min120;
  return idx.min30;
}}

function render() {{
  const data = getData();
  if (!data) return;
  if (viewEnd === 0) viewEnd = data.klines.length;
  renderInfo(data);
  renderKline(data);
  renderMACD(data);
  renderBSP(data);
}}

function renderInfo(data) {{
  const k = data.klines;
  if (!k.length) return;
  const idx = INDEX_DATA[currentIndex];
  const last = k[k.length-1];
  const prev = k.length > 1 ? k[k.length-2] : last;
  const change = last[2] - prev[2];
  const changePct = (change / prev[2] * 100);
  const cls = change >= 0 ? 'up' : 'down';
  const sign = change >= 0 ? '+' : '';
  const rangeInfo = viewEnd - viewStart < k.length
    ? `<div class="info-card"><div class="label">显示范围</div><div class="value">${{viewStart+1}}~${{viewEnd}}/${{k.length}}</div></div>` : '';
  document.getElementById('infoPanel').innerHTML = `
    <div class="info-card"><div class="label">${{idx.name}}(${{idx.code}})</div><div class="value ${{cls}}">${{last[2].toFixed(2)}}</div></div>
    <div class="info-card"><div class="label">涨跌</div><div class="value ${{cls}}">${{sign}}${{change.toFixed(2)}} (${{sign}}${{changePct.toFixed(2)}}%)</div></div>
    <div class="info-card"><div class="label">走势类型</div><div class="value">${{data.trend}}</div></div>
    <div class="info-card"><div class="label">中枢数</div><div class="value">${{data.hubs.length}}</div></div>
    <div class="info-card"><div class="label">笔数</div><div class="value">${{data.strokes.length}}</div></div>
    <div class="info-card"><div class="label">买卖点</div><div class="value">${{data.bsp.length}}</div></div>
    ${{rangeInfo}}
  `;
}}

function renderKline(data) {{
  const canvas = document.getElementById('klineCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const H = Math.min(rect.height || 400, 400);
  canvas.width = rect.width * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const allK = data.klines;
  if (!allK.length) return;
  const k = allK.slice(viewStart, viewEnd);
  if (!k.length) return;
  const pad = {{t:20, b:30, l:60, r:20}};
  const cw = (W - pad.l - pad.r) / k.length;
  const allH = k.map(x => x[3]);
  const allL = k.map(x => x[4]);
  const maxP = Math.max(...allH) * 1.01;
  const minP = Math.min(...allL) * 0.99;
  const scaleY = p => pad.t + (maxP - p) / (maxP - minP) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;

  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 0.5;
  for (let i = 0; i < 5; i++) {{
    const y = pad.t + i * (H - pad.t - pad.b) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W-pad.r, y); ctx.stroke();
    const p = maxP - i * (maxP - minP) / 4;
    ctx.fillStyle = '#8b949e'; ctx.font = '11px monospace'; ctx.textAlign = 'right';
    ctx.fillText(p.toFixed(2), pad.l - 6, y + 4);
  }}

  const dateIdx = {{}};
  allK.forEach((kk, i) => dateIdx[kk[0]] = i);
  const localIdx = {{}};
  k.forEach((kk, i) => localIdx[kk[0]] = i);

  data.hubs.forEach(h => {{
    const si = localIdx[h.start];
    const ei = localIdx[h.end];
    const gsi = dateIdx[h.start] ?? 0;
    const gei = dateIdx[h.end] ?? allK.length - 1;
    if (gei < viewStart || gsi >= viewEnd) return;
    const lsi = si !== undefined ? si : 0;
    const lei = ei !== undefined ? ei : k.length - 1;
    const x1 = scaleX(lsi) - cw/2;
    const x2 = scaleX(lei) + cw/2;
    ctx.fillStyle = 'rgba(88,166,255,0.08)';
    ctx.fillRect(x1, scaleY(h.zg), x2-x1, scaleY(h.zd)-scaleY(h.zg));
    ctx.strokeStyle = 'rgba(88,166,255,0.4)'; ctx.lineWidth = 1;
    ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zg)); ctx.lineTo(x2, scaleY(h.zg)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x1, scaleY(h.zd)); ctx.lineTo(x2, scaleY(h.zd)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(`ZG=${{h.zg.toFixed(1)}}`, x2+3, scaleY(h.zg)+3);
    ctx.fillText(`ZD=${{h.zd.toFixed(1)}}`, x2+3, scaleY(h.zd)+12);
  }});

  k.forEach((kk, i) => {{
    const [dt, o, c, hi, lo] = kk;
    const x = scaleX(i);
    const bw = Math.max(cw * 0.6, 1);
    const isUp = c >= o;
    ctx.strokeStyle = ctx.fillStyle = isUp ? '#f85149' : '#3fb950';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, scaleY(hi)); ctx.lineTo(x, scaleY(lo)); ctx.stroke();
    const top = scaleY(Math.max(o, c));
    const bot = scaleY(Math.min(o, c));
    const bh = Math.max(bot - top, 1);
    ctx.fillRect(x-bw/2, top, bw, bh);
  }});

  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.5;
  data.strokes.forEach(s => {{
    const si = localIdx[s.start];
    const ei = localIdx[s.end];
    const gsi = dateIdx[s.start] ?? 0;
    const gei = dateIdx[s.end] ?? 0;
    if (gei < viewStart && gsi < viewStart) return;
    if (gsi >= viewEnd && gei >= viewEnd) return;
    const x1 = si !== undefined ? scaleX(si) : (gsi < viewStart ? pad.l : W - pad.r);
    const y1 = scaleY(s.startP);
    const x2 = ei !== undefined ? scaleX(ei) : (gei >= viewEnd ? W - pad.r : pad.l);
    const y2 = scaleY(s.endP);
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }});

  data.bsp.forEach(p => {{
    const pi = localIdx[p.date];
    if (pi === undefined) return;
    const x = scaleX(pi);
    const y = scaleY(p.price);
    const isBuy = p.type.includes('B');
    ctx.beginPath();
    if (isBuy) {{
      ctx.moveTo(x, y+12); ctx.lineTo(x-7, y+22); ctx.lineTo(x+7, y+22); ctx.closePath();
      ctx.fillStyle = '#f85149'; ctx.fill();
    }} else {{
      ctx.moveTo(x, y-12); ctx.lineTo(x-7, y-22); ctx.lineTo(x+7, y-22); ctx.closePath();
      ctx.fillStyle = '#3fb950'; ctx.fill();
    }}
    ctx.fillStyle = isBuy ? '#f85149' : '#3fb950';
    ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(p.label.substring(0,2), x, isBuy ? y+34 : y-24);
  }});

  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
  const step = Math.max(Math.floor(k.length / 8), 1);
  for (let i = 0; i < k.length; i += step) {{
    const label = k[i][0].length > 10 ? k[i][0].substring(5) : k[i][0].substring(5);
    ctx.fillText(label, scaleX(i), H - 8);
  }}
}}

function renderMACD(data) {{
  const canvas = document.getElementById('macdCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const H = Math.min(rect.height || 150, 150);
  canvas.width = rect.width * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  const allK = data.klines;
  if (!allK.length) return;
  const k = allK.slice(viewStart, viewEnd);
  if (!k.length) return;
  const pad = {{t:10, b:20, l:60, r:20}};
  const cw = (W - pad.l - pad.r) / k.length;

  const macds = k.map(x => x[6]);
  const difs = k.map(x => x[7]);
  const deas = k.map(x => x[8]);
  const allVals = [...macds, ...difs, ...deas];
  const maxV = Math.max(...allVals.map(Math.abs)) * 1.1 || 1;

  const scaleY = v => pad.t + (maxV - v) / (2 * maxV) * (H - pad.t - pad.b);
  const scaleX = i => pad.l + i * cw + cw / 2;
  const zeroY = scaleY(0);

  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.l, zeroY); ctx.lineTo(W-pad.r, zeroY); ctx.stroke();
  ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace'; ctx.textAlign = 'right';
  ctx.fillText('0', pad.l - 6, zeroY + 3);

  k.forEach((kk, i) => {{
    const m = kk[6];
    const x = scaleX(i);
    const bw = Math.max(cw * 0.5, 1);
    ctx.fillStyle = m >= 0 ? '#f85149' : '#3fb950';
    const y1 = zeroY;
    const y2 = scaleY(m);
    ctx.fillRect(x - bw/2, Math.min(y1,y2), bw, Math.abs(y2-y1) || 1);
  }});

  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {{
    const x = scaleX(i), y = scaleY(kk[7]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();

  ctx.strokeStyle = '#f0883e'; ctx.lineWidth = 1.2;
  ctx.beginPath();
  k.forEach((kk, i) => {{
    const x = scaleX(i), y = scaleY(kk[8]);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();

  ctx.fillStyle = '#58a6ff'; ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
  ctx.fillText('DIF', W-pad.r-80, pad.t+12);
  ctx.fillStyle = '#f0883e';
  ctx.fillText('DEA', W-pad.r-40, pad.t+12);
}}

// === Zoom & Pan ===
function setupInteraction() {{
  const kCanvas = document.getElementById('klineCanvas');
  const mCanvas = document.getElementById('macdCanvas');

  function handleWheel(e) {{
    e.preventDefault();
    const data = getData();
    if (!data) return;
    const total = data.klines.length;
    const range = viewEnd - viewStart;
    const rect = kCanvas.getBoundingClientRect();
    const mouseX = (e.clientX - rect.left) / rect.width;
    const zoomFactor = e.deltaY > 0 ? 1.15 : 0.87;
    const newRange = Math.round(Math.max(MIN_VIEW, Math.min(total, range * zoomFactor)));
    const pivot = viewStart + Math.round(mouseX * range);
    viewStart = Math.round(pivot - mouseX * newRange);
    viewEnd = viewStart + newRange;
    clampView(total);
    render();
  }}

  function handleMouseDown(e) {{
    isDragging = true;
    dragStartX = e.clientX;
    dragStartView = viewStart;
    kCanvas.style.cursor = 'grabbing';
  }}

  function handleMouseMove(e) {{
    if (!isDragging) return;
    const data = getData();
    if (!data) return;
    const total = data.klines.length;
    const rect = kCanvas.getBoundingClientRect();
    const range = viewEnd - viewStart;
    const dx = e.clientX - dragStartX;
    const shift = Math.round(-dx / rect.width * range);
    viewStart = dragStartView + shift;
    viewEnd = viewStart + range;
    clampView(total);
    render();
  }}

  function handleMouseUp() {{
    isDragging = false;
    kCanvas.style.cursor = 'crosshair';
  }}

  function handleTouchStart(e) {{
    if (e.touches.length === 2) {{
      pinchStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      pinchStartRange = viewEnd - viewStart;
    }} else if (e.touches.length === 1) {{
      isDragging = true;
      dragStartX = e.touches[0].clientX;
      dragStartView = viewStart;
    }}
  }}

  function handleTouchMove(e) {{
    e.preventDefault();
    const data = getData();
    if (!data) return;
    const total = data.klines.length;

    if (e.touches.length === 2) {{
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      const scale = pinchStartDist / dist;
      const newRange = Math.round(Math.max(MIN_VIEW, Math.min(total, pinchStartRange * scale)));
      const mid = Math.round((viewStart + viewEnd) / 2);
      viewStart = Math.round(mid - newRange / 2);
      viewEnd = viewStart + newRange;
      clampView(total);
      render();
    }} else if (e.touches.length === 1 && isDragging) {{
      const rect = kCanvas.getBoundingClientRect();
      const range = viewEnd - viewStart;
      const dx = e.touches[0].clientX - dragStartX;
      const shift = Math.round(-dx / rect.width * range);
      viewStart = dragStartView + shift;
      viewEnd = viewStart + range;
      clampView(total);
      render();
    }}
  }}

  function handleTouchEnd() {{
    isDragging = false;
  }}

  [kCanvas, mCanvas].forEach(c => {{
    c.addEventListener('wheel', handleWheel, {{passive: false}});
    c.style.cursor = 'crosshair';
    c.style.touchAction = 'none';
  }});
  kCanvas.addEventListener('mousedown', handleMouseDown);
  document.addEventListener('mousemove', handleMouseMove);
  document.addEventListener('mouseup', handleMouseUp);
  kCanvas.addEventListener('touchstart', handleTouchStart, {{passive: false}});
  kCanvas.addEventListener('touchmove', handleTouchMove, {{passive: false}});
  kCanvas.addEventListener('touchend', handleTouchEnd);

  kCanvas.addEventListener('dblclick', () => {{
    resetView();
    render();
  }});
}}

function renderBSP(data) {{
  const list = document.getElementById('bspList');
  if (!data.bsp.length) {{
    list.innerHTML = '<div style="color:#8b949e;padding:10px">当前级别未识别出标准买卖点</div>';
    return;
  }}
  list.innerHTML = data.bsp.map(p => {{
    const isBuy = p.type.includes('B');
    const cls = isBuy ? 'buy' : 'sell';
    const stars = p.conf === 'high' ? '⭐⭐⭐' : p.conf === 'medium' ? '⭐⭐' : '⭐';
    return `<div class="bsp-item">
      <span class="bsp-badge ${{cls}}">${{p.label.substring(0,2)}}</span>
      <span class="bsp-date">${{p.date}}</span>
      <span class="bsp-price" style="color:${{isBuy?'#f85149':'#3fb950'}}">${{p.price.toFixed(2)}}</span>
      <span class="bsp-desc">${{p.desc}}</span>
      <span class="bsp-conf">${{stars}}</span>
    </div>`;
  }}).join('');
}}

window.addEventListener('load', () => {{
  resetView();
  render();
  setupInteraction();
}});
window.addEventListener('resize', render);
</script>
</body>
</html>"""

    desktop_path = os.path.join(report_dir, "指数轮动分析图.html")
    with open(desktop_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Written: {desktop_path} ({len(html)} bytes)")

    mobile_css = """
<style>
/* Mobile-forced overrides: always apply mobile layout */
.container { padding: 10px !important; }
h1 { font-size: 18px !important; }
h2, .section-title { font-size: 15px !important; }
.idx-tab { padding: 8px 10px !important; font-size: 12px !important; }
.level-tab { padding: 6px 12px !important; font-size: 12px !important; }
.info-panel { gap: 6px !important; }
.info-card { min-width: 80px !important; padding: 6px 8px !important; }
.info-card .value { font-size: 14px !important; }
.info-card .label { font-size: 10px !important; }
.bsp-item { flex-wrap: wrap !important; gap: 4px !important; font-size: 12px !important; }
.bsp-desc { width: 100% !important; }
table { font-size: 11px !important; display: block !important; overflow-x: auto !important; }
th, td { padding: 4px 6px !important; }
.scenario-grid { grid-template-columns: 1fr !important; }
.card { padding: 10px !important; }
canvas { max-height: 320px !important; }
.legend { gap: 8px !important; padding: 6px 10px !important; }
.legend-item { font-size: 11px !important; }
.footer-disclaimer { font-size: 11px !important; padding: 12px !important; }
</style>
"""
    mobile_html = html.replace(
        "</head>",
        mobile_css + "</head>"
    ).replace(
        "（自包含版）",
        "（移动版）"
    )

    mobile_path = os.path.join(report_dir, "指数轮动分析报告-移动版.html")
    with open(mobile_path, "w", encoding="utf-8") as f:
        f.write(mobile_html)
    print(f"  Written: {mobile_path} ({len(mobile_html)} bytes, mobile-optimized)")

    latest_dir = os.path.join(BASE, "indices", "reports")
    for fname in ["指数轮动分析图.html", "指数轮动分析报告-移动版.html"]:
        src = os.path.join(report_dir, fname)
        dst = os.path.join(latest_dir, fname)
        import shutil
        shutil.copy2(src, dst)
        print(f"  Copied to latest: {dst}")

    print("Done!")


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "20260331"
    build_standalone_html(date)
