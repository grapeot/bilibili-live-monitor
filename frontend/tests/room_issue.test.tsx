import React from 'react';
import {afterEach, expect, test} from 'vitest';
import {cleanup, render, screen} from '@testing-library/react';
import {RoomIssue} from '../src/RoomIssue';

afterEach(cleanup);

test('停止后的错误保留在默认收起的历史信息中，不作为当前告警',()=>{
  render(<RoomIssue status="stopped" error="TimeoutError: synthetic" updatedAt="2030-01-01T00:00:00Z"/>);
  expect(screen.queryByRole('alert')).toBeNull();
  const details=screen.getByText('历史采集信息（已停止，非当前故障）').closest('details');
  expect(details?.hasAttribute('open')).toBe(false);
  expect(details?.textContent).toContain('TimeoutError: synthetic');
  expect(details?.textContent).toContain('该记录最后更新');
});

test('当前运行错误仍直接显示为告警',()=>{
  render(<RoomIssue status="error" error="当前连接失败"/>);
  expect(screen.getByRole('alert').textContent).toBe('当前连接失败');
  expect(screen.queryByText('历史采集信息（已停止，非当前故障）')).toBeNull();
});

test('没有错误时不制造历史提示',()=>{
  const {container}=render(<RoomIssue status="stopped" error={null}/>);
  expect(container.textContent).toBe('');
});
