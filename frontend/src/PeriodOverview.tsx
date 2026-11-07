import React from 'react';
import './period_overview.css';

export type GiftShare={label:string;percentage:number|null};
export type PeriodSummary={days:number;start:string;end:string;observed_days:number;
 entries_daily_mean:number|null;gift_amount_yuan:number|null;gift_amount_unknown:number;gift_share?:GiftShare};
const percentage=(value:number|null)=>value==null?'—':value.toFixed(2)+'%';

export function PeriodOverview({periods,giftShare}:{periods:PeriodSummary[];giftShare?:GiftShare}){
 return <div className="period-overview">
  {periods.map(item=><section className="period-card" key={item.days} data-period-days={item.days}>
   <h2>最近 {item.days} 天</h2><div className="period-values">
    <div><span>日均来访人次</span><strong>{item.entries_daily_mean==null?'—':item.entries_daily_mean.toLocaleString('zh-CN',{maximumFractionDigits:1,minimumFractionDigits:1})}</strong></div>
    <div><span>礼物总金额</span><strong>{item.gift_amount_yuan==null?'—':'¥'+item.gift_amount_yuan.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}{item.gift_amount_unknown&&item.gift_amount_yuan!=null?'+':''}</strong></div>
    {item.gift_share&&<div><span>{item.gift_share.label}送礼占比</span><strong>{percentage(item.gift_share.percentage)}</strong></div>}
   </div><small>有记录 {item.observed_days} 天 · 含今天</small>
  </section>)}
  {giftShare&&<p className="period-total-share">{giftShare.label}总体送礼占比 <strong>{percentage(giftShare.percentage)}</strong></p>}
 </div>;
}
