import React from 'react';
import './room_links.css';

export function RoomLinks({liveUrl,archiveUrl}:{liveUrl:string;archiveUrl?:string}){
 return <nav className="room-links" aria-label="房间链接">
  <a href={liveUrl} target="_blank" rel="noreferrer">直播间 ↗</a>
  {archiveUrl&&<a href={archiveUrl}>按日汇总 →</a>}
 </nav>;
}
