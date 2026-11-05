"""可独立载入的静态观众导航；不导入数据库或运行时。"""
import json
import re
from urllib.parse import urlsplit

NAVIGATION = r'''<script id="audience-navigation">
(()=>{const base=new URL(__URL__),names=new Map();
function fill(){for(const cell of document.querySelectorAll('#gift-summary tbody tr td:first-child')){
if(cell.querySelector('a'))continue;const name=cell.textContent.trim(),ids=names.get(name)||[];
const a=document.createElement('a');a.textContent=name;const url=new URL(ids.length===1?ids[0]+'/':'',base);
if(ids.length!==1)url.searchParams.set('q',name);a.href=url.href;cell.replaceChildren(a);}}
const nav=document.createElement('nav');nav.id='audience-entry';const a=document.createElement('a');a.href=base.href;a.textContent='观众备忘';
nav.append(a);(document.querySelector('main')||document.body).prepend(nav);
fetch(new URL('index.json',base),{cache:'no-cache'}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{
for(const p of data.people||[]){if(!/^[a-f0-9]{32}$/.test(p.id))continue;for(const name of new Set(p.names||[])){
const list=names.get(name)||[];list.push(p.id);names.set(name,list);}}fill();new MutationObserver(fill).observe(document.body,{childList:true,subtree:true});}).catch(()=>{});
})();</script>'''


def with_audience_links(text, url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('观众目录必须是有效HTTPS地址')
    script = NAVIGATION.replace('__URL__', json.dumps(url.rstrip('/') + '/').replace('<', r'\u003c'))
    if 'id="audience-navigation"' in text:
        return re.sub(r'<script id="audience-navigation">.*?</script>', lambda _: script, text, flags=re.S)
    if '</body>' not in text:
        raise ValueError('页面缺少body结束标签')
    return text.replace('</body>', script + '</body>')
