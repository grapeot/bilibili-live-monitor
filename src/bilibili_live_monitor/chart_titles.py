"""直播静态页的右侧图名：新模板与历史页面共用。"""
import html

CHART_SCROLL_SCRIPT = r'''<script id="linked-chart-scroll">
(()=>{if(window.__linkedChartScroll)return;
const selector='section[data-chart],.detail-chart',synchronized=new WeakMap();
function charts(){return [...document.querySelectorAll(selector)];}
document.addEventListener('scroll',event=>{const source=event.target;if(!source.isConnected||!source.matches?.(selector))return;
const expected=synchronized.get(source);synchronized.delete(source);
if(expected!==undefined&&Math.abs(source.scrollLeft-expected)<=1)return;
const range=source.scrollWidth-source.clientWidth;if(range<=0)return;
const position=Math.max(0,Math.min(1,source.scrollLeft/range));
for(const target of charts()){if(target===source)continue;
const left=position*(target.scrollWidth-target.clientWidth);
if(Math.abs(target.scrollLeft-left)>1){target.scrollLeft=left;synchronized.set(target,target.scrollLeft);}}
},true);
window.__linkedChartScroll={latest(){requestAnimationFrame(()=>{for(const target of charts()){
target.scrollLeft=target.scrollWidth-target.clientWidth;synchronized.set(target,target.scrollLeft);}});}};
})();</script>'''


def right_title(name, width=1110):
    return (f'<text data-chart-title-side="right" x="{width - 12}" y="-10" '
            'text-anchor="end" style="font-size:14px;font-weight:600;fill:#334155">'
            + html.escape(name) + '</text>')


CHART_TITLE_SCRIPT = r'''<script id="right-chart-titles">
(()=>{if(window.__rightChartTitles)return;window.__rightChartTitles=true;
function fill(){for(const svg of document.querySelectorAll('svg')){
if(svg.querySelector('[data-chart-title-side="right"]'))continue;
const heading=svg.closest('section')?.querySelector('h2');
const label=svg.getAttribute('aria-label')||heading?.firstChild?.textContent;
if(!label)continue;const width=svg.viewBox?.baseVal?.width;if(!width||width<200)continue;
const {x,y,height}=svg.viewBox.baseVal;
svg.setAttribute('viewBox',`${x} ${y-26} ${width} ${height+26}`);
const t=document.createElementNS('http://www.w3.org/2000/svg','text');
for(const [k,v] of Object.entries({'data-chart-title-side':'right',x:width-12,y:y-10,'text-anchor':'end',style:'font-size:14px;font-weight:600;fill:#334155'}))t.setAttribute(k,v);
t.textContent=label.trim();svg.append(t);}}
fill();new MutationObserver(fill).observe(document.body||document.documentElement,{childList:true,subtree:true});})();
</script>'''


def with_chart_titles(page):
    closing = '</main>' if '</main>' in page else '</body>' if '</body>' in page else '</html>'
    if closing not in page:
        raise ValueError('静态页面缺少结束标签')
    if 'id="right-chart-titles"' not in page:
        page = page.replace(closing, CHART_TITLE_SCRIPT + closing)
    if 'id="linked-chart-scroll"' not in page:
        page = page.replace(closing, CHART_SCROLL_SCRIPT + closing)
    return page
