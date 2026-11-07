"""私有网关单场页的房间标题与观看指标展示。"""
import html
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def display_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return '—'
    return f'{value:,.0f}'


def session_overview(content, name, snapshot):
    title = html.escape(f'{name} · 单场直播分析')
    description = html.escape(f'{name}的单场直播记录：平台累计看过、在线人数、入场趋势与本场分析。', quote=True)
    content = re.sub(r'<title>[^<]*</title>', lambda _: f'<title>{title}</title>', content, count=1)
    content = re.sub(r'<h1>.*?</h1>', lambda _: f'<h1>{title}</h1>', content, count=1, flags=re.S)
    meta = (f'<meta name="description" content="{description}">'
            f'<meta property="og:title" content="{title}">'
            f'<meta property="og:description" content="{description}">'
            '<meta name="robots" content="noindex,nofollow">')
    content = content.replace('<title>', meta + '<title>', 1)
    summary = snapshot.get('session_summary')
    summary = summary if isinstance(summary, dict) else {}
    latest = snapshot.get('latest')
    online = latest.get('online') if isinstance(latest, dict) else None
    online = online if isinstance(online, dict) else {}
    stamp = online.get('at')
    try:
        stamp = datetime.fromisoformat(stamp).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m/%d %H:%M:%S')
    except (ValueError, TypeError):
        stamp = None
    live = snapshot.get('phase') == 'live'
    watched = display_count(summary.get('watched'))
    current = display_count(online.get('value'))
    note = '平台上报的本场累计值；不是此网站的访问量' if watched != '—' else '尚未收到平台累计观看数据'
    online_note = ('上报于 ' + stamp + '（北京时间）') if stamp else '尚未收到平台在线数据'
    if not live and current != '—':
        online_note += ' · 下播前最后值'
    cards = ('<section class="session-overview" aria-label="本场观看数据">'
             f'<div><span>本场累计看过</span><strong>{watched}</strong><small>{note}</small></div>'
             f'<div><span>{"最近上报在线人数" if live else "下播前在线人数"}</span>'
             f'<strong>{current}</strong><small>{html.escape(online_note)}</small></div></section>')
    style = ('<style>.session-overview{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));'
             'gap:12px;margin:16px 0}.session-overview>div{padding:16px;border:1px solid #dbe4ec;'
             'border-radius:12px;background:#f8fafc}.session-overview span,.session-overview small{display:block}'
             '.session-overview strong{display:block;font-size:30px;margin:6px 0;color:#1d4ed8}'
             '.session-overview small{font-size:12px;color:#64748b;overflow-wrap:anywhere}</style>')
    content = content.replace('<title>', style + '<title>', 1)
    return re.sub(r'(</h1>)', lambda match: match.group(1) + cards, content, count=1)
