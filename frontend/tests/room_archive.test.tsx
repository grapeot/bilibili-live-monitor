import React from 'react';
import {afterEach,expect,test,vi} from 'vitest';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {RoomArchive} from '../src/RoomArchive';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});
test('按日期筛选场次、保留跨日场次且不把累计看过汇成日独立人数',async()=>{
 const day=(date:string,keys:string[])=>({date,session_keys:keys,observed_seconds:3600,coverage_seconds:1800,
  danmaku:3,entries:4,gift_count:2,gift_amount_yuan:3,gift_count_unknown:0,gift_amount_unknown:0,followers_change:null,followers_partial:true,has_observations:true,
  watched_sessions:keys.map(key=>({key,start:'2026-01-01T15:00:00Z',value:22,observed_at:'2026-01-01T15:59:00Z'}))});
 const summary={gift_amount_yuan:3,gift_count:2,followers_change:1,watched:22};
 const value={name:'测试主播',mode:'anonymous',days:[day('2026-01-02',['a','b']),day('2026-01-01',['a'])],sessions:[
  {key:'a',start:'2026-01-01T15:00:00Z',end:'2026-01-01T17:00:00Z',status:'completed',has_result:true,summary,dates:['2026-01-01','2026-01-02']},
  {key:'b',start:'2026-01-02T02:00:00Z',end:'2026-01-02T03:00:00Z',status:'live',has_result:true,summary,dates:['2026-01-02']}],warnings:0,updated_at:'2026-01-02T03:00:00Z'};
 const fetch=vi.fn(async()=>({ok:true,json:async()=>value}));vi.stubGlobal('fetch',fetch);
 render(<RoomArchive base="/instances/guest/" roomId="1"/>);
  await screen.findByText('测试主播');
 expect(screen.getByText('近期数据概览 · 北京时间')).toBeTruthy();
 expect(screen.getAllByText('进房次数')).toHaveLength(1);
 expect(screen.getByText('累计看过（分场）')).toBeTruthy();
 expect(screen.getAllByText('22')).toHaveLength(2);
 expect(screen.getAllByText('查看最终结果 →')).toHaveLength(1);
 expect(screen.getByText('查看本场实时结果 →').getAttribute('href')).toBe('/instances/guest/sessions/b/');
 fireEvent.change(screen.getByLabelText('选择日期'),{target:{value:'2026-01-01'}});
 expect(screen.queryByText('查看本场实时结果 →')).toBeNull();
 expect(screen.getByText('跨日场次 · 本页汇总仅计入所选日期')).toBeTruthy();
 expect(fetch.mock.calls[0][0]).toBe('/instances/guest/api/rooms/1/archive');
});
