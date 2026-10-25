import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';

type Segment = {seq:number; start_utc:string; end_utc:string; text:string; status:string;
  queue_wait_ms:number; infer_ms:number; rms_dbfs:number; error:string|null; coverage_ratio?:number|null};
type Room = {room_id:string; title:string; url:string; cover_url:string; status:string;
  record_mode:'none'|'audio'|'video';asr_enabled:boolean;preview_enabled:boolean;
  recording_status:string;recorded_seconds:number;recording_files:number;recording_bytes:number;
  error:string|null; chunk_seconds:number; segments:Segment[];items:Item[]};
type Item = {id:number;room_id:string;kind:'transcript'|'danmaku'|'gift';at_utc:string;received_utc:string;text:string;
  author?:string|null;seq?:number|null;status?:string|null;error?:string|null};
type Change = {kind:'room';data:Room}|{kind:'segment';data:Segment&{room_id:string}}|{kind:'item';data:Item};
type State = {rooms:Room[]; queue_depth:number; queue_capacity:number; asr_ready:boolean;
  asr_enabled:boolean;
  overload_count:number; inflight:{room_id:string;seq:number}|null;active_count:number;pending_count:number;
  asr_restarts:number;asr_retries:number;asr_error:string|null};
const labels:Record<string,string> = {
  starting:'启动中',capturing:'采集中',queued:'排队中',transcribing:'转写中',stopped:'已停止',
  error:'异常',silent:'静音跳过',done:'已转写',empty:'未识别到文字',cancelled:'已取消',truncated:'识别截断',reconnecting:'正在重连'
};
const base = import.meta.env.BASE_URL;
const api = async(path:string, init?:RequestInit) => {
  const response = await fetch(base + 'api/' + path, init);
  const result = await response.json();
  if(!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail));
  return result;
};
const time = (iso:string) => new Date(iso).toLocaleTimeString('zh-CN',{hour12:false});

function RoomPreview({room}:{room:Room}){
  const container=useRef<HTMLDivElement>(null);
  const [visible,setVisible]=useState(false);
  const [image,setImage]=useState('');
  useEffect(()=>{const observer=new IntersectionObserver(entries=>setVisible(entries[0].isIntersecting));
    if(container.current)observer.observe(container.current);return()=>observer.disconnect()},[]);
  useEffect(()=>{
    if(!room.preview_enabled||room.status!=='capturing'||!visible)return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;
    let current='';let alive=true;
    async function refresh(){
      try{if(!document.hidden){const response=await fetch(base+`api/rooms/${room.room_id}/preview.jpg`,{signal:controller.signal,cache:'no-store'});
        if(response.ok){const blob=await response.blob();if(!alive)return;
          const next=URL.createObjectURL(blob);if(current)URL.revokeObjectURL(current);current=next;setImage(next)}}
      }catch{/* 下次请求重试，不影响弹幕时间线。 */}
      if(alive)timer=setTimeout(()=>void refresh(),333);
    }
    void refresh();return()=>{alive=false;controller.abort();if(timer)clearTimeout(timer);if(current)URL.revokeObjectURL(current);setImage('')};
  },[room.preview_enabled,room.status,room.room_id,visible]);
  return <div className="cover" ref={container}>{image?<img src={image}/>:<div className="placeholder">{room.room_id.slice(-4)}</div>}
    <span className="muted">{room.preview_enabled?'按需缩略图':'缩略图禁用'}</span></div>;
}

