import React, {useEffect, useState} from 'react';
import {WelcomeCopy} from './WelcomeCopy';
import {useInstance} from './Instance';
import {CopyLikeScript} from './CopyLikeScript';

type Room = {room_id:string;label:string;intro:string;enabled:boolean;confirmed:string|null;delivery:string;error?:string};
type Like = {id:string;room_id:string;requested:number;triggered:number;status:string};
type State = {account:{mode?:string;managed:boolean;active:string;profiles:{id:string;label:string}[];logged_in:boolean;name?:string;switching:boolean};rooms:Room[];likes:Like[];notifications:{id:string;label:string;event:string;status:string}[]};
async function api(base:string,path:string,body?:unknown,method='POST'){
  const response=await fetch(base+'api/automation'+path,body===undefined?undefined:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const value=await response.json();
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));
  return value;
}

export function AutomationControls(){
  const {base}=useInstance();
  const [state,setState]=useState<State|null>(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [profile,setProfile]=useState('');
  const [counts,setCounts]=useState<Record<string,number>>({});
  useEffect(()=>{let alive=true;let timer:ReturnType<typeof setTimeout>;
    async function refresh(){try{const next=await api(base,'');if(alive)setState(next)}catch(e){if(alive)setError((e as Error).message)}
      finally{if(alive)timer=setTimeout(()=>void refresh(),2000)}}
    void refresh();return()=>{alive=false;clearTimeout(timer)}},[]);
  async function act(path:string,body:unknown,method='POST'){
    setBusy(true);setError('');try{await api(base,path,body,method);setState(await api(base,''))}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  if(!state?.rooms.length)return error?<p role="alert">自动化状态：{error}</p>:null;
  if(state.account.mode==='anonymous')return <section className="automation-panel" aria-label="匿名监控模式"><h2>匿名监控</h2><p>未登录 · 欢迎与点赞关闭 · 实时 ASR 和定期分析按房间配置运行</p></section>;
  return <section className="automation-panel" aria-label="直播自动化控制">
    <h2>自动欢迎与账号</h2>
    <p>下播只结束本房间采集，看板持续保留。自动欢迎开关不影响录制、通知或 AI 分析开关。</p>
    {state.account.managed&&<div className="automation-actions">
      <span>当前：{state.account.profiles.find(p=>p.id===state.account.active)?.label} · {state.account.logged_in?`已登录 ${state.account.name||''}`:'未登录，自动发送暂停'}</span>
      <select aria-label="账号资料" value={profile||state.account.active} disabled={busy||state.account.switching} onChange={e=>setProfile(e.target.value)}>
        {state.account.profiles.map(p=><option key={p.id} value={p.id}>{p.label}</option>)}
      </select>
      <button disabled={busy||state.account.switching||(profile||state.account.active)===state.account.active} onClick={()=>void act('/account',{profile:profile||state.account.active})}>切换账号</button>
      <button disabled={busy||state.account.switching} onClick={()=>void act('/account/login',{})}>打开登录窗口</button>
    </div>}
    <p className="footnote">切换账号会短暂停止两房间采集；各账号登录资料分别保留。</p>
    {state.rooms.map(room=>{const running=state.likes.find(x=>x.room_id===room.room_id&&x.status==='running');
      const latest=state.likes.filter(x=>x.room_id===room.room_id).at(-1);
      return <article key={room.room_id}>
        <h3>{room.label} <small>{room.confirmed==='live'?'已开播':room.confirmed==='offline'?'已下播':'待确认'}</small></h3>
        <div className="automation-actions">
          <button aria-pressed={room.enabled} disabled={busy} onClick={()=>void act('/rooms/'+room.room_id,{enabled:!room.enabled},'PUT')}>{room.enabled?'关闭自动欢迎':'开启自动欢迎'}</button>
          <label>点赞次数 <input aria-label={room.label+'点赞次数'} type="number" min="1" max="1000" value={counts[room.room_id]??100} onChange={e=>setCounts({...counts,[room.room_id]:Number(e.target.value)})}/></label>
          <button disabled={busy||!!running||!state.account.logged_in||state.account.switching||room.confirmed!=='live'} onClick={()=>void act('/rooms/'+room.room_id+'/likes',{count:counts[room.room_id]??100,request_id:crypto.randomUUID()})}>执行点赞</button>
          <CopyLikeScript roomId={room.room_id} count={counts[room.room_id]??100}/>
          {running&&<button disabled={busy} onClick={()=>void act('/likes/'+running.id+'/stop',{})}>停止点赞</button>}
        </div>
        <p>{room.intro}</p>
        <WelcomeCopy roomId={room.room_id}/>
        <p className="footnote">欢迎：{room.delivery}{room.error?' · '+room.error:''}。{latest&&`点赞：${latest.status}，前端触发 ${latest.triggered}/${latest.requested} 次（非服务器计入数）。`}</p>
      </article>})}
    {state.notifications.length>0&&<p className="footnote">最近通知：{state.notifications.slice(-4).map(n=>`${n.label} ${n.event==='live'?'开播':'下播'} · ${n.status}`).join('；')}</p>}
    {error&&<p role="alert">{error}</p>}
  </section>;
}
