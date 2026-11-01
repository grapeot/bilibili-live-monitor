export type Gift = {
  event_type:string; gift_name?:string; num?:number; coin_type?:string|null;
  price?:number|null; total_coin?:number|null; guard_price_gold?:number|null;
  price_yuan?:number|null; guard_level?:number|null; time_source?:string;
  blind_gift?:{original_gift_price?:unknown}|null;
};

const amount = (value:unknown):value is number =>
  typeof value==='number'&&Number.isSafeInteger(value)&&value>=0;
const batteries = (gold:number) => (gold/100).toFixed(2).replace(/\.00$/,'').replace(/(\.\d)0$/,'$1')+' 电池';

export function giftLabel(gift?:Gift|null){
  return gift?.event_type==='GUARD_BUY'?'上舰 · 大航海':
    gift?.event_type==='SUPER_CHAT_MESSAGE'?'醒目留言':gift?.blind_gift?'盲盒结果':
    gift?.coin_type==='silver'?'银币礼物':gift?.coin_type==='gold'?'电池礼物':'礼物';
}

export function giftText(gift:Gift|null|undefined, original:string){
  if(!gift)return original;
  if(gift.event_type==='SUPER_CHAT_MESSAGE'){
    if(!amount(gift.price_yuan))return original;
    const message=original.replace(/^醒目留言 \d+ 元[：:]\s?/, '');
    return `醒目留言 ${gift.price_yuan*10} 电池：${message}`;
  }
  if(!gift.gift_name||!amount(gift.num)||gift.num<1)return original;
  if(gift.event_type==='GUARD_BUY'){
    return `购买 ${gift.gift_name} ×${gift.num}（大航海等级 ${gift.guard_level}）`+
      (amount(gift.guard_price_gold)?` · 平台报告金额 ${batteries(gift.guard_price_gold)}`:'');
  }
  const blind=gift.blind_gift;
  let text=`${blind?'盲盒结果：':'赠送 '}${gift.gift_name} ×${gift.num}`;
  if(gift.coin_type==='gold'){
    if(amount(gift.total_coin))text+=` · ${blind?'盲盒花费 ':''}${batteries(gift.total_coin)}`;
    if(blind&&amount(gift.price))text+=` · 抽中礼物单价 ${batteries(gift.price)}`;
    if(blind&&amount(blind.original_gift_price))text+=` · 原盲盒单价 ${batteries(blind.original_gift_price)}`;
  }else if(gift.coin_type==='silver'&&amount(gift.total_coin)){
    text+=` · ${gift.total_coin} 银币`;
  }
  return text;
}
