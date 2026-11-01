import assert from 'node:assert/strict';
import {test} from 'node:test';
import {giftLabel, giftText} from '../src/gifts.ts';

const base={event_type:'SEND_GIFT',gift_name:'合成礼物',num:1,coin_type:'gold'};

test('历史文本按结构化金额重新展示，原始对象不变',()=>{
  const gift={...base,total_coin:125};
  const before=JSON.stringify(gift);
  assert.equal(giftText(gift,'赠送 合成礼物 ×1 · 125 平台金币'),'赠送 合成礼物 ×1 · 1.25 电池');
  assert.equal(giftLabel(gift),'电池礼物');
  assert.equal(JSON.stringify(gift),before);
});

test('盲盒花费与结果单价分开，数量不误乘花费',()=>{
  const gift={...base,num:2,price:40000,total_coin:30000,
    blind_gift:{original_gift_price:15000}};
  assert.equal(giftText(gift,'旧文本'),
    '盲盒结果：合成礼物 ×2 · 盲盒花费 300 电池 · 抽中礼物单价 400 电池 · 原盲盒单价 150 电池');
});

test('缺失金额不显示零，显式零保留，银币不换算',()=>{
  assert.equal(giftText({...base,blind_gift:{}},'旧文本'),'盲盒结果：合成礼物 ×1');
  assert.equal(giftText({...base,total_coin:0},'旧文本'),'赠送 合成礼物 ×1 · 0 电池');
  assert.equal(giftText({...base,coin_type:'silver',total_coin:100},'旧文本'),
    '赠送 合成礼物 ×1 · 100 银币');
});

test('上舰不再乘数量，SC换算且保留正文',()=>{
  assert.equal(giftText({...base,event_type:'GUARD_BUY',guard_level:3,num:2,guard_price_gold:198000},'旧文本'),
    '购买 合成礼物 ×2（大航海等级 3） · 平台报告金额 1980 电池');
  assert.equal(giftText({event_type:'SUPER_CHAT_MESSAGE',price_yuan:30},'醒目留言 30 元：合成内容'),
    '醒目留言 300 电池：合成内容');
});
