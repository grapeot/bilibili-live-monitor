"""直播静态页的右侧图名：新模板与历史页面共用。"""
import html


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
    if 'id="right-chart-titles"' in page:
        return page
    closing = '</main>' if '</main>' in page else '</body>' if '</body>' in page else '</html>'
    if closing not in page:
        raise ValueError('静态页面缺少结束标签')
    return page.replace(closing, CHART_TITLE_SCRIPT + closing)
