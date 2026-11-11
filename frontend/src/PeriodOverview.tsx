import React from 'react';
import './period_overview.css';

export type GiftShare={label:string;percentage:number|null};
export type PeriodSummary={days:number;start:string;end:string;observed_days:number;
 entries_daily_mean:number|null;gift_amount_yuan:number|null;gift_amount_unknown:number;gift_share?:GiftShare;
 fans_club_latest?:number|null;fans_club_latest_date?:string|null;fans_club_daily_mean?:number|null;
 fans_club_change?:number|null;fans_club_change_since?:string|null};
const percentage=(value:number|null)=>value==null?'—':value.toFixed(2)+'%';
const signed=(value:number|null|undefined)=>value==null?'—':(value>0?'+':'')+value.toLocaleString('zh-CN');
// 跨年时保留年份，避免很久以前的基线只显示月日。
const monthDay=(date:string|null|undefined,end:string)=>!date?'':date.slice(0,4)===end.slice(0,4)?date.slice(5):date;

export function PeriodOverview({periods,giftShare}:{periods:PeriodSummary[];giftShare?:GiftShare}){
 return <div className="period-overview">
  {periods.map(item=><section className="period-card" key={item.days} data-period-days={item.days}>
   <h2>最近 {item.days} 天</h2><div className="period-values">
    <div><span>日均来访人次</span><strong>{item.entries_daily_mean==null?'—':item.entries_daily_mean.toLocaleString('zh-CN',{maximumFractionDigits:1,minimumFractionDigits:1})}</strong></div>
    <div><span>礼物总金额</span><strong>{item.gift_amount_yuan==null?'—':'¥'+item.gift_amount_yuan.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}{item.gift_amount_unknown&&item.gift_amount_yuan!=null?'+':''}</strong></div>
    {'fans_club_daily_mean' in item&&<div><span>粉丝团日均</span><strong>{item.fans_club_daily_mean==null?'—':item.fans_club_daily_mean.toLocaleString('zh-CN',{maximumFractionDigits:1,minimumFractionDigits:1})}</strong>
     {item.fans_club_latest!=null&&<em className="period-detail">最新 {item.fans_club_latest.toLocaleString('zh-CN')} · {monthDay(item.fans_club_latest_date,item.end)}</em>}</div>}
    {'fans_club_change' in item&&<div><span>粉丝团变化</span><strong>{signed(item.fans_club_change)}</strong>
     {item.fans_club_change_since&&<em className="period-detail">较 {monthDay(item.fans_club_change_since,item.end)}</em>}</div>}
    {item.gift_share&&<div><span>{item.gift_share.label}送礼占比</span><strong>{percentage(item.gift_share.percentage)}</strong></div>}
   </div><small>有记录 {item.observed_days} 天 · 含今天</small>
  </section>)}
  {giftShare&&<p className="period-total-share">{giftShare.label}总体送礼占比 <strong>{percentage(giftShare.percentage)}</strong></p>}
 </div>;
}
