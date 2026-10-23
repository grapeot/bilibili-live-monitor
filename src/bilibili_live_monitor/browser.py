import asyncio
import base64
import re

from playwright.async_api import async_playwright

from .danmaku import messages


def room_url(value: str):
    value = value.strip()
    match = re.fullmatch(r'(?:https://live\.bilibili\.com/)?([1-9]\d{0,15})/?(?:\?[^#]*)?', value)
    if not match:
        raise ValueError('请输入有效的 B 站直播间 HTTPS 链接或正整数房间号')
    return f'https://live.bilibili.com/{match[1]}'


# 从元素创建起静音，避免等待播放器加载期间漏出声音。
MUTE_SCRIPT = '''(() => {
    const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'muted');
    Object.defineProperty(HTMLMediaElement.prototype, 'muted', {
        get() {return desc.get.call(this)}, set(_) {desc.set.call(this, true)}
    });
    const mute = () => document.querySelectorAll('video,audio').forEach(v => {v.muted=true});
    new MutationObserver(mute).observe(document, {childList:true, subtree:true});
    document.addEventListener('play', mute, true);
    document.addEventListener('volumechange', e => {if (!e.target.muted) e.target.muted=true}, true);
    // 已观察到后台页面会自行暂停；只在本工具正在录制时恢复播放。
    document.addEventListener('pause', e => {
        const capture=window.__pcmMonitor || window.__monitor;
        if(capture && e.target instanceof HTMLMediaElement && !e.target.ended) {
            capture.pauses=(capture.pauses || 0)+1;
            e.target.play().then(()=>{capture.playError=null})
                .catch(error=>{capture.playError=String(error)});
        }
    }, true);
})();'''

START_RECORDER = '''() => {
    const v = document.querySelector('video'); v.muted = true;
    const tracks = v.captureStream().getAudioTracks();
    if (!tracks.length) throw new Error('播放器没有音轨');
    const recorder = new MediaRecorder(new MediaStream(tracks), {mimeType:'audio/webm;codecs=opus'});
    window.__monitor = {recorder, chunks:[], started:Date.now(), error:null};
    recorder.ondataavailable = e => {if(e.data.size) window.__monitor.chunks.push(e.data)};
    recorder.onerror = e => {window.__monitor.error = String(e.error)};
    recorder.start();
    return window.__monitor.started;
}'''

READ_RECORDER = '''async (seconds) => {
    const m = window.__monitor;
    if(!m || m.error) throw new Error(m?.error || '录音器已丢失');
    await new Promise(resolve => setTimeout(resolve, seconds * 1000));
    await new Promise((resolve,reject) => {
        const timer = setTimeout(() => reject(new Error('录音器输出超时')),5000);
        m.recorder.addEventListener('dataavailable', () => {clearTimeout(timer);resolve()}, {once:true});
        m.recorder.requestData();
    });
    const data = await new Promise(resolve => {
        const r = new FileReader(); r.onload=()=>resolve(r.result.split(',')[1]);
        r.readAsDataURL(new Blob(m.chunks,{type:'audio/webm;codecs=opus'}));
    });
    const v=document.querySelector('video');
    return {data, ended:Date.now(), muted:v.muted, paused:v.paused,
        track_live:m.recorder.stream.getAudioTracks().every(t=>t.readyState==='live')};
}'''


async def decode_webm(data: bytes):
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
        '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        audio, error = await asyncio.wait_for(process.communicate(data), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or not audio:
        raise RuntimeError(f'ffmpeg解码失败: {error.decode(errors="replace")[:500]}')
    return audio


async def ensure_playing(page):
    await asyncio.wait_for(page.evaluate(
        '''async()=>{const v=document.querySelector("video");
        if(!v)throw new Error('播放器不存在');v.muted=true;await v.play()}'''), 10)


async def close_page(page):
    if page.is_closed():
        return
    try:
        await asyncio.wait_for(page.close(), 5)
    except Exception:
        # 页面关闭失败时尝试直接关闭目标；资源名额清理仍由调用者finally保证。
        session = await asyncio.wait_for(page.context.new_cdp_session(page), 3)
        try:
            await asyncio.wait_for(session.send('Page.close'), 3)
        finally:
            await session.detach()


class Browser:
    def __init__(self, cdp_url):
        self.cdp_url = cdp_url
        self.pw = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def connect(self):
        async with self.lock:
            return await self._connect()

    async def _connect(self):
        if self.browser is None or not self.browser.is_connected():
            if self.pw:
                await self.pw.stop()
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.connect_over_cdp(self.cdp_url, timeout=15000)
        return self.browser.contexts[0]

    async def page(self, url, on_danmaku=None):
        ctx = await self.connect()
        page = await ctx.new_page()
        if on_danmaku:
            def websocket(socket):
                if '.chat.bilibili.com/' in socket.url:
                    socket.on('framereceived', lambda frame: [on_danmaku(m) for m in messages(frame)])
            page.on('websocket', websocket)
        try:
            await page.add_init_script(MUTE_SCRIPT)
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_function(
                'document.querySelector("video")?.readyState >= 2', timeout=30000)
            await ensure_playing(page)
            return page
        except BaseException:
            try:
                await close_page(page)
            except Exception:
                pass
            raise

    async def discover(self, limit):
        ctx = await self.connect()
        page = await ctx.new_page()
        try:
            await page.add_init_script(MUTE_SCRIPT)
            await page.goto('https://live.bilibili.com/', wait_until='domcontentloaded')
            await page.wait_for_function('document.querySelectorAll("a[href]").length>30')
            links = await page.evaluate('''() => [...document.querySelectorAll('a[href]')]
                .map(a=>({url:a.href,title:a.innerText.trim().slice(0,120)}))''')
            result, seen = [], set()
            for link in links:
                try:
                    url = room_url(link['url'])
                except ValueError:
                    continue
                if url not in seen:
                    seen.add(url)
                    result.append({**link, 'url': url})
            return result[:limit]
        finally:
            await close_page(page)

    async def close(self):
        # 停止 Playwright 连接，不关闭用户的 Chrome 实例。
        if self.pw:
            await self.pw.stop()


def audio_bytes(payload):
    return base64.b64decode(payload['data'])
