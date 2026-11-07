import React from 'react';
import {act,cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ArchivePage,type PublishedArchive} from '../src/ArchivePage';

afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()});
const initial:PublishedArchive={sessions:[{date:'2026-01-01',session:'早场',url:'/s/history/',hours:2,
 metrics:{online_mean:10,online_peak:20,amount:100,amount_nonself:75,followers_net:2}}],
 periods:[{days:7,start:'2025-12-26',end:'2026-01-01',observed_days:1,entries_daily_mean:40,gift_amount_yuan:100,gift_amount_unknown:0}],
 gift_share:{label:'测试观众',percentage:25}};
const settings={title:'测试直播归档',links:[{label:'歌曲搜索',href:'songs/'},{label:'观众备忘',href:'audience/'}]};
const response=(value:unknown)=>({ok:true,json:async()=>value});

it('同一React页面渲染导航、历史链接、完整表格和双轴图',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>response(initial)));
 render(<ArchivePage initial={initial} settings={settings}/>);
 await act(async()=>{});
 expect(screen.getByRole('navigation',{name:'页面导航'}).querySelectorAll('a')).toHaveLength(2);
 expect(screen.getByRole('link',{name:'歌曲搜索'}).getAttribute('href')).toBe('songs/');
 expect(screen.getByRole('link',{name:/2026-01-01/}).getAttribute('href')).toBe('/s/history/');
 expect(screen.getByRole('table')).toBeTruthy();
 expect(screen.getByLabelText('右轴：每场礼物总金额，元')).toBeTruthy();
 expect(screen.getByText('+2')).toBeTruthy();
});

it('定时刷新同时更新卡片与历史表，保留已有导航',async()=>{
 vi.useFakeTimers();
 const fresh={...initial,periods:[{...initial.periods![0],entries_daily_mean:99}],
  sessions:[{...initial.sessions[0],url:'/s/new/',date:'2026-01-02'},...initial.sessions]};
 const fetch=vi.fn().mockResolvedValueOnce(response(initial)).mockResolvedValue(response(fresh));
 vi.stubGlobal('fetch',fetch);
 render(<ArchivePage initial={initial} settings={settings}/>);
 await act(async()=>{});
 expect(fetch).toHaveBeenCalledTimes(1);
 await act(async()=>{await vi.advanceTimersByTimeAsync(60000)});
 expect(screen.getByText('99.0')).toBeTruthy();
 expect(screen.getByRole('link',{name:/2026-01-02/}).getAttribute('href')).toBe('/s/new/');
 expect(screen.getByRole('link',{name:'观众备忘'})).toBeTruthy();
});

it('刷新失败保留内容，重试成功后清除提示',async()=>{
 vi.useFakeTimers();
 const fetch=vi.fn().mockResolvedValueOnce(response(initial)).mockResolvedValueOnce(response({broken:true}))
  .mockResolvedValue(response(initial));
 vi.stubGlobal('fetch',fetch);
 render(<ArchivePage initial={initial} settings={settings}/>);
 await act(async()=>{});
 await act(async()=>{await vi.advanceTimersByTimeAsync(60000)});
 expect(screen.getByRole('alert').textContent).toContain('已有内容保留');
 expect(screen.getByRole('link',{name:/2026-01-01/})).toBeTruthy();
 await act(async()=>{await vi.advanceTimersByTimeAsync(60000)});
 expect(screen.queryByRole('alert')).toBeNull();
});

it('慢请求不重叠，卸载时取消请求与定时器',async()=>{
 vi.useFakeTimers();
 const fetch=vi.fn((_url:string,options:RequestInit)=>new Promise((_resolve,reject)=>{
  options.signal?.addEventListener('abort',()=>reject(new Error('abort')));
 }));
 vi.stubGlobal('fetch',fetch);
 const view=render(<ArchivePage initial={initial} settings={settings}/>);
 await act(async()=>{await vi.advanceTimersByTimeAsync(5000)});
 expect(fetch).toHaveBeenCalledTimes(1);
 view.unmount();
 await act(async()=>{await vi.advanceTimersByTimeAsync(120000)});
 expect(fetch).toHaveBeenCalledTimes(1);
});
