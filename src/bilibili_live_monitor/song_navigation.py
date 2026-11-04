"""静态场次页的按歌名、时间定位；兼容历史图表。"""
import json
import re
from urllib.parse import urlsplit

SONG_NAVIGATION = r'''<script id="song-navigation">
(()=>{function locate(){const p=new URLSearchParams(location.hash.slice(1));
const title=p.get('song'),at=Number(p.get('at'));if(!title||!Number.isFinite(at))return;
const detail=document.querySelector('#detail-data');if(detail){try{const songs=JSON.parse(detail.textContent).songs;
for(const svg of document.querySelectorAll('svg')){const hits=svg.querySelectorAll('.song-hit');hits.forEach((n,i)=>{const s=songs[i];if(s){n.dataset.songTitle=s.title;n.dataset.start=s.start;n.dataset.end=s.end;}});}}catch{}}
const nodes=[...document.querySelectorAll('[data-song-title][data-start]')].filter(n=>n.dataset.songTitle===title);
if(!nodes.length)return;nodes.sort((a,b)=>Math.abs(Number(a.dataset.start)-at)-Math.abs(Number(b.dataset.start)-at));
const n=nodes[0],chart=n.closest('[data-chart]')||n.closest('svg');
chart?.scrollIntoView({block:'center'});let parent=n.parentElement;
while(parent&&parent!==document.body){if(parent.scrollWidth>parent.clientWidth+5){
const a=n.getBoundingClientRect(),b=parent.getBoundingClientRect();parent.scrollLeft+=a.left-b.left-parent.clientWidth/3;break;}parent=parent.parentElement;}
setTimeout(()=>{const r=n.getBoundingClientRect();n.focus({preventScroll:true});n.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:r.left+Math.min(r.width/2,20),clientY:r.top+20}));
const tip=document.querySelector('#song-tooltip');if(tip){const heading=tip.querySelector('strong');if(heading)heading.textContent=title;else tip.textContent=title;
tip.hidden=false;tip.style.left=Math.max(12,Math.min(innerWidth-220,r.left))+'px';tip.style.top=Math.max(12,Math.min(innerHeight-90,r.top))+'px';}},150);
}addEventListener('hashchange',locate);if(document.readyState==='loading')addEventListener('DOMContentLoaded',locate);else locate();})();
</script>'''


def with_song_navigation(page):
    page = neutral_song_labels(page)
    if 'id="song-navigation"' in page:
        return page
    closing = '</body>' if '</body>' in page else '</html>'
    if closing not in page:
        raise ValueError('场次HTML缺少结束标签')
    return page.replace(closing, SONG_NAVIGATION + closing)


CATALOG_LINKS = r'''<script id="song-catalog-links">
(()=>{const base=new URL(__CATALOG_URL__),songs=new Map();
const normalize=s=>s.normalize('NFKC').trim().replace(/\s+/g,' ').toLowerCase();
function fill(){for(const row of document.querySelectorAll('#activity-summary tbody tr,#segments tr')){
const cell=row.children[1];if(!cell)continue;const raw=cell.textContent.trim();
if(!raw.startsWith('唱歌 · ')&&!cell.dataset.songCatalogTitle&&!cell.querySelector('[data-song-catalog-link]')&&!row.closest('#segments'))continue;
const title=raw.replace(/^唱歌\s*·\s*/,'').trim();
if(!title||['杂谈','唱歌','其他','歌名待确认'].includes(title))continue;
let link=cell.querySelector('[data-song-catalog-link]');
if(!link){link=document.createElement('a');link.dataset.songCatalogLink='true';link.textContent=title;
cell.replaceChildren(document.createTextNode(raw.slice(0,raw.indexOf(title))),link);}
const song=songs.get(normalize(title)),target=new URL(song?song+'/':'',base);
if(!song)target.searchParams.set('q',title);if(link.href!==target.href)link.href=target.href;
}}
fill();new MutationObserver(fill).observe(document.body||document.documentElement,{childList:true,subtree:true});
fetch(new URL('index.json',base),{cache:'no-cache'}).then(r=>{if(!r.ok)throw new Error('catalog');return r.json();})
.then(data=>{for(const song of data.songs||[]){if(/^[a-f0-9]{24}$/.test(song.id))songs.set(normalize(song.title),song.id);}fill();}).catch(()=>{});
})();</script>'''


def with_song_catalog_links(page, catalog_url=None):
    """已收录歌曲直达歌曲页，新歌暂时进入预填歌名的搜索页。"""
    if not catalog_url:
        return page
    parsed = urlsplit(catalog_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('歌曲目录必须为有效HTTPS地址')
    closing = '</body>' if '</body>' in page else '</html>'
    if closing not in page:
        raise ValueError('场次HTML缺少结束标签')
    value = json.dumps(catalog_url.rstrip('/') + '/', ensure_ascii=False).replace('<', '\\u003c')
    script = CATALOG_LINKS.replace('__CATALOG_URL__', value)
    if 'id="song-catalog-links"' in page:
        return re.sub(r'<script id="song-catalog-links">.*?</script>', lambda _: script, page, flags=re.S)
    return page.replace(closing, script + closing)


def neutral_song_labels(page):
    """旧静态模板去掉声源断言；不改变底层分类或片段边界。"""
    page = page.replace('唱歌与杂谈', '歌曲与杂谈').replace('绿色：唱歌；', '绿色：歌曲相关片段；')
    page = page.replace('主播现场演唱候选', '歌曲相关片段').replace('现场演唱候选', '歌曲相关片段')
    page = page.replace('可以看见唱的是什么歌', '可以查看歌曲名称')
    page = re.sub(r'<td>唱歌 · ([^<]*)</td>',
                  lambda m: '<td data-song-catalog-title="' + m[1].replace('"', '&quot;') + '">' + m[1] + '</td>', page)
    return page
