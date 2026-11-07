import React from 'react';
import {PeriodOverview,type PeriodSummary,type GiftShare} from './PeriodOverview';
import {SessionOverviewChart} from './SessionOverviewChart';
import {SessionSummaryTable,type SummaryRow} from './SessionSummaryTable';

export type ArchiveOverviewData={periods?:PeriodSummary[];gift_share?:GiftShare};
export function ArchiveOverview({data,rows,base=''}:{data:ArchiveOverviewData;rows:SummaryRow[];base?:string}){
 return <>
  <PeriodOverview periods={data.periods||[]} giftShare={data.gift_share}/>
  <SessionOverviewChart rows={rows}/>
  <SessionSummaryTable rows={rows} base={base}/>
 </>;
}
