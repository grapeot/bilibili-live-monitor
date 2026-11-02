"""复用已验证网页点赞组件；统计前端触发，不声称服务端计入。"""
import asyncio
import random

LIKE_STEP = r'''async () => {
  const vm = document.querySelector('.chat-control-panel')?.__vue__;
  if (!vm?.handleLikeBtnClick || !vm.loginStatus?.isLogin || !vm.isShowLikeBtn || vm._isDestroyed)
    throw new Error('点赞组件不可用或账号未登录');
  const delay = Math.max(450, Number(vm.likeBtnClickFrequencyLimit || .5)*1000+100);
  if(vm.likeBtnClickIsCoolingDown) return {triggered:false, delay};
  await vm.handleLikeBtnClick(true);
  if(!vm.likeBtnClickIsCoolingDown) throw new Error('点赞未通过网页检查');
  return {triggered:true, delay};
}'''


async def trigger_likes(page, room_id, count, allowed, progress):
    from .welcome import correct_page
    triggered = 0
    while triggered < count:
        if not allowed() or not correct_page(page.url, room_id):
            raise RuntimeError('房间、账号或开播状态变化，点赞已停止')
        result = await page.evaluate(LIKE_STEP)
        if result['triggered']:
            triggered += 1
            progress(triggered)
        await asyncio.sleep(float(result['delay'])/1000 + random.uniform(0, .45))
    return triggered
