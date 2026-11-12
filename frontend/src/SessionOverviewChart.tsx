import React,{useEffect,useLayoutEffect,useRef,useState} from 'react';
import type {SummaryRow} from './SessionSummaryTable';
import './session_overview_chart.css';

const valid=(value:number|null|undefined):value is number=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
const ceiling=(values:(number|null|undefined)[])=>{
 const max=Math.max(1,...values.filter(valid));
 const unit=10**Math.floor(Math.log10(max));return Math.ceil(max/unit)*unit;
};
const amount=(value:number|null|undefined)=>valid(value)?'¥'+value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const online=(value:number|null|undefined)=>valid(value)?value.toFixed(2):'—';
const tick=(value:number)=>value>=10000?(value/10000).toLocaleString('zh-CN',{maximumFractionDigits:1})+'万':value.toLocaleString('zh-CN',{maximumFractionDigits:1});

export function SessionOverviewChart({rows}:{rows:SummaryRow[]}){
 const container=useRef<HTMLDivElement>(null);
 const [available,setAvailable]=useState(600),[active,setActive]=useState<number|null>(null);
 useEffect(()=>{const node=container.current;if(!node)return;
  const measure=()=>setAvailable(node.clientWidth||600);measure();
  if(typeof ResizeObserver==='undefined')return;
  const observer=new ResizeObserver(measure);observer.observe(node);return()=>observer.disconnect();
 },[rows.length>0]);
 const points=[...rows].reverse();
 const width=Math.max(available,points.length*76),height=290,top=16,bottom=222;
 // 默认停在最右侧的最近场次；用户向左查看更早场次后，宽度变化或数据刷新不再把视图拉回。
 const pinned=useRef(true),latest=points.length?points[points.length-1].key||points[points.length-1].date+points[points.length-1].session:'';
 useLayoutEffect(()=>{pinned.current=true;},[latest,points.length]);
 useLayoutEffect(()=>{const node=container.current;if(node&&pinned.current)node.scrollLeft=node.scrollWidth;},[latest,points.length,width,available]);
 const follow=()=>{const node=container.current;if(node)pinned.current=node.scrollLeft+node.clientWidth>=node.scrollWidth-2;};
 const leftMax=ceiling(points.map(row=>row.metrics.online_mean)),rightMax=ceiling(points.map(row=>row.metrics.amount));
 const x=(index:number)=>(index+.5)*width/Math.max(1,points.length);
 const y=(value:number,max:number)=>bottom-value/max*(bottom-top);
 let connected=false;
 const line=points.map((row,index)=>{const value=row.metrics.amount;if(!valid(value)){connected=false;return '';}
  const command=connected?'L':'M';connected=true;return `${command}${x(index)},${y(value,rightMax)}`;
 }).join(' ');
 const selected=active==null?null:points[active];
 return <section className="archive-panel session-overview-chart" aria-label="平均在线与礼物金额趋势">
  <div className="overview-chart-heading"><h2>场次趋势</h2><div className="overview-chart-legend">
   <span className="overview-online">▰ 平均在线人数 · 左轴</span><span className="overview-gifts">━● 每场礼物总金额 · 右轴</span>
  </div></div>
  {!points.length?<p>暂无场次数据。</p>:<>
   <div className="overview-chart-units"><span>平均在线（人）</span><span>礼物金额（元）</span></div>
   <div className="overview-chart-grid">
    <svg className="overview-axis" viewBox={`0 0 52 ${height}`} preserveAspectRatio="none" aria-label="左轴：平均在线人数">
     {[0,1,2,3,4].map(i=><text key={i} x="46" y={y(leftMax*i/4,leftMax)+4} textAnchor="end">{tick(leftMax*i/4)}</text>)}
    </svg>
    <div className="overview-chart-scroll" ref={container} tabIndex={0} onScroll={follow} aria-label="按开播顺序排列的场次趋势，默认显示最近场次，可向左滚动查看更早场次">
     <svg width={width} height={height} style={{width,height}} role="group" aria-label="柱形为平均在线，折线为每场礼物总金额">
      {[0,1,2,3,4].map(i=><line className="overview-gridline" key={i} x1="0" x2={width} y1={y(leftMax*i/4,leftMax)} y2={y(leftMax*i/4,leftMax)}/>)}
      {points.map((row,index)=>valid(row.metrics.online_mean)&&<rect key={index} className={'overview-bar'+(active===index?' active':'')}
       data-value={row.metrics.online_mean} x={x(index)-17} y={y(row.metrics.online_mean,leftMax)} width="34" height={bottom-y(row.metrics.online_mean,leftMax)} rx="3"/>)}
      <path className="overview-line" d={line}/>
      {points.map((row,index)=>valid(row.metrics.amount)&&<circle key={index} className="overview-dot" data-value={row.metrics.amount}
       cx={x(index)} cy={y(row.metrics.amount,rightMax)} r={active===index?5:3.5}/>)}
      {points.map((row,index)=><g key={row.key||row.date+row.session+index} tabIndex={0} role="img"
       aria-label={`${row.date} ${row.session}，平均在线 ${online(row.metrics.online_mean)} 人，礼物总金额 ${amount(row.metrics.amount)}`}
       onMouseEnter={()=>setActive(index)} onFocus={()=>setActive(index)} onClick={()=>setActive(index)}>
       <title>{row.date} · {row.session}{'\n'}平均在线 {online(row.metrics.online_mean)} 人{'\n'}礼物总金额 {amount(row.metrics.amount)}</title>
       <rect className="overview-hit" x={x(index)-width/Math.max(1,points.length)/2} y={top} width={width/Math.max(1,points.length)} height={bottom-top}/>
       <text className="overview-date" x={x(index)} y="246" textAnchor="middle">{row.date.slice(5)}</text>
       <text className="overview-session" x={x(index)} y="266" textAnchor="middle">{row.session}</text>
      </g>)}
     </svg>
    </div>
    <svg className="overview-axis overview-right-axis" viewBox={`0 0 64 ${height}`} preserveAspectRatio="none" aria-label="右轴：每场礼物总金额，元">
     {[0,1,2,3,4].map(i=><text key={i} x="7" y={y(rightMax*i/4,rightMax)+4}>{tick(rightMax*i/4)}</text>)}
    </svg>
   </div>
   <output className="overview-chart-detail" aria-live="polite">{selected?
    `${selected.date} · ${selected.session} ｜ 平均在线 ${online(selected.metrics.online_mean)} 人 ｜ 礼物总金额 ${amount(selected.metrics.amount)}`:
    '默认显示最近场次，向左滚动查看更早场次；悬停、点击或用键盘选中可查看数值。缺失数据不记为零。'}</output>
  </>}
 </section>;
}
