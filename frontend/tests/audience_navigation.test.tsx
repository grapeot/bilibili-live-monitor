import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {afterEach, expect, test, vi} from 'vitest';
import {waitFor} from '@testing-library/react';

let observer:MutationObserver|undefined;
afterEach(()=>{observer?.disconnect();document.body.innerHTML='';vi.unstubAllGlobals()});

test.each([true,false])('实时main替换后恢复唯一观众入口，目录请求成功=%s',async success=>{
  const NativeObserver=MutationObserver;
  const TrackingObserver=class extends NativeObserver{
    constructor(callback:MutationCallback){super(callback);observer=this}
  };
  vi.stubGlobal('fetch',vi.fn(async()=>{
    if(!success)throw new Error('offline');
    return {ok:true,json:async()=>({people:[]})};
  }));
  const markup='<main><nav><a href="/songs/">歌曲搜索</a></nav></main>';
  document.body.innerHTML=markup;
  const source=readFileSync(resolve(process.cwd(),'../src/bilibili_live_monitor/audience_navigation.py'),'utf8');
  const script=source.split("NAVIGATION = r'''")[1].split("'''")[0]
    .replace(/^<script[^>]*>/,'').replace(/<\/script>$/,'')
    .replace('__URL__',JSON.stringify('https://example.com/audience/'));
  new Function('MutationObserver',script)(TrackingObserver);
  expect(document.querySelectorAll('#audience-entry')).toHaveLength(1);
  document.querySelector('main')!.outerHTML=markup;
  await waitFor(()=>expect(document.querySelectorAll('#audience-entry')).toHaveLength(1));
  expect(document.querySelector('#audience-entry')!.getAttribute('href')).toBe('https://example.com/audience/');
  document.querySelector('main')!.append(document.createElement('p'));
  await new Promise(resolve=>setTimeout(resolve,0));
  expect(document.querySelectorAll('#audience-entry')).toHaveLength(1);
});
