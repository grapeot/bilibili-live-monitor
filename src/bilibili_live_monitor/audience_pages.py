"""观众备忘静态站：公开字段白名单，不输出账号标识及原始全文。"""
import html
import json
import re
from collections import defaultdict
from pathlib import Path

from .audience_crm import SECTIONS, validate_state
from .audience_navigation import with_audience_links as with_audience_links
from .live_share import atomic_json, epoch
from .song_catalog import page, safe_url


def esc(value):
    return html.escape(str(value), quote=True)


def public_text(value, uids):
    """公开最小投影与私有逐字证据分离。原始引用不改，只隐藏展示中的敏感片段。"""
    value = str(value)
    value = re.sub(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', '[联系方式已隐藏]', value)
    value = re.sub(r'(?i)(?:(?<![A-Za-z0-9])(?:uid|qq)|微信|手机号|账号(?:是|为)?)\s*[:：=]?\s*[A-Za-z0-9_+\-]+',
                   '[账号已隐藏]', value)
    value = re.sub(r'(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)', '[联系方式已隐藏]', value)
    value = re.sub(r'(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])',
                   lambda m: '[账号已隐藏]' if m[0] in uids else m[0], value)
    value = re.sub(r'/(?:Users|home|var|private|tmp)/[^\s，。；<>]*', '[本地路径已隐藏]', value)
    return value


def stamp(value):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(epoch(value), ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')


LABELS = {'overview': '记住这个人', 'preferences': '听歌与话题偏好', 'attention': '互动注意事项',
          'promises': '未完成的事', 'actions': '下次最建议做什么', 'interactions': '互动经过'}
STATUSES = {'open': '待兑现', 'conditional': '条件未确认', 'fulfilled': '已完成', 'uncertain': '待确认'}


def crm_page(title, body, script=''):
    style = ('.profile-summary,.next-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}'
             '.profile-summary li{padding:14px}.profile-summary section,.next-steps section{min-width:0}'
             '.card,h1,li,details{overflow-wrap:anywhere}details{margin-top:10px;font-size:14px;color:#61758a}'
             'select,button{font:inherit;padding:8px;border:1px solid #b9c9dc;border-radius:8px;background:white}')
    return page(title, body, script).replace('</style>', style + '</style>', 1)


def rows_html(section, rows, evidence, sessions, home, uids):
    pieces = []
    for row in rows:
        refs = []
        for ref in row['refs']:
            item = evidence[ref['id']]
            session = sessions[item['session_id']]
            url = safe_url(session['url'], home)
            speaker = '主播' if item['speaker'] == 'streamer' else '本人' if item['speaker'] == 'viewer' else '同期观众'
            refs.append(f'<p><a href="{esc(url)}">{esc(stamp(item["at"]))}</a> '
                        f'{speaker}：{esc(public_text(ref["quote"], uids))}</p>')
        status = f'<small>{STATUSES[row["status"]]}</small>' if section == 'promises' else ''
        pieces.append(f'<li>{status}{esc(public_text(row["text"], uids))}<details><summary>查看依据</summary>{"".join(refs)}</details></li>')
    return ''.join(pieces)


def public_person(person, state, uids):
    counts = person['counts']
    summary = next(iter(state.get('overview', [])), {}).get('text', '')
    pending = sum(p['status'] != 'fulfilled' for p in state.get('promises', []))
    return {'id': person['id'], 'name': public_text(person['name'], uids),
            'names': [public_text(n, uids) for n in person['names']], 'summary': public_text(summary, uids),
            'session_count': len(person['sessions']), 'danmaku_count': counts['danmaku'],
            'last_observed': person['last_observed'], 'pending': pending}


def detail(person, state, material, config, uids=None):
    home = config['home_url']
    sessions = {s['id']: s for s in material['sessions']}
    from .audience_sources import evidence_map
    evidence = evidence_map(person, material)
    counts = person['counts']
    if uids is None:
        uids = {str(uid) for p in material['people'].values()
                for uid in [p.get('uid'), *p.get('observed_uids', [])] if uid}

    def show(value):
        return esc(public_text(value, uids))
    body = (f'<nav><a href="../">观众备忘</a> · <a href="{esc(home)}">场次主页</a></nav>'
            f'<h1>{show(person["name"])}</h1>'
            f'<p class="muted">有记录的 {len(person["sessions"])} 场 · 弹幕 {counts["danmaku"]} 条 · '
            f'最近记录 {esc(stamp(person["last_observed"]))}（北京时间）</p>')
    if len(person['names']) > 1:
        body += '<p>曾用昵称：' + '、'.join(show(n) for n in person['names'] if n != person['name']) + '</p>'
    if not evidence:
        body += '<p class="muted">目前只有进场或点赞记录，暂无足够信息整理偏好和待办。</p>'
    for section in SECTIONS if evidence else ():
        if section == 'overview':
            body += '<div class="profile-summary">'
        if section == 'promises':
            body += '<div class="next-steps">'
        rows = state.get(section, [])
        if section == 'promises':
            rows = [r for r in rows if r['status'] != 'fulfilled']
        body += f'<section><h2>{LABELS[section]}</h2><ul>'
        body += rows_html(section, rows, evidence, sessions, home, uids) if rows else (
            '<li class="muted">目前没有足够依据</li>' if section in ('overview', 'preferences', 'attention')
            else '<li class="muted">暂无已确认记录</li>')
        body += '</ul></section>'
        if section in ('attention', 'actions'):
            body += '</div>'
    fulfilled = [r for r in state.get('promises', []) if r['status'] == 'fulfilled']
    if fulfilled:
        body += '<h2>已经兑现</h2><ul>' + rows_html('promises', fulfilled, evidence, sessions, home, uids) + '</ul>'
    history = defaultdict(list)
    for event in person['history']:
        history[event['session_id']].append(event)
    body += '<h2>跨场记录</h2><ul>'
    for sid in reversed(person['sessions']):
        events = history[sid]
        session = sessions[sid]
        chats = sum(e['kind'] == 'danmaku' for e in events)
        gifts = [e for e in events if e['kind'] == 'gift']
        amount = sum(e.get('amount_milli') or 0 for e in gifts) / 1000
        unknown = '+' if any(e.get('amount_milli') is None for e in gifts) else ''
        gift_text = f' · 礼物 {len(gifts)} 笔 · 平台标价 ¥{amount:.2f}{unknown}' if gifts else ''
        body += (f'<li><a href="{esc(safe_url(session["url"], home))}">{esc(session["label"])}</a>'
                 f'<small>弹幕 {chats} 条{gift_text}</small>'
                 f'<small>记录时间 {esc(stamp(events[0]["at"]))} 至 {esc(stamp(events[-1]["at"]))}</small></li>')
    body += '</ul><p class="muted">记录时间仅表示捕获到互动的时间，不代表进出房或观看时长。</p>'
    return crm_page(public_text(person['name'], uids) + ' · ' + config['label'] + '观众备忘', body)


SCRIPT = r'''<script>
(()=>{let rows=[],limit=60;const root=document.querySelector('#people'),q=document.querySelector('#query'),
more=document.querySelector('#more'),sort=document.querySelector('#sort');
q.value=new URLSearchParams(location.search).get('q')||'';
const normalize=s=>s.normalize('NFKC').toLocaleLowerCase();
function draw(){const term=normalize(q.value.trim());let found=rows.filter(r=>normalize(r.names.join(' ')+' '+r.summary).includes(term));
found.sort(sort.value==='useful'?(a,b)=>b.pending-a.pending||Number(Boolean(b.summary))-Number(Boolean(a.summary))||b.last_observed.localeCompare(a.last_observed):
(a,b)=>b.last_observed.localeCompare(a.last_observed));root.replaceChildren();
for(const r of found.slice(0,limit)){const a=document.createElement('a');a.className='card';a.href=r.id+'/';
const h=document.createElement('h2');h.textContent=r.name;const p=document.createElement('p');p.textContent=r.summary||'已有到访记录，尚无足够互动线索';
const small=document.createElement('small');small.textContent=`${r.session_count} 场有记录 · ${r.danmaku_count} 条弹幕`+(r.pending?` · ${r.pending} 件待接上`:'');
a.append(h,p,small);root.append(a);}document.querySelector('#count').textContent=`找到 ${found.length} 位观众，显示 ${Math.min(limit,found.length)} 位`;
more.hidden=limit>=found.length;}
q.addEventListener('input',()=>{limit=60;draw();});sort.addEventListener('change',()=>{limit=60;draw();});
more.addEventListener('click',()=>{limit+=60;draw();});
fetch('index.json',{cache:'no-cache'}).then(r=>{if(!r.ok)throw Error('load');return r.json();}).then(data=>{rows=data.people;draw();})
.catch(()=>{document.querySelector('#count').textContent='暂时无法读取观众列表，请刷新重试。';});})();
</script>'''


def build(config):
    import fcntl
    import tempfile
    output = Path(config['output_dir'])
    output.parent.mkdir(parents=True, exist_ok=True)
    versions = output.parent / ('.' + output.name + '_versions')
    versions.mkdir(exist_ok=True)
    with (versions / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.exists() and not output.is_symlink():
            raise ValueError('公开目录已存在且不是版本指针，不能直接覆盖')
        with tempfile.TemporaryDirectory(prefix='building-', dir=versions) as staging:
            result = _build({**config, 'output_dir': staging})
            target = versions / result['plan_id']
            if not target.exists():
                Path(staging).rename(target)
            else:
                if any((Path(staging) / name).read_bytes() != (target / name).read_bytes() for name in result['files']):
                    raise ValueError('已构建版本内容不一致')
            temporary = output.with_name(output.name + '.next')
            if temporary.is_symlink():
                temporary.unlink()
            temporary.symlink_to(target.resolve(), target_is_directory=True)
            temporary.replace(output)
            result['directory'] = str(target.resolve())
    return result


def _build(config):
    root = Path(config['workspace'])
    current = json.loads((root / 'current.json').read_text())
    if current['room_id'] != config['room_id'] or current['scope'] != config['scope']:
        raise ValueError('当前档案属于其他主播')
    material_path = root / 'runs' / current['plan_id'] / 'material.json'
    material = json.loads(material_path.read_text())
    from .audience_crm import digest
    if digest(material) != current['plan_id'] or material['room_id'] != config['room_id']:
        raise ValueError('当前档案素材摘要不匹配')
    safe_url(config['home_url'], config['home_url'])
    output = Path(config['output_dir'])
    private = root.resolve()
    if private.is_relative_to(output.resolve()):
        raise ValueError('私有工作区不能放进公开目录')
    output.mkdir(parents=True, exist_ok=True)
    rows, files = [], ['index.html', 'index.json']
    uids = {str(uid) for p in material['people'].values()
            for uid in [p.get('uid'), *p.get('observed_uids', [])] if uid}
    for pid, person in material['people'].items():
        state = current['states'].get(pid, {})
        if person['evidence']:
            validate_state(person, state, material)
        rows.append(public_person(person, state, uids))
        directory = output / pid
        directory.mkdir(exist_ok=True)
        (directory / 'index.html').write_text(detail(person, state, material, config, uids))
        files.append(pid + '/index.html')
    atomic_json(output / 'index.json', {'scope': config['scope'], 'label': config['label'], 'people': rows})
    body = (f'<nav><a href="{esc(config["home_url"])}">场次主页</a></nav><h1>{esc(config["label"])}的观众备忘</h1>'
            '<p>先记起这个人，再接上上次的话。</p><label for="query">查昵称、曾用昵称或接话线索</label>'
            '<input id="query" type="search" placeholder="输入昵称或关键词" autocomplete="off">'
            '<p><label for="sort">排序 </label><select id="sort"><option value="useful">有待办和互动线索优先</option>'
            '<option value="recent">最近有记录</option></select></p><p id="count" role="status">正在读取观众列表…</p>'
            '<div class="cards" id="people"></div><p><button id="more" hidden>显示更多</button></p>')
    (output / 'index.html').write_text(crm_page(config['label'] + '的观众备忘', body, SCRIPT))
    return {'scope': config['scope'], 'people': len(rows), 'files': files, 'plan_id': current['plan_id']}
