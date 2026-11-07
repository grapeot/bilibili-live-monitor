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
