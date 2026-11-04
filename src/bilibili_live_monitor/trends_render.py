"""无外部资源的HTML/SVG报告；仅消费冻结数据。"""
import html
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from .chart_titles import right_title, with_chart_titles

STYLE = '''body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
background:#f3f6fb;color:#16243b;margin:0;padding:26px}main{max-width:1120px;margin:auto}
h1{font-size:27px;margin:0 0 8px}p{color:#53637a;font-size:14px;line-height:1.7}
section{background:white;border:1px solid #e0e6ef;border-radius:14px;padding:18px 18px 8px;
margin:16px 0;box-shadow:0 4px 16px #18243b05}h2{font-size:18px;margin:0 0 12px}
small{font-size:12px;color:#69788d;font-weight:400;margin-left:16px}svg{width:100%;display:block}
svg text{fill:#748094;font-size:12px}.grid{stroke:#e7edf4;stroke-width:1}a{color:#2563eb}
footer{margin:20px 0;font-size:13px;color:#617089;line-height:1.8}
.archive-nav{margin:0 0 12px}.archive-nav a{text-decoration:none;font-weight:600}
.live-pill{display:inline-flex;align-items:center;gap:7px;padding:4px 12px;border-radius:999px;background:#dbeafe;border:1px solid #93c5fd;color:#1d4ed8;font-weight:700;line-height:1.4}
.live-pill::before{content:"";width:7px;height:7px;border-radius:50%;background:#2563eb;box-shadow:0 0 0 3px #2563eb18}
.song-hit{fill:transparent;stroke:transparent;pointer-events:all}
.song-hit.song-active{fill:#16a34a;fill-opacity:.22;stroke:#15803d;stroke-width:1.5}
.song-list{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.song-chip{font:inherit;font-size:14px;text-align:left;padding:9px 12px;border-radius:9px;
border:1px solid #d4e7da;background:#f5faf6;color:#183d29;cursor:pointer}
.song-chip.song-active{background:#dcfce7;border-color:#15803d;outline:2px solid #15803d}
.song-chip span{display:block;font-size:12px;color:#53637a;margin-top:4px}
.song-tooltip{position:fixed;z-index:1000;pointer-events:none;width:max-content;max-width:calc(100vw - 24px);
box-sizing:border-box;padding:14px 18px;border:1px solid #8ccda3;border-radius:12px;
background:#f5fff8;color:#123e25;box-shadow:0 8px 28px #17372530;line-height:1.4}
.song-tooltip[hidden]{display:none}.song-tooltip strong{display:block;font-size:21px;font-weight:650}
.activity-table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e7edf4;white-space:nowrap}
.song-tooltip span{display:block;font-size:14px;margin-top:5px;color:#496453}
@media(max-width:600px){body{padding:12px}section{padding:12px 6px;overflow-x:auto}
small{display:block;margin:7px 0 0}svg{min-width:680px}}'''


SONG_SCRIPT = '''<div id="song-tooltip" class="song-tooltip" role="status" hidden><strong></strong><span></span></div><script>
(()=>{
 let pinned=null,lastPointer=null;
 const tooltip=document.getElementById('song-tooltip');
 const nodes=()=>[...document.querySelectorAll('[data-song-id]')];
 const target=e=>e.target?.closest?.('[data-song-id]');
 function activate(id){nodes().forEach(n=>n.classList.toggle('song-active',!n.disabled&&n.dataset.songId===id));}
 function show(n,x,y){
  tooltip.querySelector('strong').textContent=n.dataset.songTitle;
  tooltip.querySelector('span').textContent=n.dataset.songTime;
  tooltip.hidden=false;
  const box=tooltip.getBoundingClientRect();
  tooltip.style.left=Math.max(12,Math.min(x+16,innerWidth-box.width-12))+'px';
  const top=y-box.height-16;
  tooltip.style.top=Math.max(12,Math.min(top>=12?top:y+20,innerHeight-box.height-12))+'px';
 }
 function reset(){tooltip.hidden=true;activate(pinned);}
 function hoverTarget(e){
  const direct=target(e);if(direct)return direct;
  const svg=e.target?.closest?.('svg');
  return svg&&[...svg.querySelectorAll('.song-hit')].find(n=>{
   const r=n.getBoundingClientRect();return e.clientX>=r.left&&e.clientX<=r.right&&e.clientY>=r.top&&e.clientY<=r.bottom;
  });
 }
 document.addEventListener('pointermove',e=>{if(e.pointerType==='touch')return;lastPointer={x:e.clientX,y:e.clientY};const n=hoverTarget(e);if(n&&!n.disabled){activate(n.dataset.songId);show(n,e.clientX,e.clientY);}else reset();});
 document.addEventListener('mouseover',e=>{lastPointer={x:e.clientX,y:e.clientY};const n=target(e);if(n&&!n.disabled){activate(n.dataset.songId);show(n,e.clientX,e.clientY);}});
 document.addEventListener('mouseout',e=>{if(!e.relatedTarget||target(e))reset();});
 document.addEventListener('focusin',e=>{const n=target(e);if(n&&!n.disabled){activate(n.dataset.songId);const r=n.getBoundingClientRect();show(n,r.left,r.top);}});
 document.addEventListener('focusout',e=>{if(target(e))reset();});
 function pin(n,x,y){pinned=pinned===n.dataset.songId?null:n.dataset.songId;activate(pinned);if(pinned)show(n,x,y);else reset();}
 document.addEventListener('click',e=>{const n=hoverTarget(e);lastPointer=null;if(n&&!n.disabled){pin(n,e.clientX,e.clientY);}else{pinned=null;reset();}});
 document.addEventListener('keydown',e=>{if(e.key==='Escape'){pinned=null;reset();return;}const n=target(e);if(n instanceof SVGElement&&(e.key==='Enter'||e.key===' ')){e.preventDefault();const r=n.getBoundingClientRect();pin(n,r.left,r.top);}});
 document.addEventListener('song-bands-updated',()=>{
  if(!nodes().some(n=>n.classList.contains('song-hit')&&n.dataset.songId===pinned))pinned=null;
  reset();
 });
 document.addEventListener('scroll',()=>{
  if(!lastPointer){tooltip.hidden=true;return;}
  const {x,y}=lastPointer,n=hoverTarget({target:document.elementFromPoint(x,y),clientX:x,clientY:y});
  if(n&&!n.disabled){activate(n.dataset.songId);show(n,x,y);}else reset();
 },true);
 window.addEventListener('blur',()=>{tooltip.hidden=true;});
})();
</script>'''


