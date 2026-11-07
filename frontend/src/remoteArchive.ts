// 部署时注入已发布的房间主页地址；未配置的房间不展示入口。
export function remoteArchiveUrl(roomId:string):string|null{
 try{
  const configured=JSON.parse(import.meta.env.VITE_ROOM_ARCHIVE_URLS||'{}');
  const value=configured?.[roomId];
  if(typeof value!=='string')return null;
  const url=new URL(value);
  return url.protocol==='https:'&&!url.username&&!url.password?url.href:null;
 }catch{return null;}
}
