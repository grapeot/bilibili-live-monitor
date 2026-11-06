import React from 'react';
import {afterEach, expect, test, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {AutomationControls} from '../src/Automation';
import {InstanceProvider} from '../src/Instance';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});

function fixture(logged=true){
  return {account:{managed:true,active:'small',profiles:[{id:'small',label:'小号'},{id:'main',label:'大号'}],logged_in:logged,switching:false},
    rooms:[{room_id:'123456',label:'合成主播',intro:'合成文案',enabled:true,confirmed:'live',delivery:'idle'}],likes:[],notifications:[]};
}

test('开关使用本房间API，不改变AI设置',async()=>{
  const state=fixture();
  const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>{
    if(init?.method==='PUT')state.rooms[0].enabled=false;
    return {ok:true,json:async()=>state};
  });vi.stubGlobal('fetch',fetch);
  render(<AutomationControls/>);
  fireEvent.click(await screen.findByText('关闭自动欢迎'));
  await screen.findByText('开启自动欢迎');
  expect(fetch.mock.calls.some(([url,init])=>String(url).endsWith('/rooms/123456')&&init?.method==='PUT'&&init.body==='{"enabled":false}')).toBe(true);
  expect(fetch.mock.calls.some(([url])=>String(url).includes('/assistant/'))).toBe(false);
});

test('未登录不允许点赞，账号可选后显式切换',async()=>{
  const fetch=vi.fn(async()=>({ok:true,json:async()=>fixture(false)}));vi.stubGlobal('fetch',fetch);
  render(<AutomationControls/>);
  await screen.findByText('合成文案');
  expect((screen.getByText('执行点赞') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('账号资料'),{target:{value:'main'}});
  fireEvent.click(screen.getByText('切换账号'));
  await waitFor(()=>expect(fetch.mock.calls.length).toBeGreaterThan(1));
});

test('匿名实例无发言和账号操作，状态请求使用自身实例',async()=>{
  const state={...fixture(false),account:{...fixture(false).account,mode:'anonymous'}};
  const fetch=vi.fn(async()=>({ok:true,json:async()=>state}));vi.stubGlobal('fetch',fetch);
  render(<InstanceProvider instance={{id:'guest',label:'匿名',mode:'anonymous',base:'/instances/guest/'}}><AutomationControls/></InstanceProvider>);
  await screen.findByText('匿名监控');
  expect(screen.queryByText('执行点赞')).toBeNull();
  expect(screen.queryByText('开启自动欢迎')).toBeNull();
  expect(screen.queryByText('关闭自动欢迎')).toBeNull();
  expect(screen.queryByText('打开登录窗口')).toBeNull();
  expect(fetch.mock.calls[0][0]).toBe('/instances/guest/api/automation');
});