def render(data, activities=None, songs=None, *, charts=None, header=None, footer=None,
           show_song_list=True, refresh_revision=None):
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
        parts = [f'<svg viewBox="0 -26 1110 278" role="img" aria-label="{esc(name)}">', right_title(name)]
        if activities is not None:
            parts.append('<g data-activity-layer="true">')
            for band in activities:
                missing = band.get('reason') == '无ASR覆盖'
                if band['label'] != 'singing' and not missing:
                    continue
                start, end = max(lo, band['start']), min(hi, band['end'])
                if start < end:
                    if missing:
                        parts.append(f'<rect x="{x(start)}" y="{top}" width="{x(end)-x(start)}" '
                                     f'height="{bottom-top}" fill="#94a3b8" opacity=".25" '
                                     'data-missing-audio="true"><title>无音频数据覆盖</title></rect>')
                        continue
                    parts.append(f'<rect x="{x(start)}" y="{top}" width="{x(end)-x(start)}" '
                                 f'height="{bottom-top}" fill="#22c55e" opacity=".30"/>')
                    parts.append(f'<rect x="{x(start)}" y="210" width="{x(end)-x(start)}" '
                                 'height="6" fill="#22c55e" opacity=".95"/>')
            parts.append('</g>')
        for value in range(low, high+1, spacing):
            yy = y(value)
            parts.append(f'<path class="grid" d="M {left} {yy} H {right}"/>'
                         f'<text data-axis="left" x="52" y="{yy+4}" text-anchor="end">{value}</text>'
                         f'<text data-axis="right" x="{right+13}" y="{yy+4}" text-anchor="start">{value}</text>')
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
        # 背景命中层置于曲线下方，保留数据点原有悬停提示。
        overlays = []
        for song in songs or []:
            for segment in song['segments']:
                start, end = max(lo, segment['start']), min(hi, segment['end'])
                if start < end:
                    overlays.append(f'<rect class="song-hit" data-song-id="{esc(song["id"])}" '
                                    f'data-song-title="{esc(song["title"])}" data-song-time="{clock(start)}–{clock(end)}" '
                                    f'data-start="{start}" data-end="{end}" x="{x(start)}" y="{top}" '
                                    f'width="{x(end)-x(start)}" height="{bottom-top}" '
                                    f'tabindex="0" role="button" aria-label="{esc(song["title"])} · {clock(start)}–{clock(end)}"></rect>')
        if overlays:
            insertion = next((i for i, part in enumerate(parts) if 'class="grid"' in part), len(parts))
            parts[insertion:insertion] = ['<g data-song-layer="true">'+''.join(overlays)+'</g>']
        parts.append('</svg>')
        return ''.join(parts)

    cards = []
    for field, name, color, mode in ([] if charts is not None else [('unique_entries', '入场流量', '#2563eb', 'line'),
                                    ('gifts', '送礼频次', '#e79024', 'bar')]):
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
                                            else '送礼事件数（含SC，有订单ID时合并）')
        cards.append(f'<section><h2>{name}<small>{unit}</small></h2>{chart(name,points,color,mode)}</section>')
    for field, name, color in ([] if charts is not None else [('followers', '关注总数', '#8b5cf6'), ('fans_club', '粉丝团人数', '#e2578b')]):
        points = [{'t': row['t'], 'v': row[field], 'label': f'{clock(row["t"],True)}：{row[field]}'}
                  for row in data['source']['stocks'] if row[field] is not None]
        note = (f'最新 {points[-1]["v"]} · 推送于 {clock(points[-1]["t"])} · 总量（纵轴不从零起）'
                if points else '无观测数据')
        cards.append(f'<section><h2>{name}<small>{note}</small></h2>{chart(name,points,color,"stock")}</section>')
    for spec in charts or []:
        cards.append(f'<section data-chart="{esc(spec["field"])}"><h2>{esc(spec["name"])}'
                     f'<small>{esc(spec["note"])}</small></h2>'
                     f'{chart(spec["name"],spec["points"],spec["color"],spec["mode"])}</section>')
    song_list = ''
    if songs and show_song_list:
        chips = ''.join(f'<button class="song-chip" data-song-id="{esc(song["id"])}" '
                        f'data-song-title="{esc(song["title"])}" data-song-time="{clock(song["segments"][0]["start"])}–{clock(song["segments"][-1]["end"])}">'
                        f'{esc(song["title"])}<span>{clock(song["segments"][0]["start"])}–'
                        f'{clock(song["segments"][-1]["end"])}</span></button>' for song in songs)
        song_list = '<section id="songs"><h2>本场歌单</h2><p>悬停歌名高亮对应唱段；点按可固定，再点取消。只列已交叉核对的歌名。</p><div class="song-list">'+chips+'</div></section>'
    default_footer = ('点或柱悬停可查看数值。关注与粉丝团显示平台总量原值；'
             '点间沿用上一观测值，首点前不补值，末点后虚线延续。<br>'
             '入场在桶内按UID去重，跨桶可重复；匿名入场不计入人数，明细计数在JSON。'
             '送礼按订单/事件计次，含免费礼物、SC与上舰，不按件数或金额计数；'
             '缺订单ID时按源事件键计数，不能保证合并同单盲盒结果。<br>'
             '空心点、浅色柱为不足完整桶宽的首尾桶；无业务报文的空桶显示缺测，不填零。'
             '灰带为长时间无业务报文的可疑缺口，不能据此断言停播或真实低流量。'
             '所有横轴按接收时刻对齐。这是冻结快照，不自动刷新。')
    default_header = (f'<p>{clock(lo,True)}—{clock(hi,True)} '
             f'（{esc(data["timezone"])}）<br>范围内已观测入场账号 {summary["unique_entry_uids"]:,} · '
             f'送礼事件 {summary["gift_events"]} · <a href="data.json" download>下载冻结数据</a></p>')
    return with_chart_titles('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">'
            f'<title>{esc(data["title"])} · 直播趋势</title><style>{STYLE}</style>'
            f'<main data-revision="{esc(refresh_revision or "")}" data-refresh="{"live" if refresh_revision else "static"}">'
            f'<h1>{esc(data["title"])} · 直播趋势</h1>' + (default_header if header is None else header) +
            ('<p class="song-help">鼠标指向绿色区域可以看见唱的是什么歌（仅显示已识别歌名）。</p>' if songs else '') +
            ''.join(cards) + song_list + (SONG_SCRIPT if songs or refresh_revision else '') +
            '<footer>' + (default_footer if footer is None else footer) + '</footer></main>' +
            (REFRESH_SCRIPT if refresh_revision else '') + '</html>')


REFRESH_SCRIPT = '''<script>
(()=>{let busy=false;
 function latest(){window.__linkedChartScroll?.latest();}
 async function refresh(){
  if(document.hidden||busy)return;busy=true;
  try{
   const response=await fetch('index.html?t='+Date.now(),{cache:'no-store'});
   if(!response.ok)throw new Error('refresh');
   const next=new DOMParser().parseFromString(await response.text(),'text/html').querySelector('main');
   const old=document.querySelector('main');
   if(next&&next.dataset.revision!==old.dataset.revision){
    next.querySelectorAll('script').forEach(n=>n.remove());
    const tooltip=document.getElementById('song-tooltip');
    if(tooltip){tooltip.hidden=true;next.querySelector('#song-tooltip')?.replaceWith(tooltip);}
    old.replaceWith(next);document.dispatchEvent(new Event('song-bands-updated'));latest();
    if(next.dataset.refresh==='static')clearInterval(timer);
   }
  }catch(e){}finally{busy=false;}
 }
 latest();window.addEventListener('resize',latest);
 const timer=setInterval(refresh,10000);document.addEventListener('visibilitychange',()=>{
  if(document.querySelector('main')?.dataset.refresh!=='static')refresh();
 });
})();
</script>'''
