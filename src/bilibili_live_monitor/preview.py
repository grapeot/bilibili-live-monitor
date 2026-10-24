"""显式启用后按需抓缩略图；关闭时不创建采样任务。"""
import asyncio
import base64
import time

FRAME = '''()=>{const v=document.querySelector('video');
if(!v||v.paused||v.readyState<2)return null;
const c=document.createElement('canvas');c.width=320;c.height=180;
const x=c.getContext('2d');x.fillStyle='#101c22';x.fillRect(0,0,320,180);
const s=Math.min(320/v.videoWidth,180/v.videoHeight),w=v.videoWidth*s,h=v.videoHeight*s;
x.drawImage(v,(320-w)/2,(180-h)/2,w,h);return c.toDataURL('image/jpeg',.65).split(',')[1]}'''


class Previews:
    def __init__(self, get_page):
        self.get_page = get_page
        self.cache = {}
        self.locks = {}

    async def frame(self, room_id):
        page = self.get_page(room_id)
        if page is None or page.is_closed():
            raise KeyError(room_id)
        async with self.locks.setdefault(room_id, asyncio.Lock()):
            at, image = self.cache.get(room_id, (0, None))
            if time.monotonic() - at > 1 / 3:
                raw = await asyncio.wait_for(page.evaluate(FRAME), 3)
                if not raw:
                    raise RuntimeError('播放器暂无画面')
                image = base64.b64decode(raw)
                if self.get_page(room_id) is not page:
                    raise KeyError(room_id)
                self.cache[room_id] = (time.monotonic(), image)
            return image

    async def stop(self, room_id):
        self.cache.pop(room_id, None)
        self.locks.pop(room_id, None)

    async def close(self):
        self.cache.clear()
        self.locks.clear()
