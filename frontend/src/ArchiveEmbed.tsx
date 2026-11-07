import React from 'react';
import {createRoot} from 'react-dom/client';
import {PeriodOverview,type PeriodSummary,type GiftShare} from './PeriodOverview';
import {SessionOverviewChart} from './SessionOverviewChart';
import type {SummaryRow} from './SessionSummaryTable';

type ArchiveData={sessions:SummaryRow[];periods?:PeriodSummary[];gift_share?:GiftShare};
const target=document.getElementById('archive-overview');
if(target){
 const root=createRoot(target);
 const render=(data:ArchiveData)=>root.render(<>
  <PeriodOverview periods={data.periods||[]} giftShare={data.gift_share}/>
  <SessionOverviewChart rows={data.sessions.map(row=>({...row,key:null,has_result:false}))}/>
 </>);
 render(JSON.parse(document.getElementById('archive-data')!.textContent!));
 document.addEventListener('archive:update',event=>render((event as CustomEvent<ArchiveData>).detail));
}
