"""本地网关与静态单场页共用的展示层；不取数或改变快照。"""
import html
import json
import math
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

STYLE = '''
.session-header{display:flex;align-items:center;justify-content:space-between;gap:16px;
flex-wrap:wrap;margin:0 0 24px;padding:0 0 14px;border-bottom:1px solid #e0e6ef}
.session-brand{font-size:18px;font-weight:650;color:#16243b}
.session-header nav{display:flex;align-items:center;gap:8px 20px;flex-wrap:wrap}
.session-header a{font-size:14px;font-weight:600;line-height:1.6;padding:6px 0;
color:#2563eb;text-decoration:none;white-space:nowrap}
.session-header a:hover{text-decoration:underline}
.session-header a:focus-visible{outline:2px solid #2563eb;outline-offset:4px;border-radius:3px}
#session-summary{background:none;border:0;box-shadow:none;padding:0;margin:20px 0 24px;overflow:visible}
.session-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.session-card{min-width:0;padding:18px;background:#fff;border:1px solid #e0e6ef;
border-radius:14px;box-shadow:0 4px 16px #18243b05}
.session-card span{display:block;font-size:14px;color:#53637a}
.session-card strong{display:block;font-size:28px;line-height:1.3;margin-top:8px;
font-variant-numeric:tabular-nums;overflow-wrap:anywhere;color:#16243b}
#session-summary p{margin:10px 0 0;font-size:12px}
@media(max-width:600px){.session-header{gap:8px;margin-bottom:20px}
.session-header nav{width:100%;gap:8px 20px}.session-cards{grid-template-columns:repeat(2,minmax(0,1fr))}
.session-card{padding:14px}.session-card strong{font-size:25px}}
'''


