import React from 'react';
import {afterEach,expect,test,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {WelcomeCopy} from '../src/WelcomeCopy';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});
const fixture=()=>({revision:4,greeting:'欢迎{names}',intro:'介绍',welcome_variants:[{id:'a',info:'旧信息',cta:'点歌'}],copy_values:{}});

test('草稿不会随重渲染丢失，保存带修订号并展示生效结果',async()=>{
  const fetch=vi.fn(async(_url:unknown,init?:RequestInit)=>({ok:true,json:async()=>init?{...JSON.parse(String(init.body)),revision:5}:fixture()}));
  vi.stubGlobal('fetch',fetch);
  const view=render(<WelcomeCopy roomId="123456"/>);
  fireEvent.click(screen.getByText('编辑话术'));
  fireEvent.change(await screen.findByLabelText('话术组1主播介绍'),{target:{value:'新信息'}});
  view.rerender(<WelcomeCopy roomId="123456"/>);
  expect((screen.getByLabelText('话术组1主播介绍') as HTMLTextAreaElement).value).toBe('新信息');
  fireEvent.click(screen.getByText('关闭话术编辑'));
  fireEvent.click(screen.getByText('编辑话术'));
  expect((screen.getByLabelText('话术组1主播介绍') as HTMLTextAreaElement).value).toBe('新信息');
  fireEvent.click(screen.getByText('保存设置'));
  await screen.findByText('已保存，下一轮欢迎自动生效，无需重启');
  const call=fetch.mock.calls.find(([,init])=>init?.method==='PUT');
  expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({revision:4,welcome_variants:[{id:'a',info:'新信息',cta:'点歌'}]});
});

test('保存冲突保留草稿，允许重新载入',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(_url:unknown,init?:RequestInit)=>({ok:!init,json:async()=>init?{detail:'文案已被其他操作更新'}:fixture()})));
  render(<WelcomeCopy roomId="123456"/>);
  fireEvent.click(screen.getByText('编辑话术'));
  fireEvent.change(await screen.findByLabelText('话术组1主播介绍'),{target:{value:'我的草稿'}});
  fireEvent.click(screen.getByText('保存设置'));
  await screen.findByRole('alert');
  expect((screen.getByLabelText('话术组1主播介绍') as HTMLTextAreaElement).value).toBe('我的草稿');
  fireEvent.click(screen.getByText('重新载入'));
  await waitFor(()=>expect((screen.getByLabelText('话术组1主播介绍') as HTMLTextAreaElement).value).toBe('旧信息'));
});
