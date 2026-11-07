import React from 'react';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it} from 'vitest';
import {SessionOverviewChart} from '../src/SessionOverviewChart';
import type {SummaryRow} from '../src/SessionSummaryTable';

afterEach(cleanup);
const row=(day:string,online:number|null,amount:number|null):SummaryRow=>({key:day,date:'2026-01-'+day,session:'晚间场',hours:2,
 status:'completed',has_result:true,metrics:{online_mean:online,amount}});
it('独立双轴按开播顺序显示平均在线柱形和每场总金额折线',()=>{
 const {container}=render(<SessionOverviewChart rows={[row('03',20,2000),row('02',10,1000)]}/>);
 expect([...container.querySelectorAll('.overview-bar')].map(n=>n.getAttribute('data-value'))).toEqual(['10','20']);
 expect([...container.querySelectorAll('.overview-dot')].map(n=>n.getAttribute('data-value'))).toEqual(['1000','2000']);
 expect(screen.getByLabelText('左轴：平均在线人数')).toBeTruthy();
 expect(screen.getByLabelText('右轴：每场礼物总金额，元')).toBeTruthy();
 fireEvent.focus(screen.getByRole('img',{name:/2026-01-02/}));
 expect(screen.getByRole('status').textContent).toContain('礼物总金额 ¥1,000.00');
});
it('缺失金额使折线断开，零值保留，单点与空数据不生成无效坐标',()=>{
 const {container,rerender}=render(<SessionOverviewChart rows={[row('03',0,0),row('02',null,null),row('01',10,100)]}/>);
 expect(container.querySelectorAll('.overview-bar')).toHaveLength(2);
 expect(container.querySelectorAll('.overview-dot')).toHaveLength(2);
 const path=container.querySelector('.overview-line')?.getAttribute('d')||'';
 expect(path.match(/M/g)).toHaveLength(2);expect(path).not.toContain('L');expect(path).not.toMatch(/NaN|Infinity/);
 rerender(<SessionOverviewChart rows={[row('01',0,0)]}/>);
 expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
 rerender(<SessionOverviewChart rows={[]}/>);expect(screen.getByText('暂无场次数据。')).toBeTruthy();
});