def display_count(value, *, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return '—'
    if not signed and value < 0:
        return '—'
    return f'{value:+,.0f}' if signed and value else f'{value:,.0f}'


class Navigation(HTMLParser):
    """旧页的导航链接只作为目标回退，不复制任意 HTML。"""
    def __init__(self):
        super().__init__()
        self.inside = False
        self.links = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'nav' and ('archive-nav' in attrs.get('class', '').split()
                             or attrs.get('aria-label') == '页面导航'):
            self.inside = True
        if tag == 'a' and self.inside:
            key = attrs.get('data-nav-key') or ('audience_url' if attrs.get('id') == 'audience-entry'
                                               else 'home_url' if not self.links else 'songs_url')
            self.links[key] = attrs.get('href', '')

    def handle_endtag(self, tag):
        if tag == 'nav':
            self.inside = False


def safe_target(value):
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if value.startswith('/') and not value.startswith('//') and not parsed.netloc and not parsed.scheme:
        return value
    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
        return value
    return None


def summary_cards(snapshot):
    totals = snapshot.get('session_summary') or {}
    amount = totals.get('gift_amount_yuan')
    amount_text = (f'¥{amount:,.2f}' if isinstance(amount, (int, float)) and not isinstance(amount, bool)
                   and math.isfinite(amount) and amount >= 0 else '—')
    count = display_count(totals.get('gift_count'))
    if totals.get('gift_count_unknown'):
        count += '+'
    if totals.get('gift_amount_unknown'):
        amount_text += '+'
    metrics = [('累计看过', display_count(totals.get('watched'))),
               ('累计涨粉', display_count(totals.get('followers_change'), signed=True)),
               ('礼物总数', count), ('礼物平台金额', amount_text)]
    cards = ''.join(f'<div class="session-card"><span>{label}</span><strong>{value}</strong></div>'
                    for label, value in metrics)
    return ('<section id="session-summary" aria-label="本场汇总"><h2>本场汇总</h2>'
            f'<div class="session-cards">{cards}</div>'
            '<p>按本场已收到的数据汇总；看过人数采用平台累计值。金额为平台报告金额，非主播实收。</p></section>')


def session_metadata(snapshot):
    try:
        start = datetime.fromisoformat(snapshot['session_start']).astimezone(ZoneInfo('Asia/Shanghai'))
        end = datetime.fromisoformat(snapshot['updated_at']).astimezone(ZoneInfo('Asia/Shanghai'))
    except (KeyError, TypeError, ValueError):
        return None
    status = {'live': '<span class="live-pill">直播中</span>', 'completed': '本场已结束',
              'finalizing': '本场已结束，正在整理最终版'}.get(snapshot.get('phase'), '状态未知')
    if snapshot.get('publication_status') == 'attention':
        status = '页面更新异常，直播状态以平台为准' if snapshot.get('phase') == 'live' else '本场已结束，分析待恢复'
    end_text = end.strftime('%H:%M:%S') if start.date() == end.date() else end.strftime('%Y-%m-%d %H:%M:%S')
    return (f'<p class="session-meta">{start:%Y-%m-%d %H:%M:%S}–{end_text}（北京时间）<br>'
            f'{status} · 更新于 {end:%m/%d %H:%M:%S}</p>')


def session_overview(content, name, snapshot, *, navigation=None):
    title = html.escape(f'{name} · 单场直播分析')
    if not re.search(r'<title>', content):
        content = content.replace('<html>', '<html><title></title>', 1)
    if not re.search(r'<h1>', content):
        content = re.sub(r'(<main\b[^>]*>)', r'\1<h1></h1>', content, count=1)
    parser = Navigation()
    parser.feed(content)
    destinations = parser.links
    audience_script = re.search(r'<script id="audience-navigation">\s*\(\(\)=>\{const base=new URL\(("(?:\\.|[^"\\])*")\)', content)
    if audience_script and 'audience_url' not in destinations:
        try:
            destinations['audience_url'] = json.loads(audience_script[1])
        except ValueError:
            pass
    destinations.update(navigation or {})
    links = []
    for key, label in [('home_url', '返回主页'), ('songs_url', '歌曲搜索'), ('audience_url', '观众备忘')]:
        target = safe_target(destinations.get(key))
        if target:
            identity = ' id="audience-entry"' if key == 'audience_url' else ''
            links.append(f'<a{identity} data-nav-key="{key}" href="{html.escape(target, quote=True)}">{label}</a>')
    header = ('<header class="session-header"><span class="session-brand">' + html.escape(name) + '</span>'
              '<nav aria-label="页面导航">' + ''.join(links) + '</nav></header>')
    old_summary = re.search(r'<section id="session-summary"[^>]*>.*?</section>', content, flags=re.S)
    summary = (old_summary[0] if old_summary and not isinstance(snapshot.get('session_summary'), dict)
               else summary_cards(snapshot))
    # 允许网关对新页或旧页重复应用同一布局；保留图表、脚本与刷新标识。
    content = re.sub(r'<header class="session-header">.*?</header>', '', content, flags=re.S)
    content = re.sub(r'<nav class="archive-nav">.*?</nav>', '', content, flags=re.S)
    content = re.sub(r'<section (?:id="session-summary"|class="session-overview")[^>]*>.*?</section>', '', content, flags=re.S)
    content = re.sub(r'<style id="session-layout-style">.*?</style>', '', content, flags=re.S)
    content = re.sub(r'<meta (?:name="description"|property="og:(?:title|description)")[^>]*>', '', content)
    content = re.sub(r'<title>[^<]*</title>', lambda _: f'<title>{title}</title>', content, count=1)
    description = html.escape(f'{name}的单场直播记录：本场汇总、在线人数、入场趋势与分析。', quote=True)
    meta = (f'<meta name="description" content="{description}"><meta property="og:title" content="{title}">'
            f'<meta property="og:description" content="{description}"><style id="session-layout-style">{STYLE}</style>')
    content = content.replace('<title>', meta + '<title>', 1)
    content = re.sub(r'<h1>.*?</h1>', lambda _: header + f'<h1>{title}</h1>', content, count=1, flags=re.S)
    metadata = session_metadata(snapshot)
    if metadata:
        content = re.sub(r'(</h1>)\s*(?:<p[^>]*>.*?</p>)?', lambda m: m[1] + metadata,
                         content, count=1, flags=re.S)
    # 首屏汇总放在场次时间之后，早于图表及操作提示。
    content = re.sub(r'(</h1>(?:\s*<p[^>]*>.*?</p>)?)', lambda m: m[1] + summary,
                     content, count=1, flags=re.S)
    if safe_target(destinations.get('audience_url')):
        from .audience_navigation import with_audience_links
        content = with_audience_links(content, destinations['audience_url'])
    return content
