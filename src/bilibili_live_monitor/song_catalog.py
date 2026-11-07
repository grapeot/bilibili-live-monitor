"""按单个主播生成静态歌曲搜索、歌曲页与跨场演唱链接。"""
import argparse
import hashlib
import html
import json
import math
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .activity_summary import content_blocks
from .time_utils import epoch


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def identifier(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def safe_url(value, home):
    resolved = urljoin(home, value)
    parsed, origin = urlsplit(resolved), urlsplit(home)
    if parsed.scheme != 'https' or parsed.netloc != origin.netloc or parsed.username or parsed.password:
        raise ValueError('场次URL必须位于本主播主页的HTTPS站点')
    return resolved.split('#')[0]


class SongHits(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hits = set()
        self.detail_parts = []
        self.in_detail = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'detail-data':
            self.in_detail = True
        if 'song-hit' not in (attrs.get('class') or '').split():
            return
        title = attrs.get('data-song-title', '').strip()
        start, end = float(attrs['data-start']), float(attrs['data-end'])
        if title and math.isfinite(start) and math.isfinite(end) and end > start:
            self.hits.add((start, end, title))

    def handle_data(self, data):
        if self.in_detail:
            self.detail_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_detail = False


def performances(session):
    """现代页读已发布快照；历史页只读已显示的歌曲命中区，不读私有歌词。"""
    if session.get('snapshot'):
        snapshot = json.loads(Path(session['snapshot']).read_text())
        if snapshot.get('phase') != 'completed':
            return []
        if session.get('start') and epoch(snapshot['session_start']) != epoch(session['start']):
            raise ValueError('场次起始时间与快照不一致')
        rows = snapshot.get('activity', [])
    else:
        parser = SongHits()
        parser.feed(Path(session['html']).read_text())
        if parser.detail_parts:
            detail = json.loads(''.join(parser.detail_parts))
            for row in detail.get('songs', []):
                start, end = float(row['start']), float(row['end'])
                if row.get('title') and math.isfinite(start) and math.isfinite(end) and end > start:
                    parser.hits.add((start, end, row['title']))
        rows = [{'start': datetime.fromtimestamp(a, ZoneInfo('UTC')).isoformat(),
                 'end': datetime.fromtimestamp(b, ZoneInfo('UTC')).isoformat(),
                 'song': title, 'label': 'singing', 'music_source': 'probable_live'}
                for a, b, title in sorted(parser.hits)]
    return [r for r in content_blocks(rows, include_segments=True) if r['kind'] == 'song' and r['title']]


def catalog(config, previous=None):
    home = config['home_url']
    safe_url(home, home)
    scope = config['scope']
    if not isinstance(scope, str) or not scope:
        raise ValueError('必须指定单主播scope')
    previous = previous or {'scope': scope, 'performances': []}
    if previous.get('scope') != scope:
        raise ValueError('不能复用其他主播的歌曲登记状态')
    used, result, session_ids = set(), {}, set()
    old = previous.get('performances', [])
    for session in config['sessions']:
        sid = str(session['id'])
        if sid in session_ids:
            raise ValueError('重复场次ID')
        session_ids.add(sid)
        url = safe_url(session['url'], home)
        blocks = performances(session)
        for block_index, block in enumerate(blocks):
            title = block['title'].strip()
            normalized = key(title)
            canonical = config.get('aliases', {}).get(normalized, normalized)
            song_id = identifier(scope, canonical)
            start, end = block['start'], block['end']
            candidates = []
            for p in old:
                if p['session_id'] != sid or p['song_id'] != song_id:
                    continue
                identities = p.get('identities', [{'id': p['id'], 'start': p['start'], 'end': p['end']}])
                for identity in identities:
                    a, b = identity.get('last_start', identity['start']), identity.get('last_end', identity['end'])
                    matches = [(max(0, min(other['end'], b)-max(other['start'], a)),
                                -abs(other['start']-a), index)
                               for index, other in enumerate(blocks)
                               if identifier(scope, config.get('aliases', {}).get(key(other['title'].strip()), key(other['title'].strip()))) == song_id]
                    owner = max(matches, default=(0, 0, -1))
                    if identity['id'] not in used and owner[0] > 0 and owner[2] == block_index:
                        candidates.append(identity)
            candidates = list({p['id']: p for p in candidates}.values())
            match = max(candidates, key=lambda p: (
                min(end, p.get('last_end', p['end']))-max(start, p.get('last_start', p['start'])),
                -abs(start-p.get('last_start', p['start']))), default=None)
            pid = match['id'] if match else identifier(scope, sid, song_id, start)
            nonce = 0
            while pid in used:
                nonce += 1
                pid = identifier(scope, sid, song_id, start, nonce)
            aliases = sorted({p['id'] for p in candidates if p['id'] != pid})
            identities = candidates or [{'id': pid, 'start': start, 'end': end}]
            identities = [{**identity, 'last_start': start, 'last_end': end} if identity['id'] == pid
                          else dict(identity) for identity in identities]
            used.update([pid, *aliases])
            row = {'id': pid, 'song_id': song_id, 'session_id': sid, 'session': session['label'],
                   'title': title, 'start': start, 'end': end, 'segments': block['segments'],
                   'aliases': aliases, 'identities': identities,
                   'url': url + '#' + urlencode({'song': title, 'at': start})}
            group = result.setdefault(song_id, {'id': song_id, 'title': title, 'performances': []})
            group['performances'].append(row)
    songs = list(result.values())
    for song in songs:
        song['performances'].sort(key=lambda p: p['start'], reverse=True)
        song['count'] = len(song['performances'])
        song['latest'] = song['performances'][0]['start']
    songs.sort(key=lambda s: (-s['count'], -s['latest'], s['title']))
    data = {'schema_version': 1, 'scope': scope, 'label': config['label'], 'home_url': home, 'songs': songs}
    from .song_gifts import add_gift_stats
    add_gift_stats(data, config['sessions'])
    from .song_followers import add_follower_stats
    add_follower_stats(data, config['sessions'])
    return data


STYLE = '''body{margin:0;background:#f1f5f9;color:#16324f;font:16px/1.65 system-ui,sans-serif}
main{max-width:1080px;margin:auto;padding:28px 20px}a{color:#1d4ed8}h1{margin:12px 0}h2{font-size:21px}
.muted{color:#61758a}input{box-sizing:border-box;width:100%;font:inherit;padding:13px;border:1px solid #b9c9dc;border-radius:10px;background:white}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-top:24px}
.card{display:block;padding:22px;background:white;border:1px solid #dbe4ef;border-radius:14px;text-decoration:none;color:inherit}
.card:hover,.card:focus-visible{border-color:#2563eb;outline:2px solid #bfdbfe}li{padding:20px;background:white;border-radius:12px;margin:14px 0;list-style:none}ul{padding:0}
.button{display:inline-block;background:#1d4ed8;color:white;padding:8px 16px;border-radius:8px;text-decoration:none}
.sort-options{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 8px}.sort-options button{font:inherit;font-size:14px;border:1px solid #b9c9dc;border-radius:8px;padding:9px 12px;background:white;color:#16324f;cursor:pointer}
.sort-options button[aria-pressed="true"]{background:#1d4ed8;color:white;border-color:#1d4ed8}.sort-options button:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
[hidden]{display:none!important}small{display:block}.empty{padding:28px;text-align:center}'''


def page(title, body, script=''):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main>{script}</body></html>')


def clock(at):
    return datetime.fromtimestamp(at, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def render(data):
    from .song_followers import render_followers
    from .song_gifts import render_gifts
    esc = html.escape
    cards, pages = [], {}
    for song in data['songs']:
        stats = song.get('gifts')
        metrics = {'latest': song['latest'], 'count': song['count'],
                   'followers': song.get('followers_change') if song.get('followers_change') is not None else '',
                   **{key: stats[field] if stats else '' for key, field in
                      [('gifts', 'gift_count'), ('users', 'gift_users'), ('amount', 'gift_amount_yuan')]}}
        attributes = ' '.join(f'data-sort-{name}="{value}"' for name, value in metrics.items())
        cards.append(f'<a class="card" href="{song["id"]}/" data-title="{esc(key(song["title"]), quote=True)}" {attributes}>'
                     f'<h2>{esc(song["title"])}</h2><span>{song["count"]} 次出现</span>'
                     f'<small class="muted">最近 {clock(song["latest"])[:10]}</small>{render_gifts(song.get("gifts"))}'
                     + render_followers(song.get('followers_change')) + '</a>')
        rows = []
        for item in song['performances']:
            anchors = ''.join(f'<span id="performance-{alias}"></span>' for alias in item['aliases'])
            duration = sum(s['end'] - s['start'] for s in item['segments'])
            rows.append(f'<li id="performance-{item["id"]}">{anchors}<strong>{esc(item["session"])}</strong>'
                        f'<div>{clock(item["start"])}–{clock(item["end"])[11:]}（北京时间）</div>'
                         f'<p class="muted">已识别歌曲片段 {round(duration)} 秒</p>'
                          + render_gifts(item.get('gifts')) +
                          render_followers(item.get('followers_change')) +
                         f'<a class="button" href="{esc(item["url"], quote=True)}">打开该次片段</a></li>')
        pages[song['id'] + '/index.html'] = page(song['title'], '<a href="../">← 返回歌曲搜索</a>'
            f'<h1>{esc(song["title"])}</h1><p>{esc(data["label"])} · {song["count"]} 次出现</p>'
            + '<p class="muted">歌曲片段可能是主播演唱，也可能是播放的音乐，目前尚未具备可靠区分两者的功能。</p>'
            + render_gifts(song.get('gifts')) + render_followers(song.get('followers_change'))
            + '<p class="muted">每次歌曲片段开始前30秒至结束后2分钟；同歌重叠窗口按礼物事件去重，人数跨次去重。金额为平台标价，非主播实收；“+”表示另有未知记录。净增关注为窗口内关注总数之差，可能为负，同歌重叠窗口只计一次；观测不足时不记作0，不代表歌曲带来的新增人数。</p>'
            + f'<ul>{"".join(rows)}</ul>')
    script = '''<script>const q=document.querySelector('#search'),cards=[...document.querySelectorAll('.card')];
const grid=document.querySelector('.cards'),buttons=[...document.querySelectorAll('[data-sort-key]')];
let sortKey='count',descending=true;
function value(card){const raw=card.getAttribute('data-sort-'+sortKey);return raw===null||raw===''?null:Number(raw);}
function sort(){const ordered=cards.map((card,index)=>({card,index,value:value(card)}));
ordered.sort((a,b)=>{const av=a.value,bv=b.value;if(av===null||bv===null)return av===bv?a.index-b.index:av===null?1:-1;
return (descending?bv-av:av-bv)||a.index-b.index;});
grid.append(...ordered.map(row=>row.card));
for(const button of buttons){const active=button.dataset.sortKey===sortKey;button.setAttribute('aria-pressed',String(active));
button.textContent=button.dataset.label+(active?(descending?' ↓':' ↑'):'');}
const label=buttons.find(b=>b.dataset.sortKey===sortKey).dataset.label;
document.querySelector('#sort-status').textContent=label+'：'+(descending?'从高到低':'从低到高');}
for(const button of buttons)button.addEventListener('click',()=>{if(sortKey===button.dataset.sortKey)descending=!descending;
else{sortKey=button.dataset.sortKey;descending=true;}sort();});
function filter(){const s=q.value.normalize('NFKC').trim().toLowerCase();let n=0;for(const c of cards){c.hidden=!c.dataset.title.includes(s);if(!c.hidden)n++;}
document.querySelector('#count').textContent=`${n} 首歌曲`;document.querySelector('#empty').hidden=n!==0;}
 q.value=new URLSearchParams(location.search).get('q')||'';
 q.addEventListener('input',filter);sort();filter();</script>'''
    body = f'<a href="{esc(data["home_url"], quote=True)}">← 返回场次主页</a><h1>{esc(data["label"])} · 歌曲</h1>'
    body += '<p class="muted">从已完成场次查找歌曲片段。片段可能是主播演唱，也可能是播放的音乐，目前尚未具备可靠区分两者的功能。净增关注按片段前30秒至结束后2分钟的关注总数变化统计，包含取关，不代表歌曲带来的新增人数。</p><label for="search">搜索歌名</label><input id="search" type="search" placeholder="输入歌名" autocomplete="off">'
    body += '<div class="sort-options" role="group" aria-label="歌曲排序">' + ''.join(
        f'<button type="button" data-sort-key="{key}" data-label="{label}" aria-pressed="false">{label}</button>'
        for key, label in [('latest', '最近时间'), ('count', '出现次数'), ('gifts', '礼物件数'),
                            ('users', '送礼人数'), ('amount', '礼物金额'), ('followers', '净增关注')]) + '</div>'
    body += '<small class="muted">再点同一项切换升序／降序；暂无统计的歌曲排在最后。</small><small id="sort-status" class="muted" aria-live="polite"></small>'
    body += f'<p id="count" aria-live="polite"></p><div class="cards">{"".join(cards)}</div><p id="empty" class="empty" hidden>没有找到匹配歌曲</p>'
    pages['index.html'] = page(data['label'] + ' · 歌曲搜索', body, script)
    return pages


def build(config):
    output, state_path = Path(config['output_dir']), Path(config['state_file'])
    previous = json.loads(state_path.read_text()) if state_path.exists() else None
    data = catalog(config, previous)
    pages = render(data)
    active = {s['id'] for s in data['songs']}
    for old in (previous or {}).get('performances', []):
        if old['song_id'] not in active:
            pages[old['song_id'] + '/index.html'] = page('歌曲记录已更新',
                '<a href="../">← 返回歌曲搜索</a><h1>歌曲记录已更新</h1><p>当前没有可用的歌曲片段。</p>')
    output.mkdir(parents=True, exist_ok=True)
    for name, text in pages.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(text)
        temp.replace(path)
    public_songs = [{**s, 'performances': [{k: v for k, v in p.items() if k != 'identities'}
                                          for p in s['performances']]} for s in data['songs']]
    index = {'schema_version': 1, 'label': data['label'], 'songs': public_songs}
    (output / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix('.tmp')
    temp.write_text(json.dumps({'scope': config['scope'], 'performances': [p for s in data['songs'] for p in s['performances']]}, ensure_ascii=False))
    temp.replace(state_path)
    return {'songs': len(data['songs']), 'performances': sum(s['count'] for s in data['songs']),
            'files': sorted([*pages, 'index.json'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(json.loads(args.config.read_text())), ensure_ascii=False))
