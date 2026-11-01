import React, {useEffect, useState} from 'react';

type Scope = {room_id:string;session_id:string;since:string;source_session:string};
type Ref = {id:string;version:number};
type Card = {id:string;kind:'chat'|'medal'|'song';title:string;status:string;version:number;
  author:string;uid?:string;manual:boolean;eligible?:boolean;evidence:Ref[];
  pronunciation:{text:string;uncertain:boolean;override:boolean};guard_observation?:unknown};
type Snapshot = {config:Scope;cards:Card[];evidence:{id:string;version:number;latest_version?:number;data:Record<string,unknown>}[];
  worker:{status?:string;last_error?:string;calls?:number;cursor?:number;ai_cursor?:number}};
const base = import.meta.env.BASE_URL;
async function api(path:string, init?:RequestInit){
  const response=await fetch(base+'api/assistant/'+path,init);
  const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'操作失败');
  return result;
}
const names:Record<string,string>={pending:'待处理',responded:'已回应',dismissed:'已忽略',queued:'待唱',sung:'已唱',rejected:'已拒绝',done:'已完成'};
const choices={chat:['pending','responded','dismissed'],medal:['pending','done','dismissed'],song:['pending','queued','sung','rejected']};
const workerNames:Record<string,string>={live:'运行中',paused:'暂停：等待可靠直播状态',ended:'本场已结束',budget_paused:'调用预算已用完',retry_pending:'等待重试'};

export function Assistant(){
  const [sessions,setSessions]=useState<Scope[]>([]);
  const [session,setSession]=useState('');
  const [data,setData]=useState<Snapshot|null>(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [history,setHistory]=useState(false);
  useEffect(()=>{const controller=new AbortController();let alive=true;
    const refresh=async()=>{try{const rows:Scope[]=await api('sessions',{signal:controller.signal});
      if(alive){setSessions(rows);setSession(old=>old||rows[0]?.session_id||'')}}catch(e){if(alive)setError((e as Error).message)}};
    void refresh();const timer=setInterval(()=>void refresh(),5000);
    return()=>{alive=false;controller.abort();clearInterval(timer)};
  },[]);
  useEffect(()=>{if(!session)return;let alive=true;const controller=new AbortController();setData(null);
    const refresh=async()=>{try{const value=await api('state?session='+encodeURIComponent(session),{signal:controller.signal});
      if(alive)setData(value)}catch(e){if(alive)setError((e as Error).message)}};
    void refresh();const timer=setInterval(()=>void refresh(),2000);
    return()=>{alive=false;controller.abort();clearInterval(timer)};
  },[session]);
  async function mutate(path:string, method:string, body:unknown){
    setBusy(true);setError('');
    try{await api(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      setData(await api('state?session='+encodeURIComponent(session)))}
    catch(e){setError((e as Error).message);try{setData(await api('state?session='+encodeURIComponent(session)))}catch{/* 保留最后一次可见状态 */}}
    finally{setBusy(false)}
  }
  function cardView(card:Card){return <article className="assistant-card" key={card.id}>
    <p><strong>{card.author||'身份未确认'}</strong>{card.pronunciation.text&&<small className="reading"> {card.pronunciation.text}{card.pronunciation.uncertain?' · 读音待确认':''}</small>}</p>
    <p>{card.title}</p><p className="room-sub">{card.kind==='song'&&card.status==='pending'?'待确认':names[card.status]}{card.manual?' · 人工确认':''}</p>
    <div className="assistant-actions"><label>状态 <select aria-label={card.title+'状态'} disabled={busy} value={card.status}
      onChange={e=>void mutate('cards/'+encodeURIComponent(card.id),'PATCH',{session,expected_version:card.version,status:e.target.value})}>
      {choices[card.kind].map(s=><option value={s} key={s}>{s==='pending'&&card.kind==='song'?'待确认':names[s]}</option>)}</select></label>
      <button disabled={busy} onClick={()=>void mutate('cards/'+encodeURIComponent(card.id)+'/undo','POST',{session,expected_version:card.version})}>撤销上次状态</button>
    </div>
    <details><summary>证据与读音</summary>
      {card.evidence.map(ref=>{const e=data?.evidence.find(e=>e.id===ref.id&&e.version===ref.version);return <div key={ref.id}>
        <p>证据 {ref.id} · 引用版本 {ref.version}{e?.latest_version&&e.latest_version!==ref.version?' · 已有修订，请复核':''}</p><pre>{JSON.stringify(e?.data,null,2)}</pre></div>})}
      {card.guard_observation!=null&&<pre>{JSON.stringify(card.guard_observation,null,2)}</pre>}
      <form onSubmit={e=>{e.preventDefault();const form=new FormData(e.currentTarget);
        void mutate('pronunciation','PUT',{session,name:card.author,reading:String(form.get('reading')||'')})}}>
        <label>读音覆盖<input name="reading" aria-label={card.author+'读音覆盖'} defaultValue={card.pronunciation.override?card.pronunciation.text:''} placeholder="留空恢复自动读音" maxLength={300}/></label>
        <button disabled={busy||!card.author}>保存读音</button>
      </form>
    </details>
  </article>}
  return <section className="assistant" aria-label="直播助手">
    <h1>直播助手</h1><p>根据已采到的弹幕和语音整理待办，所有判断均可人工调整。</p>
    <label>房间 <select aria-label="助手房间" disabled={busy} value={session} onChange={e=>setSession(e.target.value)}>
      {!sessions.length&&<option value="">尚未配置场次</option>}
      {sessions.map(s=><option value={s.session_id} key={s.session_id}>{s.room_id} · {s.session_id}</option>)}</select></label>
    {error&&<p role="alert">{error}</p>}
    {data&&<><p className="room-sub">{workerNames[data.worker.status||'']||'等待 worker'} · 模型调用 {data.worker.calls||0} 次 · 读取进度 {data.worker.cursor||0} / AI 完成 {data.worker.ai_cursor||0}{data.worker.last_error?' · '+data.worker.last_error:''}</p>
      <label><input type="checkbox" checked={history} onChange={e=>setHistory(e.target.checked)}/>显示已完成 / 已忽略，供恢复或撤销</label>
      <div className="assistant-columns">{(['chat','medal','song'] as const).map(kind=>{
        const cards=data.cards.filter(c=>c.kind===kind&&(kind!=='medal'||c.eligible)
          &&(history||kind==='song'||c.status==='pending'));
        return <section key={kind}><h2>{{chat:'未回应弹幕',medal:'灯牌观众',song:'点歌单'}[kind]} <small>{cards.length}</small></h2>
          {kind==='medal'&&<p className="room-sub">有当前主播灯牌、已确认非舰长、尚未观察到点歌；不是刚点亮名单。</p>}
          {cards.length?cards.map(cardView):<p className="waiting">暂无符合条件的记录</p>}
        </section>})}</div></>}
  </section>;
}
