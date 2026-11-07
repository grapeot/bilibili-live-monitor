import React from 'react';

export type SummaryRow={key:string|null;date:string;session:string;hours:number;status:string;has_result:boolean;
 metrics:Record<string,number|null>};
const columns:[string,string][]=[['online_mean','平均在线'],['online_peak','峰值在线'],['watched_last','累计看过'],
 ['followers_net','净关注'],['amount','全部礼物标价'],['amount_nonself','其他观众礼物标价'],['paid_donors_nonself','其他付费人数']];
export function SessionSummaryTable({rows,base,shared}:{rows:SummaryRow[];base:string;shared:boolean}){
 const show=(value:number|null|undefined,key:string)=>value==null?'—':
  key==='online_mean'?value.toFixed(2):key.startsWith('amount')?'¥'+value.toFixed(2):value.toLocaleString('zh-CN');
 return <section className="archive-panel" aria-label="场次汇总"><h2>场次汇总</h2>
  <p>{shared?'与远程主页共用同一份汇总数据。':'按与远程主页相同的场次口径计算；缺失信息显示为 —。'}按北京时间开播日期列出完整场次，跨午夜不拆分。</p>
  <div style={{overflowX:'auto'}}><table style={{width:'100%',whiteSpace:'nowrap',textAlign:'left'}}><thead><tr>
   <th>日期 · 场次</th><th>时长</th>{columns.map(([key,label])=><th key={key}>{label}</th>)}<th>状态</th>
  </tr></thead><tbody>{rows.map((row,index)=><tr key={row.key||row.date+row.session+index}>
   <td>{row.has_result&&row.key?<a href={base+'sessions/'+row.key+'/'}>{row.date} · {row.session}</a>:row.date+' · '+row.session}</td>
   <td>{row.hours==null?'—':row.hours.toFixed(2)+' 小时'}</td>{columns.map(([key])=><td key={key}>{show(row.metrics[key],key)}</td>)}
   <td>{row.status==='completed'?'已完成':row.status==='live'?'直播中':'整理中'}</td>
  </tr>)}</tbody></table></div>
  {!rows.length&&<p>暂无场次汇总。</p>}
 </section>;
}
