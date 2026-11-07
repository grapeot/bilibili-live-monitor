import React, {useEffect, useState} from 'react';
import {useInstance} from './Instance';

type Scope = {room_id:string;session_id:string;since:string;source_session:string};
type Ref = {id:string;version:number};
type Card = {id:string;kind:'chat'|'medal'|'song';title:string;status:string;version:number;
  author:string;uid?:string;manual:boolean;eligible?:boolean;evidence:Ref[];
  first_gift_at?:string|null;last_gift_at?:string|null;gift_quantity?:number;
  qualification_rule?:string;
  pronunciation:{text:string;uncertain:boolean;override:boolean;unknown?:string[];
    candidates?:{character:string;readings:string[]}[]};guard_observation?:unknown};
type Snapshot = {config:Scope;cards:Card[];evidence:{id:string;version:number;latest_version?:number;data:Record<string,unknown>}[];
  control?:{enabled:boolean;revision:number};
  agent?:{status?:string;compaction_count?:number;dense_window_drops?:number;truncated_tool_results?:number;
    truncated_query_results?:number;
    input_bytes?:number;byte_limit?:number;last_prompt_eval_count?:number|null;
    estimated_input_tokens?:number;context_token_limit?:number;
    aliases?:{uid:string;alias:string;version:number;status?:string}[];
    trace?:{at:number;tool:string;reason:string;error?:string|null;cached?:boolean}[]};
  worker:{status?:string;last_error?:string;calls?:number;cursor?:number;ai_cursor?:number;
    pending_observations?:number;last_observation?:number;last_analysis?:number}};
async function callApi(base:string,path:string, init?:RequestInit){
  const response=await fetch(base+'api/assistant/'+path,init);
  const result=await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'操作失败');
  return result;
}
const names:Record<string,string>={pending:'待处理',responded:'已回应',dismissed:'已忽略',queued:'待唱',sung:'已唱',rejected:'已拒绝',done:'已完成'};
const choices={chat:['pending','responded','dismissed'],medal:['pending','done','dismissed'],song:['pending','queued','sung','rejected']};
const workerNames:Record<string,string>={live:'运行中',paused:'暂停：等待可靠直播状态',ended:'本场已结束',budget_paused:'调用预算已用完',retry_pending:'等待重试',context_capacity_paused:'上下文容量暂停'};

function conciseReading(pronunciation:Card['pronunciation']){
  if(pronunciation.override)return pronunciation.text;
  return (pronunciation.candidates||[]).reduce((text,candidate)=>candidate.readings.length?
    text.split(`${candidate.character}(${candidate.readings.join(' / ')})`).join(candidate.readings[0]):text,
  pronunciation.text);
}

