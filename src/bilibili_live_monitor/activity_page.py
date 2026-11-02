"""本地活动复核页：颜色只是候选，可试听并导出人工修订。"""
import json

from .trends_render import render

SCRIPT = r'''
const state = JSON.parse(document.getElementById('activity-data').textContent);
const names={singing:'唱歌',speaking:'讲话',mixed:'混合',unknown:'不确定'};
const sourceNames={probable_live:'主播现场演唱候选',recording:'播放录音／他人演唱',uncertain:'声源不确定',none:'无歌曲活动'};
const win=state.windows, lo=Date.parse(state.since)/1000, hi=Date.parse(state.until)/1000;
let offset=0, stopAt=null, playRequest=0;
const audio=document.getElementById('listen');
const clock=t=>new Date(t*1000).toLocaleTimeString('zh-CN',{timeZone:state.timezone,hour12:false});
const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function selection(){
 const filter=document.getElementById('kind-filter').value;
 return win.map((w,i)=>({w,i})).filter(({w})=>filter==='all'||w.label===filter);
}
function table(){
 const rows=selection(); offset=Math.max(0,Math.min(offset,Math.max(0,Math.ceil(rows.length/30)-1)*30));
 document.getElementById('page-count').textContent=`${rows.length}块 · ${offset+1}–${Math.min(offset+30,rows.length)}`;
 document.getElementById('review-rows').innerHTML=rows.slice(offset,offset+30).map(({w,i})=>
  `<tr><td>${clock(w.start)}<br>${clock(w.end)}</td><td><select data-i="${i}" aria-label="活动分类 ${i}">`+
  Object.entries(names).map(([k,n])=>`<option value="${k}" ${k===w.label?'selected':''}>${n}</option>`).join('')+
   `</select><br><select data-source-i="${i}" aria-label="歌曲声源 ${i}">`+
   Object.entries(sourceNames).map(([k,n])=>`<option value="${k}" ${k===w.music_source?'selected':''}>${n}</option>`).join('')+
   `</select><br><small>${w.provenance==='manual'?'人工修订':w.provenance==='semantic_review'?'语义复核候选':'自动候选'}</small></td>`+
  `<td><button data-play="${i}" ${w.audio?'':'disabled'}>试听</button></td>`+
  `<td>${esc(w.text)}<br><small>${esc(w.reason)}</small>`+
  (w.text_features?`<small>转写密度 ${w.text_features.chars_per_second} 字/秒 · 互动词 ${w.text_features.interaction_keyword_count}处 · 互动句文字占比 ${Math.round(w.text_features.interaction_char_ratio*100)}%</small>`:'')+
  `</td></tr>`).join('');
}
function paint(){
 const edges=[...new Set([lo,hi,...win.flatMap(w=>[Math.max(lo,w.start),Math.min(hi,w.end)])])].sort((a,b)=>a-b);
 let rectangles='';const live=[];
 for(let i=0;i<edges.length-1;i++){
  const a=edges[i],b=edges[i+1]; if(b<=lo||a>=hi)continue;
   const active=win.filter(w=>w.start<b&&w.end>a), labels=new Set(active.map(w=>
    w.label==='singing'&&w.music_source!=='probable_live'?'unknown':w.label));
   const label=labels.size===1?[...labels][0]:'unknown';
   if(label!=='singing'&&active.length)continue;
   const x=65+(a-lo)/(hi-lo)*980,width=(b-a)/(hi-lo)*980;
   if(!active.length){
    rectangles+=`<rect x="${x}" y="16" width="${width}" height="191" fill="#94a3b8" opacity=".25" data-missing-audio="true"><title>${clock(a)}–${clock(b)} 无音频数据覆盖</title></rect>`;
    continue;
   }
   live.push({start:a,end:b});
   rectangles+=`<rect x="${x}" y="16" width="${width}" height="191" fill="#22c55e" opacity=".30"><title>${clock(a)}–${clock(b)} ${names[label]}</title></rect>`;
   rectangles+=`<rect x="${x}" y="210" width="${width}" height="6" fill="#22c55e" opacity=".95"><title>${clock(a)}–${clock(b)} ${names[label]}</title></rect>`;
 }
  document.querySelectorAll('[data-activity-layer]').forEach(g=>g.innerHTML=rectangles);
  const counts={};win.forEach(w=>counts[w.label]=(counts[w.label]||0)+1);
  const songs=win.filter(w=>w.label==='singing');
  document.getElementById('label-counts').textContent=[
   `现场演唱候选 ${songs.filter(w=>w.music_source==='probable_live').length}块`,
   `录音中的歌曲 ${songs.filter(w=>w.music_source==='recording').length}块`,
   `声源不明的歌曲 ${songs.filter(w=>w.music_source!=='probable_live'&&w.music_source!=='recording').length}块`,
   ...['speaking','mixed','unknown'].map(k=>`${names[k]} ${counts[k]||0}块`)
  ].join(' · ');
  if(state.songs?.length){
   const visible=state.songs.map(song=>({...song,segments:song.segments.flatMap(segment=>live.map(b=>({
    start:Math.max(segment.start,b.start),end:Math.min(segment.end,b.end)
   })).filter(b=>b.start<b.end))}));
   const overlays=visible.flatMap(song=>song.segments.map(b=>
    `<rect class="song-hit" data-song-id="${esc(song.id)}" data-song-title="${esc(song.title)}" data-song-time="${clock(b.start)}–${clock(b.end)}" data-start="${b.start}" data-end="${b.end}" x="${65+(b.start-lo)/(hi-lo)*980}" y="16" width="${(b.end-b.start)/(hi-lo)*980}" height="191" aria-label="${esc(song.title)} · ${clock(b.start)}–${clock(b.end)}"></rect>`)).join('');
   document.querySelectorAll('[data-song-layer]').forEach(g=>g.innerHTML=overlays);
   document.querySelectorAll('.song-chip').forEach(button=>{
    const song=visible.find(s=>s.id===button.dataset.songId);
    button.disabled=!song?.segments.length;
    button.querySelector('span').textContent=button.disabled?'当前无有效唱段':`${clock(song.segments[0].start)}–${clock(song.segments.at(-1).end)}`;
    button.dataset.songTime=button.querySelector('span').textContent;
   });
   document.dispatchEvent(new Event('song-bands-updated'));
  }
}
document.getElementById('review-rows').addEventListener('change',e=>{
 const sourceEdit=e.target.dataset.sourceI!==undefined;
 const index=sourceEdit?e.target.dataset.sourceI:e.target.dataset.i;
 if(index===undefined)return;
 const w=win[Number(index)];
 if(sourceEdit){
  if(e.target.value==='none'&&w.label==='singing'){table();return;}
  w.music_source=e.target.value;
 }else{
  w.label=e.target.value;
   if(['singing','mixed'].includes(w.label)&&(!w.music_source||w.music_source==='none'))w.music_source='uncertain';
 }
 w.provenance='manual';w.reason=sourceEdit?'人工复核声源修订':'人工复核修订';
 paint();table();document.getElementById('edit-status').textContent='有未导出的人工修订';
});
document.getElementById('review-rows').addEventListener('click',async e=>{
 if(e.target.dataset.play===undefined)return;
 const w=win[Number(e.target.dataset.play)]; if(!w.audio)return;
 const request=++playRequest;
 stopAt=w.relative_end;
 document.getElementById('playing').textContent=`${clock(w.start)}–${clock(w.end)} ${w.text}`;
 try{
  const target=new URL(w.audio,location.href).href;
  if(audio.src!==target){
   const loaded=new Promise((resolve,reject)=>{audio.onloadedmetadata=resolve;audio.onerror=()=>reject(new Error('音频读取失败'));});
   audio.src=target;audio.load();await loaded;
  }
  if(request!==playRequest)return;
  audio.currentTime=w.relative_start;await audio.play();
 }catch(error){document.getElementById('playing').textContent=String(error);}
});
audio.addEventListener('timeupdate',()=>{if(stopAt!==null&&audio.currentTime>=stopAt)audio.pause();});
document.getElementById('kind-filter').addEventListener('change',()=>{offset=0;table();});
document.getElementById('prev').onclick=()=>{offset-=30;table();};
document.getElementById('next').onclick=()=>{offset+=30;table();};
document.getElementById('export-labels').onclick=()=>{
 const data={schema_version:1,dataset_id:state.dataset_id,labels:win.map(w=>({source_key:w.source_key,label:w.label,music_source:w.music_source,provenance:w.provenance,reason:w.reason}))};
 const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
 const link=document.createElement('a');link.href=url;link.download='labels.reviewed.json';link.click();
 setTimeout(()=>URL.revokeObjectURL(url),1000);document.getElementById('edit-status').textContent='已导出修订，刷新前请保留文件';
};
table();paint();
'''


