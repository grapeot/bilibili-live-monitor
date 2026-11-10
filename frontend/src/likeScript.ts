/** 只生成文本；由用户在自己的已登录直播间控制台执行。 */
export function likeScript(roomId:string,count:number):string{
  if(!/^\d+$/.test(roomId)||!Number.isInteger(count)||count<1||count>1000)
    throw new Error('点赞次数须为 1–1000 的整数');
  return `(async () => {
  const roomId = ${JSON.stringify(roomId)};
  const target = ${count};
  const correctRoom = () => location.protocol === 'https:' &&
    location.hostname === 'live.bilibili.com' &&
    location.pathname.replace(/\\/$/, '') === '/' + roomId;
  if (!correctRoom()) throw new Error('请在 https://live.bilibili.com/' + roomId + ' 的控制台执行');
  const vm = document.querySelector('.chat-control-panel')?.__vue__;
  if (!vm?.handleLikeBtnClick || !vm.loginStatus?.isLogin || !vm.isShowLikeBtn || vm._isDestroyed)
    throw new Error('点赞组件不可用或账号未登录');
  window.biliLikeTask?.stop();
  const task = { count: 0, stopped: false, stop() { this.stopped = true; } };
  window.biliLikeTask = task;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  console.log('开始点赞。停止：biliLikeTask.stop()；进度：biliLikeTask.count');
  try {
    while (!task.stopped && task.count < target) {
      if (!correctRoom() || vm._isDestroyed || !vm.loginStatus?.isLogin || !vm.isShowLikeBtn ||
          document.querySelector('.chat-control-panel')?.__vue__ !== vm)
        throw new Error('页面、登录状态或点赞入口发生变化，已停止');
      const frequency = Number(vm.likeBtnClickFrequencyLimit || 0.5);
      if (!Number.isFinite(frequency) || frequency < 0) throw new Error('点赞冷却配置无效');
      const interval = Math.max(450, frequency * 1000 + 100);
      if (!vm.likeBtnClickIsCoolingDown) {
        await vm.handleLikeBtnClick(true);
        if (!vm.likeBtnClickIsCoolingDown) throw new Error('点赞未通过网页检查');
        task.count++;
        if (task.count % 100 === 0) console.log('本轮已触发 ' + task.count + '/' + target + ' 次点赞');
      }
      if (!task.stopped && task.count < target) await sleep(interval + Math.random() * 450);
    }
  } catch (error) {
    console.error('点赞中止：', error);
  } finally {
    task.stopped = true;
    console.log('结束：本轮共触发 ' + task.count + ' 次点赞（非服务器计入数）。');
  }
})();`;
}
