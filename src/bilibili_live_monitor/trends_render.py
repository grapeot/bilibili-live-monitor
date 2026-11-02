"""无外部资源的HTML/SVG报告；仅消费冻结数据。"""
import html
import math
from datetime import datetime
from zoneinfo import ZoneInfo

STYLE = '''body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
background:#f3f6fb;color:#16243b;margin:0;padding:26px}main{max-width:1120px;margin:auto}
h1{font-size:27px;margin:0 0 8px}p{color:#53637a;font-size:14px;line-height:1.7}
section{background:white;border:1px solid #e0e6ef;border-radius:14px;padding:18px 18px 8px;
margin:16px 0;box-shadow:0 4px 16px #18243b05}h2{font-size:18px;margin:0 0 12px}
small{font-size:12px;color:#69788d;font-weight:400;margin-left:16px}svg{width:100%;display:block}
svg text{fill:#748094;font-size:12px}.grid{stroke:#e7edf4;stroke-width:1}a{color:#2563eb}
footer{margin:20px 0;font-size:13px;color:#617089;line-height:1.8}
@media(max-width:600px){body{padding:12px}section{padding:12px 6px;overflow-x:auto}
small{display:block;margin:7px 0 0}svg{min-width:680px}}'''


def render(data):
    zone = ZoneInfo(data['timezone'])
    lo = datetime.fromisoformat(data['since']).timestamp()
    hi = datetime.fromisoformat(data['until']).timestamp()
    summary = data['summary']
    def esc(text):
        return html.escape(str(text), quote=True)

    def clock(value, full=False):
        return datetime.fromtimestamp(value, zone).strftime('%Y-%m-%d %H:%M:%S' if full else '%H:%M:%S')

    def chart(name, points, color, mode):
        left, right, top, bottom = 65, 1045, 16, 207
        values = [p['v'] for p in points if p['v'] is not None]
        minimum, maximum = min(values, default=0), max(values, default=1)
        low = max(0, math.floor(minimum - max(1, (maximum-minimum)*.15))) if mode == 'stock' else 0
        high = max(low+1, math.ceil(maximum + max(1, (maximum-low)*.12)))
        magnitude = 10**math.floor(math.log10(max(1, (high-low)/4)))
        spacing = next(s*magnitude for s in (1, 2, 5, 10) if s*magnitude >= (high-low)/4)
        low, high = math.floor(low/spacing)*spacing, math.ceil(high/spacing)*spacing
        def x(t):
            return left + (t-lo)/(hi-lo)*(right-left)

        def y(v):
            return bottom - (v-low)/(high-low)*(bottom-top)
        parts = [f'<svg viewBox="0 0 1080 252" role="img" aria-label="{esc(name)}">']
        for value in range(low, high+1, spacing):
            yy = y(value)
            parts.append(f'<path class="grid" d="M {left} {yy} H {right}"/>'
                         f'<text x="52" y="{yy+4}" text-anchor="end">{value}</text>')
        for i in range(6):
            t = lo + (hi-lo)*i/5
            parts.append(f'<text x="{x(t)}" y="233" text-anchor="middle">{clock(t)[:5]}</text>')
        for gap in summary['gaps']:
            parts.append(f'<rect x="{x(gap["start"])}" y="{top}" '
                         f'width="{x(gap["end"])-x(gap["start"])}" height="{bottom-top}" '
                         f'fill="#94a3b8" opacity=".22"><title>可疑采集缺口 '
                         f'{clock(gap["start"],True)}—{clock(gap["end"],True)}</title></rect>')
        if not values:
            parts.append('<text x="550" y="110" text-anchor="middle">无观测数据</text>')
        path = ''
        previous = None
        for point in points:
            t, value = point['t'], point['v']
            if value is None or (mode == 'line' and point.get('partial')):
                previous = None
                continue
            if mode == 'bar':
                opacity = '.4' if point['partial'] else '1'
                parts.append(f'<rect x="{x(t)+1}" y="{y(value)}" '
                             f'width="{max(.5,x(point["end"])-x(t)-2)}" height="{bottom-y(value)}" '
                             f'fill="{color}" opacity="{opacity}"><title>{esc(point["label"])}</title></rect>')
                continue
            separated = previous is None or (mode == 'line' and any(
                g['start'] < t and g['end'] > previous for g in summary['gaps']))
            path += f' M {x(t)} {y(value)}' if separated else (
                f' H {x(t)} V {y(value)}' if mode == 'stock' else f' L {x(t)} {y(value)}')
            previous = t
        if path:
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.8"/>')
        if mode != 'bar':
            for point in points:
                if point['v'] is None:
                    continue
                fill = 'white' if point.get('partial') else color
                parts.append(f'<circle cx="{x(point["t"])}" cy="{y(point["v"])}" r="3" '
                             f'fill="{fill}" stroke="{color}"><title>{esc(point["label"])}</title></circle>')
        if mode == 'stock' and points:
            last = points[-1]
            parts.append(f'<path d="M {x(last["t"])} {y(last["v"])} H {right}" '
                         f'fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="4 4"/>')
        parts.append('</svg>')
        return ''.join(parts)

    cards = []
    for field, name, color, mode in [('unique_entries', '入场流量', '#2563eb', 'line'),
                                    ('gifts', '送礼频次', '#e79024', 'bar')]:
        points = []
        for bucket in summary['bins']:
            observed = bucket['raw_messages'] or bucket['entries'] or bucket['gifts']
            v = bucket[field] if observed else None
            points.append({'t': bucket['start'] if mode == 'bar' else (bucket['start']+bucket['end'])/2,
                           'end': bucket['end'], 'v': v, 'partial': bucket['partial'],
                           'label': f'{clock(bucket["start"],True)}—{clock(bucket["end"])}：'
                                    f'{v if v is not None else "无观测"}' +
                                    ('（不完整桶）' if bucket['partial'] else '')})
        unit = f'每{data["bin_seconds"]}秒' + ('可识别入场人数（桶内去重）' if mode == 'line'
                                            else '送礼事件数（含SC，同单合并）')
        cards.append(f'<section><h2>{name}<small>{unit}</small></h2>{chart(name,points,color,mode)}</section>')
    for field, name, color in [('followers', '关注总数', '#8b5cf6'), ('fans_club', '粉丝团人数', '#e2578b')]:
        points = [{'t': row['t'], 'v': row[field], 'label': f'{clock(row["t"],True)}：{row[field]}'}
                  for row in data['source']['stocks'] if row[field] is not None]
        note = (f'最新 {points[-1]["v"]} · 推送于 {clock(points[-1]["t"])} · 总量（纵轴不从零起）'
                if points else '无观测数据')
        cards.append(f'<section><h2>{name}<small>{note}</small></h2>{chart(name,points,color,"stock")}</section>')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(data["title"])} · 直播趋势</title><style>{STYLE}</style><main>'
            f'<h1>{esc(data["title"])} · 直播趋势</h1><p>{clock(lo,True)}—{clock(hi,True)} '
            f'（{esc(data["timezone"])}）<br>范围内已观测入场账号 {summary["unique_entry_uids"]:,} · '
            f'送礼事件 {summary["gift_events"]} · <a href="data.json" download>下载冻结数据</a></p>' +
            ''.join(cards) + '<footer>点或柱悬停可查看数值。关注与粉丝团显示平台总量原值；'
            '点间沿用上一观测值，首点前不补值，末点后虚线延续。<br>'
            '入场在桶内按UID去重，跨桶可重复；匿名入场不计入人数，明细计数在JSON。'
            '送礼按订单/事件计次，含免费礼物、SC与上舰，不按件数或金额计数。<br>'
            '空心点、浅色柱为不足完整桶宽的首尾桶；无业务报文的空桶显示缺测，不填零。'
            '灰带为长时间无业务报文的可疑缺口，不能据此断言停播或真实低流量。'
            '所有横轴按接收时刻对齐。这是冻结快照，不自动刷新。</footer></main></html>')
