import React from 'react';

export function RoomIssue({status,error,updatedAt}:{status:string;error:string|null;updatedAt?:string}){
  if(!error)return null;
  if(status!=='stopped')return <p className="room-error" role="alert">{error}</p>;
  return <details className="historical-room-issue">
    <summary>历史采集信息（已停止，非当前故障）</summary>
    {updatedAt&&<p>该记录最后更新：{new Date(updatedAt).toLocaleString('zh-CN',{hour12:false})}</p>}
    <p>{error}</p>
  </details>;
}
