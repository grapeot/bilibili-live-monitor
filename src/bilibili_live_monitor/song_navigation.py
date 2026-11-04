"""静态场次页的按歌名、时间定位；兼容历史图表。"""

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
    if 'id="song-navigation"' in page:
        return page
    closing = '</body>' if '</body>' in page else '</html>'
    if closing not in page:
        raise ValueError('场次HTML缺少结束标签')
    return page.replace(closing, SONG_NAVIGATION + closing)
