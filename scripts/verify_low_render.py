"""在现有CDP实例创建独立合成页，验证低渲染、自动播放和验证码检测边界。"""
import argparse
import asyncio

from bilibili_live_monitor.browser import Browser
from bilibili_live_monitor.monitor import PAGE_INSPECTION_SCRIPT


async def verify(url):
    browser = Browser(url)
    context = await browser.connect()
    page = await context.new_page()
    try:
        await page.set_content('''<video></video><div class="geetest_panel"
            style="width:100px;height:50px;visibility:visible;opacity:1">synthetic</div>''')
        # 不拉媒体流；用受控paused属性检查真实DOM事件与MutationObserver行为。
        setup = '''v=>{v.testPaused=false;Object.defineProperty(v,'paused',{get:()=>v.testPaused});
            v.pause=()=>{v.testPaused=true}}'''
        await page.evaluate(f"({setup})(document.querySelector('video'))")
        untouched = await page.evaluate(PAGE_INSPECTION_SCRIPT, False)
        assert untouched['attention'] == 'verification_or_login'
        assert await page.evaluate("!window.__liveMonitorPauseGuard && !document.querySelector('video').paused")
        result = await page.evaluate(PAGE_INSPECTION_SCRIPT, True)
        assert result['attention'] == 'verification_or_login'
        assert await page.evaluate("document.querySelector('video').paused")
        assert await page.evaluate("getComputedStyle(document.documentElement).opacity==='0'")
        assert await page.evaluate("getComputedStyle(document.querySelector('.geetest_panel')).visibility==='visible'")
        await page.evaluate('''()=>{const v=document.querySelector('video');v.testPaused=false;
            v.dispatchEvent(new Event('play'))}''')
        assert await page.evaluate("document.querySelector('video').paused")
        await page.evaluate(f"()=>{{const v=document.createElement('video');({setup})(v);document.body.append(v)}}")
        await page.wait_for_function("[...document.querySelectorAll('video')].every(v=>v.paused)")
        await page.evaluate(PAGE_INSPECTION_SCRIPT, True)
        assert await page.locator('#live-monitor-low-render').count() == 1
        await page.evaluate('''()=>{document.querySelector('.geetest_panel').remove();
            const style=document.createElement('style');style.textContent=
                '@keyframes testCaptchaFade{from{opacity:0}to{opacity:1}}';document.head.append(style);
            const panel=document.createElement('div');panel.className='geetest_panel';
            panel.style.cssText='width:100px;height:50px;animation:testCaptchaFade .1s linear forwards';
            document.body.append(panel)}''')
        await page.wait_for_function("Number(getComputedStyle(document.querySelector('.geetest_panel')).opacity)>.9")
        assert (await page.evaluate(PAGE_INSPECTION_SCRIPT, True))['attention'] == 'verification_or_login'
        print('PASS: opt-in only; all videos paused; replay/new-video guarded; static/fading CAPTCHA observable; style idempotent')
    finally:
        await page.close()
        await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cdp-url', required=True)
    asyncio.run(verify(parser.parse_args().cdp_url))
