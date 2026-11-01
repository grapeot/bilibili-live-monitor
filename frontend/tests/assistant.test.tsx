import React from 'react';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {Assistant} from '../src/Assistant';

const scope={room_id:'123456',session_id:'session',since:'2030-01-01T00:00:00Z',source_session:'generation'};
let card:Record<string,unknown>;
let requests:{url:string;body?:Record<string,unknown>}[];
beforeEach(()=>{
  card={id:'chat:1',kind:'chat',title:'想听歌曲A',status:'pending',version:1,author:'用户A',manual:false,
    evidence:[{id:'1',version:1}],pronunciation:{text:'yòng hù A',uncertain:true,override:false}};
  requests=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
    const body=init?.body?JSON.parse(String(init.body)):undefined;requests.push({url,body});
    if(url.endsWith('/sessions'))return {ok:true,json:async()=>[scope]};
    if(url.includes('/cards/')){card={...card,status:body.status||'pending',version:Number(card.version)+1,manual:true};return {ok:true,json:async()=>card}}
    if(url.endsWith('/pronunciation'))return {ok:true,json:async()=>({ok:true})};
    return {ok:true,json:async()=>({config:scope,cards:[card],evidence:[{id:'1',version:1,data:{text:'想听歌曲A',author:'用户A'}}],worker:{status:'live',cursor:1001,ai_cursor:2,pending_observations:3,last_observation:1893456000}})};
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
describe('助手操作',()=>{
  it('显示三栏、原名与读音，并用版本操作和恢复',async()=>{
    render(<Assistant/>);
    await screen.findByText('想听歌曲A');
    expect(screen.getByText('用户A')).toBeTruthy();
    expect(screen.getByText(/读音待确认/)).toBeTruthy();
    expect(screen.getByText('灯牌观众')).toBeTruthy();
    expect(screen.getByText('点歌单')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('想听歌曲A状态'),{target:{value:'responded'}});
    await waitFor(()=>expect(requests.some(r=>r.body?.expected_version===1&&r.body?.status==='responded')).toBe(true));
    await waitFor(()=>expect(screen.queryByText('想听歌曲A')).toBeNull());
    fireEvent.click(screen.getByLabelText('显示已完成 / 已忽略，供恢复或撤销'));
    await screen.findByText('想听歌曲A');
    fireEvent.click(screen.getByText('撤销上次状态'));
    await waitFor(()=>expect(requests.some(r=>r.url.endsWith('/undo')&&r.body?.expected_version===2)).toBe(true));
  });
  it('展开真实证据并保存读音覆盖',async()=>{
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    fireEvent.click(screen.getByText('证据与读音'));
    expect(screen.getByText(/证据 1/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('用户A读音覆盖'),{target:{value:'yòng hù A'}});
    fireEvent.click(screen.getByText('保存读音'));
    await waitFor(()=>expect(requests.some(r=>r.url.endsWith('/pronunciation')&&r.body?.reading==='yòng hù A')).toBe(true));
  });
  it('版本冲突保持错误可见并刷新',async()=>{
    const original=fetch;
    vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>url.includes('/cards/')?
      {ok:false,json:async()=>({detail:'卡片已经更新，请刷新后重试'})}:original(url,init)));
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    fireEvent.change(screen.getByLabelText('想听歌曲A状态'),{target:{value:'responded'}});
    expect(await screen.findByRole('alert')).toHaveProperty('textContent','卡片已经更新，请刷新后重试');
  });
  it('顶部显示真实待分析条数，异基数游标仅供诊断',async()=>{
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    expect(screen.getByText(/待分析观察 3 条 · 最近观察更新/)).toBeTruthy();
    expect(screen.queryByText(/读取进度/)).toBeNull();
    fireEvent.click(screen.getByText('运行诊断'));
    expect(screen.getByText(/源事件游标：1001；AI 已完成观察序号：2/)).toBeTruthy();
  });
  it('显示多音字候选与扩展汉字未知提示，原名保留',async()=>{
    card={...card,author:'重𠮷',pronunciation:{text:'重(zhòng / chóng) 𠮷',uncertain:true,override:false,unknown:['𠮷'],
      candidates:[{character:'重',readings:['zhòng','chóng']}]}};
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    expect(screen.getByText('重𠮷')).toBeTruthy();
    expect(screen.getByLabelText('昵称读音').textContent?.trim()).toBe('zhòng 𠮷 · 读音待确认 · 未知读音：𠮷');
    fireEvent.click(screen.getByText('证据与读音'));
    expect(screen.getByLabelText('完整读音').textContent).toBe('重(zhòng / chóng) 𠮷');
    expect(screen.getByText(/不代表读音已确认/)).toBeTruthy();
  });
  it('主行只显示每字首候选，详情保留全部候选且不改原数据',async()=>{
    const pronunciation={text:'余(yú / tú / xú) shēng 余(yú / tú / xú)',uncertain:true,override:false,
      candidates:[{character:'余',readings:['yú','tú','xú']}]};
    card={...card,author:'余生余',pronunciation};
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    const main=screen.getByLabelText('昵称读音');
    expect(main.textContent?.trim()).toBe('yú shēng yú · 读音待确认');
    expect(main.textContent).not.toMatch(/tú|xú|[()]/);
    const detail=screen.getByLabelText('完整读音');
    expect(detail.closest('details')?.open).toBe(false);
    fireEvent.click(screen.getByText('证据与读音'));
    expect(detail.closest('details')?.open).toBe(true);
    expect(detail.textContent).toBe(pronunciation.text);
    expect(pronunciation.candidates[0].readings).toEqual(['yú','tú','xú']);
  });
  it('人工覆盖读音不因候选列表而简化',async()=>{
    card={...card,pronunciation:{text:'余(yú / tú / xú)',uncertain:false,override:true,
      candidates:[{character:'余',readings:['yú','tú','xú']}]}};
    render(<Assistant/>);await screen.findByText('想听歌曲A');
    expect(screen.getByLabelText('昵称读音').textContent?.trim()).toBe('余(yú / tú / xú)');
  });
});