def page_html(bundle):
    trend = bundle['trends']
    page = render(trend, activities=bundle['bands'], songs=bundle.get('songs'))
    page = page.replace('href="data.json"', 'href="activity.json"').replace('下载冻结数据', '下载活动数据')
    note = '''<section id="activity-help"><h2>活动候选 · 可试听修订</h2>
<p><span style="background:#22c55e4d;color:#126b36;padding:4px 8px;border-radius:4px">■ 绿色背景：主播现场演唱候选</span>　
<span style="background:#94a3b840;padding:4px 8px;border-radius:4px">■ 灰色：无数据覆盖</span>　
播放录音、声源不确定、讲话及混合不着色。</p>
<p>颜色来自ASR文本启发式或语义复核候选，不是声学分类真值。歌词、独白、背景音乐和录播可能误判；
时间是源片与约30秒切块的估算。转写密度按文字数/整块时长计算，不是实际语速，不能单独区分说唱。
请试听并修订，不能据此直接归因关注增长。</p><p id="label-counts"></p></section>'''
    review = '''<section id="review"><h2>逐块复核</h2><p>试听会从该块起点播放，到终点暂停。
修改下拉选项会同步更新四图色带；修改暂存于本页，刷新前请导出JSON。
下一轮用 <code>activity build --labels labels.reviewed.json</code> 复用修订。</p>
<label>筛选 <select id="kind-filter"><option value="all">全部</option><option value="singing">唱歌</option>
<option value="speaking">讲话</option><option value="mixed">混合</option><option value="unknown">不确定</option></select></label>
<button id="prev">上一页</button><button id="next">下一页</button><span id="page-count"></span>
<button id="export-labels">导出修订</button><span id="edit-status"></span>
<table><thead><tr><th>估算时间</th><th>分类</th><th>音频</th><th>ASR / 依据</th></tr></thead><tbody id="review-rows"></tbody></table>
<audio id="listen" controls preload="none"></audio><p id="playing"></p></section>'''
    styles = '''<style>#review{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid #e0e6ef;padding:10px;text-align:left;vertical-align:top}td:first-child{white-space:nowrap}
td small{margin:0}button,select{padding:7px;margin:4px;border:1px solid #ccd5e3;border-radius:6px;background:white;color:#17273f}
audio{width:100%;margin-top:16px}#playing{max-height:90px;overflow:auto}#edit-status{font-size:12px;color:#ab5e0c}</style>'''
    public_windows = [{k: v for k, v in w.items() if k in (
        'source_key', 'start', 'end', 'relative_start', 'relative_end', 'text', 'label', 'music_source', 'reason', 'provenance', 'audio',
        'text_features')}
        for w in bundle['windows']]
    data = json.dumps({'windows': public_windows, 'songs': bundle.get('songs', []), 'dataset_id': bundle['dataset_id'],
                       'since': trend['since'], 'until': trend['until'],
                       'timezone': trend['timezone']}, ensure_ascii=False)
    data = data.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    insertion = page.index('<section>')
    page = page[:insertion]+note+page[insertion:]
    return page.replace('</main></html>', review+styles+
                        f'<script id="activity-data" type="application/json">{data}</script>'+
                        f'<script>{SCRIPT}</script></main></html>')
