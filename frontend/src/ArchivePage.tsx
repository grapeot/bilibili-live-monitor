import React from 'react';
import {ArchiveOverview,type ArchiveOverviewData} from './ArchiveOverview';
import {ArchiveHeader,type ArchiveLink} from './ArchiveHeader';
import type {SummaryRow} from './SessionSummaryTable';
import {useArchiveData} from './useArchiveData';
import './room_archive.css';

export type PublishedSession=Omit<SummaryRow,'key'|'has_result'|'status'>&{url:string;status?:string;start_utc?:string;updated_at?:string};
export type PublishedArchive=ArchiveOverviewData&{sessions:PublishedSession[]};
export type ArchivePageSettings={title:string;links:ArchiveLink[];refresh_url?:string};
const validArchive=(value:unknown)=>!!value&&typeof value==='object'&&Array.isArray((value as PublishedArchive).sessions);

export function ArchivePage({initial,settings}:{initial:PublishedArchive;settings:ArchivePageSettings}){
 const {data,error}=useArchiveData(settings.refresh_url||'sessions.json',60000,initial,validArchive);
 const current=data||initial;
 const rows:SummaryRow[]=current.sessions.map(row=>({...row,key:null,has_result:false,status:row.status||'completed'}));
 return <main className="room-archive published-archive">
  <ArchiveHeader title={settings.title} links={settings.links} rows={rows}/>
  {error&&<p role="alert">{error}；已有内容保留，稍后自动重试。</p>}
  <ArchiveOverview data={current} rows={rows}/>
 </main>;
}
