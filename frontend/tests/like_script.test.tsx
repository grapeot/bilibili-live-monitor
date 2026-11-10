import React from 'react';
import {afterEach,expect,test,vi} from 'vitest';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {CopyLikeScript} from '../src/CopyLikeScript';
import {likeScript} from '../src/likeScript';

afterEach(()=>{cleanup();vi.unstubAllGlobals()});

function run(room='123456',stop=false){
  const vm={loginStatus:{isLogin:true},isShowLikeBtn:true,_isDestroyed:false,
    likeBtnClickFrequencyLimit:.35,likeBtnClickIsCoolingDown:false,
    handleLikeBtnClick:vi.fn(async()=>{vm.likeBtnClickIsCoolingDown=true;if(stop)win.biliLikeTask.stop()})};
  const previous={stop:vi.fn()};
  const win={biliLikeTask:previous as {stop:()=>void;count?:number;stopped?:boolean}};
  const doc={querySelector:()=>({__vue__:vm})};
  const log={log:vi.fn(),error:vi.fn()};
  const location={protocol:'https:',hostname:'live.bilibili.com',pathname:'/'+room};
  const timer=vi.fn((resolve:()=>void)=>{vm.likeBtnClickIsCoolingDown=false;resolve()});
  const execute=()=>new Function('window','document','location','console','setTimeout',
    'return '+likeScript('123456',3))(win,doc,location,log,timer);
  return {execute,vm,win,previous,log,timer,location,doc};
}

test('生成代码实际执行指定次数，沿用网页冷却及随机等待',async()=>{
  const f=run();await f.execute();
  expect(f.vm.handleLikeBtnClick).toHaveBeenCalledTimes(3);
  expect(f.previous.stop).toHaveBeenCalledOnce();
  expect(f.win.biliLikeTask.count).toBe(3);
  expect(f.win.biliLikeTask.stopped).toBe(true);
  expect(f.timer).toHaveBeenCalledTimes(2);
  for(const call of f.timer.mock.calls)expect((call as unknown as [unknown,number])[1]).toBeGreaterThanOrEqual(450);
});

test('错误房间零触发，且不打断已有任务',async()=>{
  const f=run('654321');await expect(f.execute()).rejects.toThrow('123456');
  expect(f.vm.handleLikeBtnClick).not.toHaveBeenCalled();
  expect(f.previous.stop).not.toHaveBeenCalled();
});

test('停止命令终止后续触发',async()=>{
  const f=run('123456',true);await f.execute();
  expect(f.vm.handleLikeBtnClick).toHaveBeenCalledOnce();
  expect(f.win.biliLikeTask.count).toBe(1);
});

test('未登录不会触发点赞',async()=>{
  const f=run();f.vm.loginStatus.isLogin=false;
  await expect(f.execute()).rejects.toThrow('未登录');
  expect(f.vm.handleLikeBtnClick).not.toHaveBeenCalled();
});

test.each([{hostname:'example.com'},{protocol:'http:'}])('错误域名或协议零触发 %s',async change=>{
  const f=run();Object.assign(f.location,change);
  await expect(f.execute()).rejects.toThrow('123456');
  expect(f.vm.handleLikeBtnClick).not.toHaveBeenCalled();
});

test.each(['navigation','logout','replacement'])('运行中%s停止后续触发',async change=>{
  const f=run();f.vm.handleLikeBtnClick.mockImplementation(async()=>{
    f.vm.likeBtnClickIsCoolingDown=true;
    if(change==='navigation')f.location.pathname='/654321';
    if(change==='logout')f.vm.loginStatus.isLogin=false;
    if(change==='replacement')f.doc.querySelector=()=>({__vue__:{...f.vm}});
  });
  await f.execute();expect(f.vm.handleLikeBtnClick).toHaveBeenCalledOnce();
  expect(f.log.error).toHaveBeenCalled();
});

test.each([0,1.5,1001,NaN,Infinity])('拒绝无效次数 %s',count=>{
  expect(()=>likeScript('123456',count)).toThrow();
});

test('拒绝房间代码注入',()=>expect(()=>likeScript('123456;alert(1)',2)).toThrow());

test('复制使用当前房间次数，不请求自动化接口',async()=>{
  const writeText=vi.fn(async()=>{});vi.stubGlobal('navigator',{clipboard:{writeText}});
  const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
  const {rerender}=render(<CopyLikeScript roomId="123456" count={2}/>);
  rerender(<CopyLikeScript roomId="654321" count={7}/>);
  fireEvent.click(screen.getByText('拷贝 JavaScript 代码'));
  await screen.findByText(/已复制 7 次/);
  expect(writeText).toHaveBeenCalledWith(likeScript('654321',7));
  expect(fetch).not.toHaveBeenCalled();
});

test('剪贴板拒绝时展示可手动复制的完整代码',async()=>{
  vi.stubGlobal('navigator',{clipboard:{writeText:vi.fn(async()=>{throw new Error('denied')})}});
  render(<CopyLikeScript roomId="123456" count={5}/>);
  fireEvent.click(screen.getByText('拷贝 JavaScript 代码'));
  const area=await screen.findByLabelText('点赞 JavaScript 代码') as HTMLTextAreaElement;
  expect(area.value).toBe(likeScript('123456',5));
});

test('缺少剪贴板API时可手动复制，次数修改后清除旧代码',async()=>{
  vi.stubGlobal('navigator',{});
  const {rerender}=render(<CopyLikeScript roomId="123456" count={5}/>);
  fireEvent.click(screen.getByText('拷贝 JavaScript 代码'));
  expect((await screen.findByLabelText('点赞 JavaScript 代码') as HTMLTextAreaElement).value).toBe(likeScript('123456',5));
  rerender(<CopyLikeScript roomId="123456" count={9}/>);
  expect(screen.queryByLabelText('点赞 JavaScript 代码')).toBeNull();
});