const RoomCard = React.memo(function RoomCard({room,status,onError}:{room:Room;status:string;onError:(s:string)=>void}){
  const scroller=useRef<HTMLDivElement>(null);
  const [following,setFollowing]=useState(true);
  const latest=room.segments.at(-1);
  useLayoutEffect(()=>{
    if(following&&scroller.current)scroller.current.scrollTop=scroller.current.scrollHeight;
  },[room.items,following]);
  return <article className="card">
    <RoomPreview room={room}/>
    <div className="card-body"><div className="room-heading"><a href={room.url} target="_blank" rel="noreferrer">{room.title.replace(/ - 哔哩哔哩直播.*$/,'')}</a><span className={'badge '+status}>{labels[status]||status}</span></div>
    <div className="room-sub"><span>房间 {room.room_id} · {room.chunk_seconds} 秒 / 段</span><button className="text-button" disabled={room.status==='stopped'} onClick={()=>api(`rooms/${room.room_id}/stop`,{method:'POST'}).catch(e=>onError(e.message))}>停止监控</button></div>
    {room.error&&<p className="room-error">{room.error}</p>}
    <p className="room-sub">保存：{({none:'关闭',audio:'原始音频',video:'原始音视频'})[room.record_mode]} · ASR：{room.asr_enabled?'启用':'禁用'} · 弹幕 / 礼物：保存并显示</p>
    {room.record_mode!=='none'&&<p className="room-sub">录制 {room.recording_status==='recording'?'进行中':room.recording_status==='starting'?'启动中':room.recording_status==='stopped'?'已结束':room.recording_status} · {(room.recorded_seconds/60).toFixed(1)} 分钟 · {(room.recording_bytes/1e6).toFixed(1)} MB · {room.recording_files} 文件</p>}
    <div className="timeline-tools"><button className="text-button" aria-pressed={following} onClick={()=>{setFollowing(true);if(scroller.current)scroller.current.scrollTop=scroller.current.scrollHeight}}>{following?'跟随最新':'已暂停·回最新'}</button><a href={base+'api/export?room_id='+room.room_id}>导出本房快照</a></div>
    <div className={'segments'+(following?' following':'')} ref={scroller} onScroll={e=>{const n=e.currentTarget;setFollowing(n.scrollHeight-n.scrollTop-n.clientHeight<32)}}>
      {!room.items.length?<p className="waiting">等待弹幕、礼物或首个音频切片…</p>:room.items.map(item=><div className={'segment '+item.kind+' '+(item.status||'')} key={item.id}>
        <div className="segment-meta"><time>{time(item.at_utc)}</time><span>{item.kind==='gift'?'礼物 / 付费互动':item.kind==='danmaku'?'弹幕':`语音转写 #${item.seq} · ${labels[item.status||'']||item.status}`}</span></div>
        <p>{item.author&&<b className="chat-author">{item.author}：</b>}{item.text||item.error||(item.status==='silent'?'音量低于阈值，已跳过识别':item.status==='empty'?'本段未识别到文字':'等待识别…')}</p>
      </div>)}
    </div>
    <footer>{room.asr_enabled?<><span>等待 {latest?`${(latest.queue_wait_ms/1000).toFixed(1)}s`:'—'}</span><span>推理 {latest?`${(latest.infer_ms/1000).toFixed(1)}s`:'—'}</span><span>{latest?`${latest.rms_dbfs} dBFS`:'等待音频'}</span><span>覆盖 {latest?.coverage_ratio!=null?`${(latest.coverage_ratio*100).toFixed(0)}%`:'—'}</span></>:<span>ASR 禁用 · 不创建识别音频队列</span>}</footer>
    </div></article>;
});

