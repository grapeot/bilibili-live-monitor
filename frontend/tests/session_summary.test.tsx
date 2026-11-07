import React from 'react';
import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it} from 'vitest';
import {SessionSummaryTable} from '../src/SessionSummaryTable';

afterEach(cleanup);
it('展示共享汇总全部指标，并使用本地场次入口',()=>{
 render(<SessionSummaryTable base="/instances/guest/" shared rows={[{key:'test',date:'2026-01-01',session:'晚间场',
  hours:2,status:'completed',has_result:true,metrics:{online_mean:12.345,online_peak:30,watched_last:1234,
   followers_net:2,amount:10,amount_nonself:8,paid_donors_nonself:3}}]}/>);
 expect(screen.getByText('12.35')).toBeTruthy();expect(screen.getByText('1,234')).toBeTruthy();
 expect(screen.getByText('¥8.00')).toBeTruthy();expect(screen.getByText(/与远程主页共用/)).toBeTruthy();
 expect(screen.getByRole('link').getAttribute('href')).toBe('/instances/guest/sessions/test/');
});
it('缺少数据不渲染零，也不为仅存历史汇总生成失效链接',()=>{
 render(<SessionSummaryTable base="/instances/guest/" shared={false} rows={[{key:null,date:'2026-01-01',session:'晚间场',
  hours:2,status:'completed',has_result:false,metrics:{online_mean:null,online_peak:0,watched_last:null}}]}/>);
 expect(screen.getAllByText('—')).toHaveLength(6);expect(screen.getByText('0')).toBeTruthy();
 expect(screen.queryByRole('link')).toBeNull();
});
it('没有本地详情的历史场次可打开配置来源的原详情',()=>{
 render(<SessionSummaryTable base="/instances/guest/" shared rows={[{key:null,date:'2026-01-01',session:'早场',
  hours:2,status:'completed',has_result:false,detail_url:'https://archive.example.com/s/history/',metrics:{}}]}/>);
 expect(screen.getByRole('link').getAttribute('href')).toBe('https://archive.example.com/s/history/');
 expect(screen.getByRole('link').getAttribute('rel')).toBe('noreferrer');
});
