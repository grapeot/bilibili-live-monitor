import React from 'react';
import {afterEach,expect,it} from 'vitest';
import {cleanup,render,screen} from '@testing-library/react';
import {PeriodOverview} from '../src/PeriodOverview';

afterEach(cleanup);
it('展示滚动窗口、实际天数、零值和可选占比，缺失不填零',()=>{
 render(<PeriodOverview periods={[
  {days:7,start:'2026-01-02',end:'2026-01-08',observed_days:2,entries_daily_mean:0,gift_amount_yuan:12,gift_amount_unknown:0,gift_share:{label:'测试观众',percentage:25}},
  {days:30,start:'2025-12-10',end:'2026-01-08',observed_days:0,entries_daily_mean:null,gift_amount_yuan:null,gift_amount_unknown:0}
 ]} giftShare={{label:'测试观众',percentage:50}}/>);
 expect(screen.getByText('最近 7 天')).toBeTruthy();
 expect(screen.getByText('最近 30 天')).toBeTruthy();
 expect(screen.getByText('有记录 2 天 · 含今天')).toBeTruthy();
 expect(screen.getByText('0.0')).toBeTruthy();
 expect(screen.getByText('¥12.00')).toBeTruthy();
 expect(screen.getAllByText('—')).toHaveLength(2);
 expect(screen.getByText('25.00%')).toBeTruthy();
 expect(screen.getByText('50.00%')).toBeTruthy();
});

it('默认不展示指定观众占比',()=>{
 render(<PeriodOverview periods={[]}/>);
 expect(screen.queryByText(/占比/)).toBeNull();
});

it('粉丝团按天收盘：日均、最新与较基线日期的变化',()=>{
 render(<PeriodOverview periods={[
  {days:7,start:'2026-01-04',end:'2026-01-10',observed_days:3,entries_daily_mean:1,gift_amount_yuan:0,gift_amount_unknown:0,
   fans_club_latest:60,fans_club_latest_date:'2026-01-10',fans_club_daily_mean:65,fans_club_change:-20,fans_club_change_since:'2026-01-03'},
  {days:30,start:'2025-12-12',end:'2026-01-10',observed_days:1,entries_daily_mean:1,gift_amount_yuan:0,gift_amount_unknown:0,
   fans_club_latest:null,fans_club_latest_date:null,fans_club_daily_mean:null,fans_club_change:null,fans_club_change_since:null}
 ]}/>);
 expect(screen.getAllByText('粉丝团日均')).toHaveLength(2);
 expect(screen.getByText('65.0')).toBeTruthy();
 expect(screen.getByText('最新 60 · 01-10')).toBeTruthy();
 expect(screen.getByText('-20')).toBeTruthy();
 expect(screen.getByText('较 01-03')).toBeTruthy();
});

it('旧数据没有粉丝团字段时不显示该项',()=>{
 render(<PeriodOverview periods={[{days:7,start:'2026-01-04',end:'2026-01-10',observed_days:1,entries_daily_mean:1,gift_amount_yuan:0,gift_amount_unknown:0}]}/>);
 expect(screen.queryByText(/粉丝团/)).toBeNull();
});

it('跨年基线保留年份',()=>{
 render(<PeriodOverview periods={[{days:30,start:'2025-12-12',end:'2026-01-10',observed_days:2,entries_daily_mean:1,gift_amount_yuan:0,gift_amount_unknown:0,
  fans_club_latest:60,fans_club_latest_date:'2026-01-10',fans_club_daily_mean:60,fans_club_change:5,fans_club_change_since:'2025-12-20'}]}/>);
 expect(screen.getByText('较 2025-12-20')).toBeTruthy();
 expect(screen.getByText('最新 60 · 01-10')).toBeTruthy();
});
