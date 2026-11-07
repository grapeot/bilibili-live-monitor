import React from 'react';
import {afterEach,expect,test} from 'vitest';
import {cleanup,render,screen} from '@testing-library/react';
import {RoomLinks} from '../src/RoomLinks';

afterEach(cleanup);
test('卡片同时提供直播间和本站按日汇总入口',()=>{
 render(<RoomLinks liveUrl="https://live.bilibili.com/123456" archiveUrl="/instances/example/rooms/123456/"/>);
 expect(screen.getByRole('link',{name:'直播间 ↗'}).getAttribute('href')).toBe('https://live.bilibili.com/123456');
 expect(screen.getByRole('link',{name:'按日汇总 →'}).getAttribute('href')).toBe('/instances/example/rooms/123456/');
});
test('未配置归档的实例不生成失效入口',()=>{
 render(<RoomLinks liveUrl="https://live.bilibili.com/123456"/>);
 expect(screen.queryByRole('link',{name:'按日汇总 →'})).toBeNull();
});
