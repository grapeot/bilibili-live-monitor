import React from 'react';
import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
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
it('默认滚到最右侧最近场次；向左查看后刷新或变宽不拉回；停在最右时变窄仍贴最右；新场次回到最右',()=>{
 const left=new WeakMap<Element,number>();let client=400,resize=()=>{};
 const names=['scrollLeft','scrollWidth','clientWidth'];
 const own=names.map(name=>[name,Object.getOwnPropertyDescriptor(HTMLElement.prototype,name)] as const);
 const observer=globalThis.ResizeObserver;
 globalThis.ResizeObserver=class{constructor(callback:()=>void){resize=callback;}observe(){}disconnect(){}} as unknown as typeof ResizeObserver;
 Object.defineProperty(HTMLElement.prototype,'scrollWidth',{configurable:true,get(){return Number((this.firstElementChild as SVGElement|null)?.getAttribute('width')??0);}});
 Object.defineProperty(HTMLElement.prototype,'clientWidth',{configurable:true,get:()=>client});
 Object.defineProperty(HTMLElement.prototype,'scrollLeft',{configurable:true,get(){return left.get(this)??0;},
  set(value:number){left.set(this,Math.max(0,Math.min(value,this.scrollWidth-this.clientWidth)));}});
 const atEnd=(n:HTMLElement)=>n.scrollLeft+n.clientWidth>=n.scrollWidth;
 try{
  const rows=Array.from({length:20},(_,i)=>row(String(20-i).padStart(2,'0'),10,100));
  const {container,rerender}=render(<SessionOverviewChart rows={rows}/>);
  const scroll=container.querySelector('.overview-chart-scroll') as HTMLElement;
  expect(atEnd(scroll)).toBe(true);
  client=300;act(()=>resize());expect(atEnd(scroll)).toBe(true);
  scroll.scrollLeft=100;fireEvent.scroll(scroll);
  rerender(<SessionOverviewChart rows={[...rows]}/>);expect(scroll.scrollLeft).toBe(100);
  client=350;act(()=>resize());expect(scroll.scrollLeft).toBe(100);
  rerender(<SessionOverviewChart rows={[row('21',30,3000),...rows]}/>);expect(atEnd(scroll)).toBe(true);
 }finally{
  globalThis.ResizeObserver=observer;
  for(const [name,descriptor] of own){if(descriptor)Object.defineProperty(HTMLElement.prototype,name,descriptor);else delete (HTMLElement.prototype as unknown as Record<string,unknown>)[name];}
 }
});