function App(){
  const [state,setState] = useState<State>({rooms:[],queue_depth:0,queue_capacity:20,asr_ready:false,asr_enabled:false,overload_count:0,inflight:null,active_count:0,pending_count:0,asr_restarts:0,asr_retries:0,asr_error:null});
  const [recordMode,setRecordMode]=useState('audio');
  const [asrEnabled,setAsrEnabled]=useState(false);
  const [previewEnabled,setPreviewEnabled]=useState(false);
  const [connected,setConnected] = useState(false);
  const [url,setUrl] = useState('');
  const [seconds,setSeconds] = useState(30);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [candidates,setCandidates] = useState<{url:string;title:string}[]>([]);
  const [fullscreen,setFullscreen]=useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>|null>(null);
  const changes=useRef<Change[]>([]);
  useEffect(()=>{const update=()=>setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange',update);return()=>document.removeEventListener('fullscreenchange',update)},[]);
  useEffect(()=>{
    let alive=true;
    const enqueue=(change:Change)=>{changes.current.push(change);if(timer.current)return;
      timer.current=setTimeout(()=>{timer.current=null;const batch=changes.current;changes.current=[];
        if(!alive)return;
        setState(s=>{let rooms=s.rooms;
          for(const change of batch){const id=change.data.room_id;const index=rooms.findIndex(r=>r.room_id===id);
            if(change.kind==='room'){
              const old=rooms[index];const next={...change.data,segments:old?.segments||[],items:old?.items||[]};
              rooms=index<0?[...rooms,next]:rooms.map((r,i)=>i===index?next:r);
            }else if(index>=0){const old=rooms[index];let next:Room;
              if(change.kind==='segment'){const seg=change.data;next={...old,segments:[...old.segments.filter(x=>x.seq!==seg.seq),seg].sort((a,b)=>a.seq-b.seq).slice(-20)}}
              else{const item=change.data;next={...old,items:[...old.items.filter(x=>x.id!==item.id),item].sort((a,b)=>Date.parse(b.received_utc)-Date.parse(a.received_utc)||b.id-a.id).slice(0,80).sort((a,b)=>Date.parse(a.at_utc)-Date.parse(b.at_utc)||a.id-b.id)}}
              rooms=rooms.map((r,i)=>i===index?next:r);
            }
          }return {...s,rooms}});
      },200)};
    const events = new EventSource(base + 'api/events');
    events.onopen=()=>setConnected(true);
    events.onerror=()=>setConnected(false);
    events.addEventListener('snapshot',e=>{changes.current=[];setState(JSON.parse((e as MessageEvent).data))});
    for(const kind of ['room','segment','item'] as const)events.addEventListener(kind,e=>enqueue({kind,data:JSON.parse((e as MessageEvent).data)}));
    events.addEventListener('metrics',e=>setState(s=>({...s,...JSON.parse((e as MessageEvent).data)})));
    return ()=>{alive=false;events.close();if(timer.current)clearTimeout(timer.current);timer.current=null;changes.current=[]};
  },[]);
  async function add(value:string){
    setBusy(true);setError('');
    try{await api('rooms',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:value,chunk_seconds:seconds,record_mode:recordMode,asr_enabled:asrEnabled,preview_enabled:previewEnabled})});setUrl('')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  const active=state.active_count+state.pending_count;
  return <main>
    <header><div><div className="eyebrow">本地 CDP 静音捕获 · Qwen3-ASR</div><h1>B站多路直播监控看板</h1><p>多路直播语音、弹幕与礼物混合时间线。</p></div>
      <div className="header-actions"><button className="secondary" onClick={async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}catch(e){setError((e as Error).message)}}}>{fullscreen?'退出全屏':'进入全屏'}</button><span className={'connection '+(connected?'online':'')}><i/>{connected?'实时连接正常':'正在重连'}</span></div></header>
    <section className="metrics">
      <div><span>监控任务（含重连 / 启动）</span><strong>{active}<small> 路</small></strong></div>
      <div><span>队列深度</span><strong>{state.queue_depth}<small> / {state.queue_capacity}</small></strong></div>
      <div><span>ASR · 重启 {state.asr_restarts} / 重试 {state.asr_retries}</span><strong className="word">{!state.asr_enabled?'禁用':state.asr_ready?'已就绪':state.asr_error?'等待恢复':'正在加载'}</strong></div>
      <div><span>队列过载次数</span><strong className={state.overload_count?'warning':''}>{state.overload_count}</strong></div>
    </section>
    <form onSubmit={e=>{e.preventDefault();void add(url)}}>
      <input aria-label="直播间链接或房间号" placeholder="输入直播间链接或房间号" value={url} onChange={e=>setUrl(e.target.value)} required/>
      <select aria-label="切片窗口" value={seconds} onChange={e=>setSeconds(Number(e.target.value))}>{[5,10,20,30,60].map(n=><option key={n} value={n}>{n} 秒 / 段</option>)}</select>
      <select aria-label="保存类型" value={recordMode} onChange={e=>setRecordMode(e.target.value)}><option value="audio">只保存音频</option><option value="video">保存视频＋音频</option><option value="none">不保存媒体</option></select>
      <label><input type="checkbox" checked={asrEnabled} onChange={e=>setAsrEnabled(e.target.checked)}/>ASR</label>
      <label><input type="checkbox" checked={previewEnabled} onChange={e=>setPreviewEnabled(e.target.checked)}/>缩略图</label>
      <button disabled={busy}>{busy?'正在连接…':'添加直播间'}</button>
      <button type="button" className="secondary" disabled={busy} onClick={async()=>{
        setBusy(true);setError('');try{setCandidates(await api('discover?limit=20'))}catch(e){setError((e as Error).message)}finally{setBusy(false)}
      }}>发现直播</button>
    </form>
    {error&&<div className="alert" role="alert">{error}<button onClick={()=>setError('')}>×</button></div>}
    {state.asr_error&&<div className="alert" role="alert">模型：{state.asr_error}</div>}
    {candidates.length>0&&<section className="candidates">{candidates.map(c=><button disabled={busy} key={c.url} className="secondary" onClick={()=>void add(c.url)}>{c.title||c.url}</button>)}</section>}
    <div className="section-title"><h2>直播间 <span>{state.rooms.length}</span></h2><a className="export-link" href={base+'api/export'}>导出全部快照</a></div>
    {state.rooms.length===0&&<section className="empty">当前暂无监控房间，请输入直播间链接或房间号开始监听。</section>}
    <section className="grid">{state.rooms.map(room=><RoomCard key={room.room_id} room={room} status={room.status==='capturing'&&state.inflight?.room_id===room.room_id?'transcribing':room.status} onError={setError}/>)}</section>
    <p className="footnote">每房间显示最近80条弹幕、礼物与转写；完整时间线保存于本地SQLite，可导出JSONL。金币、银币与SC人民币金额分开；盲盒结果标价不代表真实付费或主播净收入。游戏声和背景音乐可能造成误识别。</p>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
