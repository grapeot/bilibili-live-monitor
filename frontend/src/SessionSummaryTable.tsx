import React from 'react';
import './session_summary.css';

export type SummaryRow={key:string|null;date:string;session:string;hours:number;status:string;has_result:boolean;
 detail_url?:string|null;
 metrics:Record<string,number|null>};
const columns:[string,string][]=[['online_mean','平均在线'],['online_peak','峰值在线'],['watched_last','累计看过'],
 ['followers_net','净关注'],['amount','全部礼物标价'],['amount_nonself','其他观众礼物标价'],['paid_donors_nonself','其他付费人数']];
export function SessionSummaryTable({rows,base}:{rows:SummaryRow[];base:string;shared:boolean}){
 const show=(value:number|null|undefined,key:string)=>value==null?'—':
  key==='online_mean'?value.toFixed(2):key.startsWith('amount')?'¥'+value.toFixed(2):value.toLocaleString('zh-CN');
 return <section className="archive-panel session-summary" aria-label="场次汇总"><h2>场次汇总</h2>
  <p>按开播日期查看完整场次，跨天直播不拆分。暂无记录的指标显示为 —。</p>
  <div className="session-summary-scroll" tabIndex={0} aria-label="场次汇总表，可横向滚动"><table><thead><tr>
   <th>日期 · 场次</th><th>时长</th>{columns.map(([key,label])=><th key={key}>{label}</th>)}<th>状态</th>
  </tr></thead><tbody>{rows.map((row,index)=><tr key={row.key||row.date+row.session+index}>
   <td>{row.has_result&&row.key?<a href={base+'sessions/'+row.key+'/'}>{row.date} · {row.session}</a>:row.detail_url?
    <a href={row.detail_url} target="_blank" rel="noreferrer">{row.date} · {row.session} ↗</a>:row.date+' · '+row.session}</td>
   <td>{row.hours==null?'—':row.hours.toFixed(2)+' 小时'}</td>{columns.map(([key])=><td key={key}>{show(row.metrics[key],key)}</td>)}
   <td>{row.status==='completed'?'已完成':row.status==='live'?'直播中':'整理中'}</td>
  </tr>)}</tbody></table></div>
  {!rows.length&&<p>暂无场次汇总。</p>}
 </section>;
}
