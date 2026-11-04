import React, {useState} from 'react';

type Variant={id:string;info:string;cta:string};
type Copy={revision:number;greeting:string;intro:string;welcome_variants:Variant[];copy_values:Record<string,string>};

export function WelcomeCopy({roomId}:{roomId:string}){
  const [open,setOpen]=useState(false);
  const [draft,setDraft]=useState<Copy|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [message,setMessage]=useState('');
  async function request(value?:Copy):Promise<Copy>{
    const response=await fetch(import.meta.env.BASE_URL+'api/automation/rooms/'+roomId+'/copy',value===undefined?undefined:{
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
    const body=await response.json();
    if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));
    return body;
  }
  async function load(){
    setBusy(true);setError('');setMessage('');
    try{setDraft(await request());setOpen(true)}catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function save(){
    if(!draft)return;
    setBusy(true);setError('');setMessage('');
    try{setDraft(await request(draft));setMessage('已保存，下一轮欢迎自动生效，无需重启')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  function change(index:number,key:'info'|'cta',value:string){
    if(draft)setDraft({...draft,welcome_variants:draft.welcome_variants.map((v,i)=>i===index?{...v,[key]:value}:v)});
    setMessage('');
  }
  return <div className="welcome-copy">
    <button className="secondary" disabled={busy} onClick={()=>open?setOpen(false):draft?setOpen(true):void load()}>{open?'关闭话术编辑':'编辑话术'}</button>
    {open&&draft&&<div className="welcome-copy-editor">
      <label>点名欢迎<textarea aria-label="点名欢迎" value={draft.greeting} disabled={busy}
        onChange={e=>{setDraft({...draft,greeting:e.target.value});setMessage('')}}/></label>
      <small>保留 {'{names}'} 作为观众名字，{'{suffix}'} 可选。</small>
      <label>直播间介绍<textarea aria-label="直播间介绍" value={draft.intro} disabled={busy}
        onChange={e=>{setDraft({...draft,intro:e.target.value});setMessage('')}}/></label>
      {draft.welcome_variants.map((v,i)=><fieldset key={v.id}>
        <legend>话术组 {i+1}</legend>
        <label>主播介绍<textarea aria-label={`话术组${i+1}主播介绍`} value={v.info} disabled={busy} onChange={e=>change(i,'info',e.target.value)}/></label>
        <label>点歌引导<textarea aria-label={`话术组${i+1}点歌引导`} value={v.cta} disabled={busy} onChange={e=>change(i,'cta',e.target.value)}/></label>
        <button className="secondary" disabled={busy} onClick={()=>{setDraft({...draft,welcome_variants:draft.welcome_variants.filter((_,j)=>i!==j)});setMessage('')}}>删除本组</button>
      </fieldset>)}
      <div className="automation-actions">
        <button className="secondary" disabled={busy||draft.welcome_variants.length>=20} onClick={()=>{
          setDraft({...draft,welcome_variants:[...draft.welcome_variants,{id:crypto.randomUUID(),info:'',cta:''}]});setMessage('')}}>新增一组</button>
        <button disabled={busy} onClick={()=>void save()}>保存设置</button>
        <button className="secondary" disabled={busy} onClick={()=>void load()}>重新载入</button>
      </div>
      {draft.welcome_variants.length===0&&<p>未设置随机组时，使用直播间介绍。</p>}
    </div>}
    {message&&<p role="status">{message}</p>}{error&&<p role="alert">{error}</p>}
  </div>;
}
