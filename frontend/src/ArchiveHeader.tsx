import React from 'react';
import type {SummaryRow} from './SessionSummaryTable';
import './archive_page.css';

export type ArchiveLink={label:string;href:string;external?:boolean};
export function ArchiveHeader({title,links,rows=[]}:{title:string;links:ArchiveLink[];rows?:SummaryRow[]}){
 const live=rows.filter(row=>row.status==='live').length;
 const complete=rows.filter(row=>row.status==='completed').length;
 const pending=rows.length-live-complete;
 return <header className="archive-header">
  <div><p className="archive-eyebrow">近期数据概览 · 北京时间</p><h1>{title}</h1>
   <p>最近 7 天与 30 天的来访、礼物和每场直播。</p>
   {!!rows.length&&<p className="archive-session-count">
    {!!complete&&<span>{complete} 场已完成</span>}
    {!!live&&<span className="archive-status live">{live} 场直播中</span>}
    {!!pending&&<span>{pending} 场整理中</span>}
   </p>}
  </div>
  <nav className="archive-navigation" aria-label="页面导航">
   {links.map(link=><a key={link.href} id={link.label==='观众备忘'?'audience-entry':undefined} href={link.href} target={link.external?'_blank':undefined}
    rel={link.external?'noopener noreferrer':undefined}>{link.label}</a>)}
  </nav>
 </header>;
}