export function Assistant(){
  const {base}=useInstance();
  const api=(path:string,init?:RequestInit)=>callApi(base,path,init);
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
    <p><strong>{card.author||'身份未确认'}</strong>{card.pronunciation.text&&<small className="reading" aria-label="昵称读音"> {conciseReading(card.pronunciation)}{card.pronunciation.uncertain?' · 读音待确认':''}{!!card.pronunciation.unknown?.length&&` · 未知读音：${card.pronunciation.unknown.join('、')}`}</small>}</p>
    <p>{card.title}</p><p className="room-sub">{card.kind==='song'&&card.status==='pending'?'待确认':names[card.status]}{card.manual?' · 人工确认':''}</p>
    {card.kind==='medal'&&card.first_gift_at&&<p className="room-sub">粉丝团灯牌 ×{card.gift_quantity} · 首次收到 <time dateTime={card.first_gift_at}>{new Date(card.first_gift_at).toLocaleTimeString('zh-CN',{hour12:false})}</time></p>}
    <div className="assistant-actions"><label>状态 <select aria-label={card.title+'状态'} disabled={busy} value={card.status}
      onChange={e=>void mutate('cards/'+encodeURIComponent(card.id),'PATCH',{session,expected_version:card.version,status:e.target.value})}>
      {choices[card.kind].map(s=><option value={s} key={s}>{s==='pending'&&card.kind==='song'?'待确认':names[s]}</option>)}</select></label>
      <button disabled={busy} onClick={()=>void mutate('cards/'+encodeURIComponent(card.id)+'/undo','POST',{session,expected_version:card.version})}>撤销上次状态</button>
    </div>
    <details><summary>证据与读音</summary>
      {card.pronunciation.text&&<p aria-label="完整读音">{card.pronunciation.text}</p>}
      {card.pronunciation.uncertain&&<p>主行使用词典首候选供速读，不代表读音已确认；完整候选如上，未知字保留原字，可在下方覆盖读音。</p>}
      {card.evidence.map(ref=>{const e=data?.evidence.find(e=>e.id===ref.id&&e.version===ref.version);return <div key={`${ref.id}:${ref.version}`}>
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
    {data&&<><p className="room-sub">{workerNames[data.worker.status||'']||'等待 worker'} · 模型调用 {data.worker.calls||0} 次 · 待分析观察 {data.worker.pending_observations??0} 条 · 最近观察更新 {data.worker.last_observation?new Date(data.worker.last_observation*1000).toLocaleTimeString('zh-CN',{hour12:false}):'尚无'}{data.worker.last_error?' · '+data.worker.last_error:''}</p>
      <p>AI 助手：{data.control?.enabled?'已启用':'已关闭'} · 启动默认关闭；关闭时仍可查看与人工调整。
        <button type="button" disabled={busy||(!data.control?.enabled&&data.worker.status==='ended')}
          onClick={()=>void mutate('control','PUT',{session,enabled:!data.control?.enabled})}>
          {data.control?.enabled?'关闭 AI 助手':'启用 AI 助手'}</button></p>
      <details><summary>运行诊断</summary><p>源事件游标：{data.worker.cursor||0}；AI 已完成观察序号：{data.worker.ai_cursor||0}（两个序号不是同一计数）。最近分析完成：{data.worker.last_analysis?new Date(data.worker.last_analysis*1000).toLocaleTimeString('zh-CN',{hour12:false}):'尚无'}</p></details>
      {data.agent&&<details><summary>Agent 状态与查证轨迹</summary>
        <p>{({disabled:'AI已关闭',ready:'等待分析',sleeping:'休眠，等待新观察',working:'正在查证',turn_limit:'达到工具轮次上限',waiting_retry:'等待重试',budget_paused:'调用预算已用完',context_capacity_paused:'上下文容量暂停'})[data.agent.status||'']||data.agent.status}
          {' · '}上下文裁剪 {data.agent.compaction_count||0} 次 · 密集窗口移除 {data.agent.dense_window_drops||0} 轮 · 工具结果截断 {data.agent.truncated_tool_results||0} 次 · 查询覆盖截断 {data.agent.truncated_query_results||0} 次</p>
        <p>最近输入 {data.agent.input_bytes??'—'} / {data.agent.byte_limit??'—'} UTF-8字节；估算输入 {data.agent.estimated_input_tokens??'—'} / {data.agent.context_token_limit??'—'} tokens（预算代理，不是精确token数）；Ollama报告上次输入 {data.agent.last_prompt_eval_count??'未知'} tokens。</p>
        {!!data.agent.aliases?.length&&<p>称呼关系：{data.agent.aliases.map(a=>`${a.alias} → UID ${a.uid}（${a.status==='confirmed'?'已查证':a.status==='unconfirmed'?'未确认':'待复核'}）`).join('；')}</p>}
        <ol>{data.agent.trace?.map((entry,index)=><li key={`${entry.at}:${index}`}>{entry.tool}：{entry.reason}{entry.cached?' · 已有回执，未重复执行':''}{entry.error?` · ${entry.error}`:''}</li>)}</ol>
      </details>}
      <label><input type="checkbox" checked={history} onChange={e=>setHistory(e.target.checked)}/>显示已完成 / 已忽略，供恢复或撤销</label>
      <div className="assistant-columns">{(['chat','medal','song'] as const).map(kind=>{
        const cards=data.cards.filter(c=>c.kind===kind&&(kind!=='medal'||(c.eligible&&c.qualification_rule==='fan_club_gift_v1'))
          &&(history||kind==='song'||c.status==='pending'));
        return <section key={kind}><h2>{{chat:'未回应弹幕',medal:'送灯牌待跟进',song:'点歌单'}[kind]} <small>{cards.length}</small></h2>
          {kind==='medal'&&<p className="room-sub">本场收到粉丝团灯牌礼物、已确认非舰长，且尚未完成点歌跟进；佩戴粉丝牌等级不计入。</p>}
          {cards.length?cards.map(cardView):<p className="waiting">暂无符合条件的记录</p>}
        </section>})}</div></>}
  </section>;
}
