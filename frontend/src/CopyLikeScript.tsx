import React, {useEffect,useState} from 'react';
import {likeScript} from './likeScript';

export function CopyLikeScript({roomId,count}:{roomId:string;count:number}){
  const [message,setMessage]=useState('');
  const [manual,setManual]=useState('');
  useEffect(()=>{setManual('');setMessage('')},[roomId,count]);
  const valid=Number.isInteger(count)&&count>=1&&count<=1000;
  async function copy(){
    const script=likeScript(roomId,count);
    setManual('');setMessage('');
    try{
      await navigator.clipboard.writeText(script);
      setMessage(`已复制 ${count} 次点赞代码。在已登录的目标直播间控制台执行；停止：biliLikeTask.stop()。`);
    }catch{
      setManual(script);
      setMessage('自动复制失败，请手动复制下方代码，在已登录的目标直播间控制台执行。停止：biliLikeTask.stop()。');
    }
  }
  return <>
    <button disabled={!valid} onClick={()=>void copy()}>拷贝 JavaScript 代码</button>
    {message&&<p role="status">{message}</p>}
    {manual&&<textarea aria-label="点赞 JavaScript 代码" readOnly value={manual} rows={8} style={{width:'100%'}} onFocus={e=>e.currentTarget.select()}/>}
  </>;
}
